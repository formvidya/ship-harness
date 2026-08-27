#!/usr/bin/env python3
"""Turnkey installer for the methodology-harness.

The methodology-harness is the portable, generic development-methodology harness:
the full gate suite (CI matrix + selective CI, code-quality, version-check,
change-review, the two-layer security pipeline) + agent rendering + the vendored
context ledger -- all driven by ONE fill-once file, ``.context/config.yml``.

Drop ``tools/harness/methodology-harness/`` (and its sibling ``tools/harness/context-harness/``,
which it vendors) into a repo, then from the repo root run::

    python tools/harness/methodology-harness/install.py

It is **idempotent** -- never clobbers a filled-in config, only adds what's
missing:

  1. ``.context/config.yml``                from config.example.yml (skipped if present)
  2. ``.github/workflows/*.yml``            the gate workflows (skipped per-file if present)
  3. prints the manual steps it cannot do for you (branch protection, secrets).

Flags:
  --target DIR   install into DIR instead of the detected repo root
  --force        overwrite .context/config.yml from the example
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
# Gate workflows the harness ships. A project enables/disables each via config;
# an unused workflow is harmless (its gate just never has anything to do).
_WORKFLOWS = [
    "ci.yml",
    "code-quality.yml",
    "version-check.yml",
    "change-review.yml",
    "security-scan.yml",
    "security-sca.yml",
    "security-llm-review.yml",
]


def _find_repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    return start


def _copy_if_missing(src: Path, dst: Path, force: bool = False) -> str:
    if not src.is_file():
        return "skip (template missing)"
    if dst.exists() and not force:
        return "skip (exists)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return "wrote"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="repo root to install into (default: auto-detect)")
    ap.add_argument("--force", action="store_true", help="overwrite .context/config.yml")
    args = ap.parse_args(argv)

    repo = Path(args.target).resolve() if args.target else _find_repo_root(Path.cwd())
    print(f"methodology-harness installer -> {repo}")

    steps: list[tuple[str, str]] = [
        (
            ".context/config.yml",
            _copy_if_missing(HARNESS / "config.example.yml", repo / ".context" / "config.yml", args.force),
        )
    ]
    for wf in _WORKFLOWS:
        steps.append(
            (
                f".github/workflows/{wf}",
                _copy_if_missing(HARNESS / "templates" / "workflows" / wf, repo / ".github" / "workflows" / wf),
            )
        )
    for name, status in steps:
        print(f"  [{status}] {name}")

    if not (repo / "tools" / "context-harness").exists():
        print("\n  WARNING: tools/harness/context-harness/ is not present -- this harness vendors it")
        print("  (config loader + ledger). Copy it in and run its install.py first.")

    print("\nDone. Manual steps the installer cannot do for you:")
    print("  1. pip install pyyaml   (the only runtime dependency for the gates)")
    print("  2. Fill in .context/config.yml for your project (services, languages, paths).")
    print("  3. Make the gates actually required (the anti-'advisory theater' step):")
    print("       python tools/harness/methodology-harness/scripts/enforcement_check.py --apply")
    print("       python tools/harness/methodology-harness/scripts/enforcement_check.py --verify  # ship as a CI job")
    print("  4. Seed the generic support squad (testing/security/change-manager/...),")
    print("     add your own engineer agents under .context/agents/, then render:")
    print("       python tools/harness/methodology-harness/scripts/render_agents.py --seed")
    print("     (--seed is idempotent and never overwrites an agent you've written.)")
    print("  5. (Optional) the predictive security layer: set the ANTHROPIC_API_KEY repo")
    print("       secret, then flip gates.security_llm to required:true + escalate enabled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
