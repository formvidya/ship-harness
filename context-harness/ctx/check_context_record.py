#!/usr/bin/env python3
"""Context-record gate.

Verifies that any PR which changes a code-root file (per ``.context/config.yml``)
also adds/updates a valid context record for that PR. This is the load-bearing
enforcement layer — a direct, config-parameterized clone of
``scripts/check_version_bumps.py``: same BASE_SHA/HEAD_SHA env contract, same
``git diff --name-only`` mechanics, same exempt-glob filtering, same
``[skip-context]`` audited override, same per-area PASS/FAIL output.

Modes
-----
**CI mode** (default): diff ``BASE_SHA..HEAD_SHA`` (set by the workflow from the
PR event). ``PR_NUMBER`` identifies the expected record file.

    BASE_SHA=abc HEAD_SHA=def PR_NUMBER=142 python check_context_record.py

**Pre-commit mode** (``--staged``): diff the staged changeset against HEAD. The
PR number is resolved from ``gh`` if available, else the record presence check is
relaxed to "a record file is staged".

Floor
-----
``--floor merged`` (the CI default) lints the record against the merged-level
requirements regardless of its ``status`` field: non-placeholder ``What Was
Done`` / ``Architecture Used`` sections plus ``test_results`` / ``risk_level``
/ ``services_affected`` / ``agent_decisions`` frontmatter. Rationale: the
record describes the change this PR is about to merge, so merged-level
substance is due *before* merge — records previously sat at ``status: open``
forever and the strict floors never bound (in the field, an entire 16-record
backlog shipped with scaffold TODOs). Pre-commit mode defaults to ``--floor status``
(drafting-friendly); CI is the binding layer.

The floor vocabulary, the default, and the per-floor requirement tables all
come from ``schema`` (``FLOOR_CHOICES`` / ``DEFAULT_GATE_FLOOR`` /
``requirements_for``) and are shared verbatim with ``ctx lint`` — see the
comment above ``RECORD_NATIVE_FLOOR`` in ``schema.py`` for why.

Override
--------
Include ``[skip-context]`` (the marker from config) in any commit message in the
PR range to bypass; the bypass is printed to the run log for audit, mirroring
``[skip-version-check]``.

Exit code: 0 = PASS, 1 = FAIL. Designed to be the entry point for both
``.github/workflows/context-check.yml`` and the pre-commit hook.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Make stdout robust on legacy Windows code pages (cp1252). Output is kept
# ASCII, but this guards against any record path / message that isn't.
try:  # pragma: no cover - environment dependent
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import (  # noqa: E402
    DEFAULT_GATE_FLOOR,
    FLOOR_CHOICES,
    RECORD_NATIVE_FLOOR,
    lint_record,
    normalize_floor,
)

from config import Config, load_config  # noqa: E402


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _changed_files(cfg: Config, base: str | None, head: str | None, staged: bool) -> list[str]:
    root = cfg.repo_root
    if staged:
        out = _git("diff", "--cached", "--name-only", cwd=root)
    else:
        if not base or not head:
            raise SystemExit("CI mode requires BASE_SHA and HEAD_SHA env vars.")
        out = _git("diff", "--name-only", f"{base}..{head}", cwd=root)
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def _commit_messages(cfg: Config, base: str | None, head: str | None, staged: bool) -> str:
    root = cfg.repo_root
    try:
        if staged:
            return _git("log", "-1", "--format=%B", cwd=root)
        return _git("log", "--format=%B", f"{base}..{head}", cwd=root)
    except subprocess.CalledProcessError:
        return ""


def _resolve_pr_number(cfg: Config, staged: bool) -> int | None:
    env = os.environ.get("PR_NUMBER")
    if env and env.isdigit():
        return int(env)
    # pre-commit fallback: ask gh for the PR on the current branch.
    try:
        out = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            cwd=cfg.repo_root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="pre-commit mode (diff staged vs HEAD)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--floor",
        choices=FLOOR_CHOICES,
        default=None,
        help=(
            f"lint the record at this lifecycle floor (shared vocabulary with `ctx lint`). "
            f"Default: '{DEFAULT_GATE_FLOOR}' in CI mode (substance is due before merge), "
            f"'{RECORD_NATIVE_FLOOR}' in --staged mode (drafting-friendly)."
        ),
    )
    args = parser.parse_args(argv)
    floor = args.floor or (RECORD_NATIVE_FLOOR if args.staged else DEFAULT_GATE_FLOOR)
    lint_floor = normalize_floor(floor)

    cfg = load_config()
    base = os.environ.get("BASE_SHA")
    head = os.environ.get("HEAD_SHA")
    mode = "staged" if args.staged else "CI"

    changed = _changed_files(cfg, base, head, args.staged)
    print(f"context-check: mode={mode}, {len(changed)} file(s) in diff")

    # Audited override (mirrors [skip-version-check]).
    msgs = _commit_messages(cfg, base, head, args.staged)
    if cfg.skip_marker in msgs:
        line = f"context-check: SKIPPED via {cfg.skip_marker} (logged)"
        print(f"  [!] {line}")
        _append_override_log(cfg, line)
        return 0

    # Which changed files are non-exempt and fall under a code root?
    code_changes: dict[str, list[str]] = {}
    for f in changed:
        if cfg.is_exempt(f):
            continue
        root = cfg.matched_code_root(f)
        if root:
            code_changes.setdefault(root, []).append(f)

    if not code_changes:
        print("context-check: no non-exempt code-root files changed -> PASS")
        return 0

    pr_number = _resolve_pr_number(cfg, args.staged)
    record_changed = any(_is_record_path(cfg, f) for f in changed)

    failures: list[str] = []

    # 1. A record file must be touched by this PR.
    if pr_number is not None:
        record = cfg.record_path(pr_number)
        rel = record.relative_to(cfg.repo_root).as_posix()
        if not record.is_file():
            failures.append(
                f"code changed but no context record at {rel}. "
                f"Create it (ctx init --pr {pr_number}); see the context-harness docs."
            )
        elif rel not in changed and not args.staged:
            failures.append(
                f"context record {rel} exists but was not updated in this PR. A change to code must update its record."
            )
        else:
            problems = lint_record(record, floor=lint_floor)
            if problems:
                failures.append(f"{rel} failed lint (floor={floor}):\n      - " + "\n      - ".join(problems))
    elif not record_changed:
        # pre-commit with no resolvable PR: require *some* record to be staged.
        failures.append(
            f"code changed but no context record file is staged. Add/update a record under {cfg.ledger.records_dir}/."
        )

    print("\nPer-code-root results:")
    for root, files in sorted(code_changes.items()):
        sample = ", ".join(files[:3]) + (" ..." if len(files) > 3 else "")
        status = "FAIL" if failures else "PASS"
        print(f"  [{status}] {root}  ({len(files)} file(s): {sample})")

    if failures:
        print("\ncontext-check: FAIL")
        for fmsg in failures:
            print(f"  -> {fmsg}")
        print(
            f"\nFix: add/update the record, OR add '{cfg.skip_marker}' to a commit "
            f"message if this is a documented exception (logged). "
            f"See the context-harness docs for the record schema."
        )
        return 1

    print("\ncontext-check: PASS")
    return 0


def _is_record_path(cfg: Config, rel: str) -> bool:
    return rel.replace("\\", "/").startswith(cfg.ledger.records_dir.rstrip("/") + "/")


def _append_override_log(cfg: Config, line: str) -> None:
    if not cfg.ledger.overrides_log:
        return
    log = cfg.repo_root / cfg.ledger.overrides_log
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # logging is best-effort; never fail the gate on a log write


if __name__ == "__main__":
    sys.exit(main())
