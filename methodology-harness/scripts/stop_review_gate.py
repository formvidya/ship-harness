#!/usr/bin/env python3
"""Stop-review gate — Claude Code ``Stop`` hook.

The teeth of the "gate the exit, not the emergency" design. The ops-marker gate
(:mod:`ops_marker_gate`) logs every mutating infra/CI/secret command to
``.claude/ops-review-owed-<branch>`` without ever blocking. THIS hook blocks the
*turn* from finishing while that marker is unreviewed — forcing an independent
security/testing pass before work is declared done.

Decision
--------
- ``OPS_REVIEW_BYPASS=1`` in the environment → allow (emergency escape hatch).
- If ``.claude/ops-review-owed-<branch>`` exists AND
  (``.claude/ops-reviewed-<branch>`` is missing OR the owed marker is newer than
  the reviewed marker) → emit ``{"decision":"block","reason":"..."}`` and exit 0.
- Otherwise → print nothing, exit 0 (allow the stop).

The decision is that mtime comparison and nothing else. The op COUNT in the
block message is advisory — it is what a human reads to size up the turn's
blast radius — so it is computed after the verdict, from the marker's RECORD
format (see :data:`ops_marker_gate.MARKER_RECORD_RE`), and degrades to a
number-free sentence rather than ever changing whether we block.

Fail-open, never wedge: if we are not in a git repo, or the markers can't be
read, we allow. A broken gate must not trap an agent mid-session.

Exit code: always 0 (the block is expressed via the JSON ``decision`` field,
per the Claude Code Stop-hook contract, not via a non-zero exit).
"""

from __future__ import annotations

import json
import os
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


def _count_records(owed: Path) -> int | None:
    """Logged mutations in the owed marker, or ``None`` when the count cannot be
    trusted.

    Counts RECORDS, not lines. ops_marker_gate writes one record per mutating
    command as ``<ISO8601>\\t<command>`` with the command copied verbatim, so a
    multi-line body (a ``gh pr comment --body`` with markdown in it, a heredoc)
    spans many lines and is still one op. Counting non-empty lines instead
    reported "21 mutating ops" for two commands.

    The record shape is imported from the writer rather than restated here —
    that restatement is exactly how the two drifted. The import is lazy and
    guarded because this is the fail-open Stop hook: an unimportable sibling
    must degrade the *message* (``None`` → no number claimed), never the block.
    """
    try:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from ops_marker_gate import count_records

        return count_records(owed.read_text(encoding="utf-8"))
    except (OSError, ImportError, ValueError):
        return None


def _summary(n: int | None, rel: str) -> str:
    """The headline a human reads to size up the turn's blast radius.

    Degrades honestly. The marker existing is what makes this hook block, and
    that is known independently of any count — so when the count is unavailable
    (unreadable marker) or zero (marker present, no complete records yet: a
    torn write, or a hand-touched file), say that instead of asserting
    "0 mutating ops ran this session", which reads as "nothing happened" while
    the gate is blocking on the opposite claim.
    """
    if n is None:
        return f"Unreviewed ops are recorded in {rel} (count unavailable — marker unreadable)"
    if n == 0:
        return f"Unreviewed ops are recorded in {rel} (no complete records to count)"
    unit = "op" if n == 1 else "ops"
    return f"{n} mutating {unit} ran this session (logged in {rel})"


def main() -> int:
    # Drain stdin (the Stop payload) so we don't leave the pipe unread; we don't
    # actually need any field from it, but reading keeps the contract clean.
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except (OSError, ValueError):
            pass

    if os.environ.get("OPS_REVIEW_BYPASS") == "1":
        return 0

    root = _repo_root()
    if root is None:
        return 0  # not a git repo -> never wedge

    branch = _branch(root)
    owed = root / ".claude" / f"ops-review-owed-{branch}"
    reviewed = root / ".claude" / f"ops-reviewed-{branch}"

    owed_mtime = _mtime(owed)
    if owed_mtime is None:
        return 0  # nothing owed (or unreadable) -> allow

    reviewed_mtime = _mtime(reviewed)
    # Reviewed and at least as new as the last owed op -> cleared.
    if reviewed_mtime is not None and reviewed_mtime >= owed_mtime:
        return 0

    # Count logged mutations for the message (best-effort; never blocks on it).
    rel = f".claude/ops-review-owed-{branch}"
    reason = (
        f"{_summary(_count_records(owed), rel)} with no recorded review.\n"
        "Before finishing:\n"
        "  1. Run a security AND/OR testing agent over the changes.\n"
        "  2. Record findings via:\n"
        "       py tools/harness/context-harness/ctx/ctx.py decide --agent security \\\n"
        '         --decision "..." --rationale "..."\n'
        f"  3. Clear the gate:  touch .claude/ops-reviewed-{branch}\n"
        "Emergency bypass: OPS_REVIEW_BYPASS=1 (records that you skipped review)."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
