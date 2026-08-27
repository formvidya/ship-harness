#!/usr/bin/env python3
"""Unified installer for the harness suite -- ONE command, BOTH harnesses.

``tools/harness/`` bundles the two harnesses that ship together:

  - ``context-harness/``      the portable context-ledger: the config
                              loader, the per-change record gate, ``ctx``.
  - ``methodology-harness/``  the full dev-methodology: the gate suite
                              (selective CI, code-quality, version-check,
                              change-review, the two-layer security pipeline) +
                              agent rendering. It IMPORTS the context-harness
                              config loader, so the two are installed together.

Drop ``tools/harness/`` into a repo and run, from the repo root::

    python tools/harness/install.py

It runs both sub-installers in the right order (methodology first, so its
SUPERSET ``.context/config.yml`` -- ledger keys + methodology keys -- becomes the
single fill-once file; then context-harness adds the ledger infra: the Context
Check gate, the pre-dev hook, .gitignore entries, and ``ctx bootstrap``). Both
sub-installers are idempotent, so re-running never clobbers a filled-in config.

Flags (forwarded to both):
  --target DIR   install into DIR instead of the detected repo root
  --force        overwrite .context/config.yml from the methodology example
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _run(label: str, script: Path, args: list[str]) -> int:
    print(f"\n{'=' * 70}\n  {label}\n{'=' * 70}")
    if not script.is_file():
        print(f"  SKIP: {script} not found")
        return 0
    return subprocess.run([sys.executable, str(script), *args]).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="repo root to install into (default: auto-detect)")
    ap.add_argument("--force", action="store_true", help="overwrite .context/config.yml")
    args = ap.parse_args(argv)

    forwarded: list[str] = []
    if args.target:
        forwarded += ["--target", args.target]
    if args.force:
        forwarded += ["--force"]

    print("harness suite installer (context-ledger + methodology)")

    # Methodology FIRST: its config.example.yml is the SUPERSET, so it becomes the
    # canonical .context/config.yml. Context-harness then skips the (now-present)
    # config and installs only the ledger-specific infra + runs ctx bootstrap.
    rc = _run("1/2  methodology-harness", HERE / "methodology-harness" / "install.py", forwarded)
    rc |= _run(
        "2/2  context-harness (ledger infra + ctx bootstrap)", HERE / "context-harness" / "install.py", forwarded
    )

    print(f"\n{'=' * 70}")
    print("  harness suite installed." if rc == 0 else "  harness suite installed WITH ERRORS (see above).")
    print("  Single source of truth: .context/config.yml  (fill it in once).")
    print("  Re-run anytime; it never clobbers your config.")
    print(f"{'=' * 70}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
