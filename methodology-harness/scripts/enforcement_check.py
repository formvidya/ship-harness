#!/usr/bin/env python3
"""Enforcement check.

The methodology review's core finding was *enforcement-by-prose*: docs told a
human to make checks required, but nothing verified it — so most gates ran and
changed nothing. This script is the cure. It derives the set of status checks
that SHOULD be required from ``.context/config.yml`` (every gate with
``required: true``, plus the CI fan-in and the context gate) and then either:

  --verify  (default)  reads the branch's actual required checks via the GitHub
                       API and FAILS if any expected check is missing. Ship this
                       as a CI job so a removed required-check turns the build
                       red instead of silently disarming the fleet.
  --apply              writes the expected checks into branch protection (union
                       with whatever is already there — never removes a check a
                       project added itself).
  --list               prints the expected set and exits.

This is what stops the generic harness from shipping the same disease it cures:
"required" becomes a verifiable, enforced fact, not a sentence in a README.

Auth: uses ``gh`` (the GitHub CLI) so it works in CI (GITHUB_TOKEN) and locally.
Repo + branch are auto-detected (``gh repo view`` / config) or passed explicitly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# One config, one loader: reuse the context-harness config loader. Its `.raw`
# exposes the extended methodology sections (gates, languages, ...).
_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import load_config  # noqa: E402

# Default check-name for a gate when config doesn't name one explicitly.
_DEFAULT_CHECK_NAMES = {
    "code_quality": "Code Quality / pre-commit (PR diff)",
    "security": "Security / semgrep (new findings)",
    "security_sca": "Security / sca (dependency CVEs)",
    "security_llm": "security-llm-review",
    "version_check": "Version Check / service VERSION bumped",
    "change_review": "change-review",
}


def expected_checks(cfg) -> list[str]:
    """The status checks that MUST be required, derived from config."""
    gates = cfg.raw.get("gates", {}) or {}
    checks: set[str] = set()
    for name, g in gates.items():
        if not isinstance(g, dict):
            continue
        if name == "ci":
            rc = g.get("required_check")
            if rc:
                checks.add(rc)
            continue
        if g.get("required"):
            checks.add(g.get("check_name") or _DEFAULT_CHECK_NAMES.get(name, name))
    # The context-ledger gate is always required when the ledger is in use.
    if cfg.ci_check_name:
        checks.add(cfg.ci_check_name)
    return sorted(checks)


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def _repo() -> str:
    return _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()


def _current_required(repo: str, branch: str) -> list[str]:
    try:
        out = _gh(
            "api", f"repos/{repo}/branches/{branch}/protection/required_status_checks", "--jq", "[.checks[].context]"
        )
        return json.loads(out)
    except subprocess.CalledProcessError:
        return []


def cmd_verify(cfg, repo: str, branch: str) -> int:
    expected = expected_checks(cfg)
    current = _current_required(repo, branch)
    missing = [c for c in expected if c not in current]
    print(f"enforcement-check: {repo}@{branch}")
    print(f"  expected required checks ({len(expected)}): {expected}")
    print(f"  currently required ({len(current)}): {current}")
    if missing:
        print(f"\n::error::Branch protection is missing required check(s): {missing}")
        print("  Run: python tools/harness/methodology-harness/scripts/enforcement_check.py --apply")
        return 1
    print("\nenforcement-check: PASS -- every expected gate is actually required.")
    return 0


def cmd_apply(cfg, repo: str, branch: str) -> int:
    expected = expected_checks(cfg)
    current = _current_required(repo, branch)
    union = sorted(set(current) | set(expected))
    body = {"strict": True, "checks": [{"context": c} for c in union]}
    subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/branches/{branch}/protection/required_status_checks",
            "--input",
            "-",
        ],
        input=json.dumps(body),
        text=True,
        check=True,
        capture_output=True,
    )
    print(f"enforcement-check: applied {len(union)} required checks to {repo}@{branch}:")
    for c in union:
        print(f"  - {c}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="fail if an expected check isn't required (default)")
    mode.add_argument("--apply", action="store_true", help="write the expected checks into branch protection")
    mode.add_argument("--list", action="store_true", help="print the expected set and exit")
    ap.add_argument("--repo", help="owner/name (default: gh repo view)")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.list:
        for c in expected_checks(cfg):
            print(c)
        return 0
    repo = args.repo or _repo()
    if args.apply:
        return cmd_apply(cfg, repo, args.branch)
    return cmd_verify(cfg, repo, args.branch)


if __name__ == "__main__":
    sys.exit(main())
