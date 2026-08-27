#!/usr/bin/env python3
"""Pre-dev consultation gate.

A Claude Code ``PreToolUse`` hook for ``Edit``/``Write``. It blocks the *first*
edit of a code-root file on a branch until ``ctx query`` has been run (which
writes ``.claude/context-consulted-{branch}`` as a side effect). The well-behaved
path — query first — is frictionless; this only catches the edit that skipped it.

It is a *local nudge*, not the enforcement of record. The binding enforcement is
the required ``Context Check`` CI gate (check_context_record.py). This hook just
makes "look before you leap" the default for agent-driven work.

Config-aware: reads ``.context/config.yml`` for the code roots — no
project paths are hard-coded here. Best-effort path extraction from the raw tool
input; when in doubt it allows (a local nudge must never hard-block real work on
a parse miss).

Exit 0 = allow. Exit 1 = block (Claude Code surfaces the message to the agent).
Override: ``CTX_SKIP_PREDEV=1`` in the environment, or just run ``ctx query``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load_config  # noqa: E402


def _tool_input(argv: list[str]) -> str:
    """Best-effort raw tool-input string for path extraction.

    Current Claude Code passes the tool call as JSON on **stdin**
    (``{"tool_name":"Edit","tool_input":{"file_path":"...", ...}}``); the older
    wiring passed ``$TOOL_INPUT`` as argv. Prefer stdin JSON, fall back to an
    argv join so both contracts fire. When we get JSON we flatten the
    ``tool_input`` values into one string (file paths + content) so the code-root
    prefix match below still works. Never raises — a nudge must not crash on a
    parse miss."""
    raw = ""
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError):
            raw = ""
    raw = raw.strip()
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            ti = data.get("tool_input")
            if isinstance(ti, dict):
                return " ".join(str(v) for v in ti.values())
            if isinstance(ti, str):
                return ti
            # JSON with no usable tool_input -> fall through to argv.
        if data is not None:
            # Parsed but unusable shape; still prefer argv over the raw JSON blob.
            return " ".join(argv)
        return raw  # not JSON at all -> treat as the literal tool input
    return " ".join(argv)


def _branch(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = out or "detached"
    except OSError:
        branch = "unknown"
    # Must match query.py's marker naming exactly (flat, path-safe).
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("CTX_SKIP_PREDEV") == "1":
        return 0
    argv = argv if argv is not None else sys.argv[1:]
    tool_input = _tool_input(argv)
    if not tool_input.strip():
        return 0

    try:
        cfg = load_config()
    except SystemExit:
        return 0  # no config -> harness not installed here -> don't block

    # Does the edit target a code root? Match the literal prefix of each glob
    # (e.g. "services/**" -> "services") against the raw input, tolerating both
    # "/" and "\" separators (absolute Windows paths included).
    targets_code_root = False
    for root in cfg.code_roots:
        prefix = root.split("*")[0].strip("/")
        if not prefix:
            continue
        if re.search(rf"(^|[\s\"'/\\]){re.escape(prefix)}[/\\]", tool_input):
            targets_code_root = True
            break
    if not targets_code_root:
        return 0

    marker = cfg.repo_root / ".claude" / f"context-consulted-{_branch(cfg.repo_root)}"
    if marker.exists():
        return 0

    print(
        "BLOCKED (context): you're editing a code-root file but haven't consulted "
        "the context ledger on this branch.\n"
        "  Run first:  python tools/harness/context-harness/ctx/ctx.py query "
        '--service <svc> --intent "<what you\'re doing>"\n'
        "  Read the [BAD] decisions and open loops it returns before editing.\n"
        "  (Override: set CTX_SKIP_PREDEV=1. The required Context Check CI gate "
        "still enforces the record at merge.)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
