#!/usr/bin/env python3
"""Historian-check — Claude Code ``UserPromptSubmit`` hook (advisory only).

At the *start* of a turn, remind the agent if there is prior ops/infra work on
this branch that has not yet been routed through a security/testing review and
the Historian (the context ledger). Purely advisory: it injects context, it
NEVER blocks — blocking is the Stop hook's job ("gate the exit, not the
emergency"). This just keeps unreviewed work visible so it isn't forgotten.

- If ``.claude/ops-review-owed-<branch>`` exists without a newer
  ``.claude/ops-reviewed-<branch>`` → emit an ``additionalContext`` payload.
- Otherwise → print nothing.

Exit code: always 0.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


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
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch)


def _repo_root() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return Path(out) if out else None
    except OSError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def main() -> int:
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except (OSError, ValueError):
            pass

    root = _repo_root()
    if root is None:
        return 0

    branch = _branch(root)
    owed = root / ".claude" / f"ops-review-owed-{branch}"
    reviewed = root / ".claude" / f"ops-reviewed-{branch}"

    owed_mtime = _mtime(owed)
    if owed_mtime is None:
        return 0

    reviewed_mtime = _mtime(reviewed)
    if reviewed_mtime is not None and reviewed_mtime >= owed_mtime:
        return 0

    rel = f".claude/ops-review-owed-{branch}"
    context = (
        f"⚠ Prior ops/infra work on this branch is unreviewed ({rel}). "
        "Route it through a security/testing agent + the Historian (ctx) "
        "before starting new work."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
