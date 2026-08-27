#!/usr/bin/env python3
"""Generic security-scan gate.

Runs Semgrep as a DIFF scan (only new findings block) with the three
improvements the review flagged over the hand-rolled original this replaces:

  - rulesets come from config (``gates.security.configs``) and DEFAULT TO
    including ``p/secrets`` -- the repo has secret-handling scripts that the
    original rulesets didn't cover;
  - emits SARIF so findings surface in the GitHub Security tab (the workflow
    uploads it when ``gates.security.sarif_upload`` is true);
  - the Semgrep version is pinned in the workflow image, not floating.

This is a thin, testable command-builder around the semgrep CLI -- semgrep IS
the engine, so there's no bespoke analysis here. Everything project-specific
(rulesets, severities) comes from ``.context/config.yml``.

    gates:
      security:
        configs: [p/python, p/owasp-top-ten, p/security-audit, p/secrets]
        sarif_upload: true

Override: ``[skip-security-scan]`` in any commit message in the range (logged).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, load_config  # noqa: E402

SKIP_MARKER = "[skip-security-scan]"
_DEFAULT_CONFIGS = ["p/python", "p/owasp-top-ten", "p/security-audit", "p/secrets"]
_DEFAULT_SEVERITIES = ["ERROR", "WARNING"]


def security_cfg(cfg: Config) -> dict:
    return (cfg.raw.get("gates", {}) or {}).get("security", {}) or {}


def build_semgrep_argv(cfg: Config, base: str | None, sarif_path: str | None) -> list[str]:
    """The semgrep command. Diff-scan against `base`; configured rulesets;
    ERROR+WARNING; SARIF output when requested. Deterministic + unit-tested."""
    sec = security_cfg(cfg)
    configs = sec.get("configs") or _DEFAULT_CONFIGS
    severities = sec.get("severities") or _DEFAULT_SEVERITIES
    argv = ["semgrep", "scan", "--error", "--metrics=off"]
    for c in configs:
        argv += ["--config", c]
    for s in severities:
        argv += ["--severity", s]
    if base:
        argv += ["--baseline-commit", base]
    if sarif_path:
        argv += ["--sarif", "--output", sarif_path]
    return argv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sarif", help="write SARIF to this path (for the Security tab)")
    ap.add_argument("--print-argv", action="store_true", help="print the semgrep command and exit (no scan)")
    args = ap.parse_args(argv)

    cfg = load_config()
    base = os.environ.get("BASE_SHA")
    head = os.environ.get("HEAD_SHA")

    sec = security_cfg(cfg)
    sarif_path = args.sarif if (args.sarif and sec.get("sarif_upload")) else None
    cmd = build_semgrep_argv(cfg, base, sarif_path)

    if args.print_argv:
        print(" ".join(cmd))
        return 0

    if SKIP_MARKER in _commit_messages(cfg, base, head):
        print(f"security-scan: SKIPPED via {SKIP_MARKER} (logged)")
        return 0

    print("security-scan: " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=cfg.repo_root).returncode
    except FileNotFoundError:
        print("::error::semgrep is not installed. The workflow must provide it (pinned image).")
        return 1


def _commit_messages(cfg: Config, base, head) -> str:
    try:
        return subprocess.run(
            ["git", "log", "--format=%B", f"{base}..{head}"],
            cwd=cfg.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
