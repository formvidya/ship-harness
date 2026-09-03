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

Historian
---------
``--require-historian`` (on by default in CI mode) demands an ``agent_decisions``
entry from ``context-keeper``. ``--historian-advisory`` keeps that axis
*evaluated* but demotes it from fatal to a printed WARNING; the workflow passes
it on DRAFT pushes only. The Historian reads a change at the END of its
lifecycle, so mid-draft its decision is not late, it is not yet due — and a
required check that is red on every draft push of every code PR is the
always-red signal nobody reads. The axis is still reported, so a
green draft run never reads as "the Historian has been here". Every other axis
stays fatal on drafts, which is the whole point of this job not being
draft-gated.

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
    HISTORIAN_AGENT,
    RECORD_NATIVE_FLOOR,
    is_historian_problem,
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
    parser.add_argument(
        "--require-historian",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            f"require an agent_decisions entry from '{HISTORIAN_AGENT}'. Default: on in CI mode "
            f"(the record is about to merge, so it is due an author-independent read), off in "
            f"--staged mode (the Historian runs at the end of a change, not while you draft it)."
        ),
    )
    parser.add_argument(
        "--historian-advisory",
        action="store_true",
        help=(
            "evaluate the historian requirement but report it as a non-fatal WARNING instead of "
            "failing. For DRAFT pushes: the Historian runs at the end of a change, so its absence "
            "mid-draft is the expected state, not a defect. The axis is still reported, so a green "
            "draft run never reads as 'the Historian has been here'."
        ),
    )
    args = parser.parse_args(argv)
    floor = args.floor or (RECORD_NATIVE_FLOOR if args.staged else DEFAULT_GATE_FLOOR)
    lint_floor = normalize_floor(floor)
    require_historian = args.require_historian if args.require_historian is not None else not args.staged
    # --historian-advisory sets the SEVERITY of the historian finding, not whether the
    # axis is looked at, so it implies evaluation even under an explicit
    # --no-require-historian: reporting-and-not-failing is strictly more informative
    # than not looking, and a green draft run that never mentions the Historian is the
    # silent skip this flag removed.
    evaluate_historian = require_historian or args.historian_advisory

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
    deferred: list[str] = []

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
            problems = lint_record(record, floor=lint_floor, require_historian=evaluate_historian)
            if args.historian_advisory:
                # Split the historian axis out of the flat problem list. `schema` owns
                # both the message and the predicate, so the gate never re-derives WHEN
                # the requirement bites -- only what it costs. A second copy of the floor
                # gating here would rebuild the local-vs-CI drift that centralising it
                # was meant to prevent.
                deferred = [msg for msg in problems if is_historian_problem(msg)]
                problems = [msg for msg in problems if not is_historian_problem(msg)]
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

    if deferred:
        print("\nDeferred to Ready-flip (WARNING -- does not fail this run):")
        for msg in deferred:
            print(f"  -> {msg}")
        print("  EXPECTED until the Historian runs: context-keeper reads a change at the END")
        print("  of its lifecycle, so on a draft its absence is the normal state and not a")
        print("  defect. Failing here would be red on every draft push of every code PR,")
        print("  which is how a gate becomes wallpaper nobody reads. The SAME requirement is")
        print("  FATAL once this PR is marked Ready for review, and in the merge queue, so")
        print("  it must be satisfied before merge either way.")
        # The annotation is what a reviewer sees on the PR without opening the run
        # log, so it has to carry the "not broken" reading on its own. Emitted after
        # the human block: the raw ::warning:: line also lands in the log, and
        # interleaving one per finding breaks up the paragraph a person reads.
        print(
            "::warning title=Historian decision outstanding (expected on a draft)::"
            "This record has no context-keeper decision yet. That is EXPECTED until the "
            "Historian runs and does NOT mean the PR is broken -- the agent reads a change "
            "at the end of its lifecycle. It becomes fatal when this PR is marked Ready "
            "for review, so run the context-keeper agent before flipping it."
        )

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

    note = f" ({len(deferred)} deferred to Ready-flip, see above)" if deferred else ""
    print(f"\ncontext-check: PASS{note}")
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
