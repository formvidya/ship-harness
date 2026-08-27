#!/usr/bin/env python3
"""Ops security-review gate (feat/ops-review-gate — the binding CI teeth).

The durable, non-bypassable half of the ops-review gate. The local Stop hook
(stop_review_gate.py) can be bypassed with ``OPS_REVIEW_BYPASS=1`` and only runs
in an agent session; this checker runs in CI on every PR that touches a
security-sensitive path (infrastructure, workflows, secrets, auth, k8s overlays,
cert-manager/cloudflare). Make it a REQUIRED status in branch protection and a
change to those areas cannot merge without a recorded security pass.

PASS when EITHER:
  1. the PR carries the ``security-reviewed`` label (a human-reviewed security
     pass, applied after review), OR
  2. the context record ``docs/context/records/CTX-{pr:04d}.md`` contains a
     decision with ``agent: security`` in its YAML frontmatter (the Security
     agent recorded findings via ``ctx decide --agent security``).

FAIL (exit 1) otherwise, with a clear remediation message.

Arg handling mirrors check_context_record.py: the PR number comes from the
``PR_NUMBER`` env var (set by the workflow) or a positional ``<pr>`` argument,
falling back to ``gh pr view`` locally. Exit code: 0 = PASS, 1 = FAIL.

The label check needs GitHub API access (``gh``); when it's unavailable (e.g.
local runs with no ``gh``) we fall back to the record check alone, which is the
authoritative signal in-repo.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Robust stdout on legacy Windows code pages (matches check_context_record.py).
try:  # pragma: no cover - environment dependent
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Reuse the context-harness record parser (same YAML-frontmatter model the
# Context Check gate uses) rather than reimplementing a parser.
_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from schema import RecordError, parse_record  # noqa: E402

from config import load_config  # noqa: E402

SECURITY_LABEL = "security-reviewed"
SECURITY_AGENT = "security"


def _is_sensitive_path(f: str) -> bool:
    """True if a changed file makes the PR security-sensitive (and thus requires
    a recorded review). Mirrors the path set the ops-security-review workflow
    used to carry as a ``paths:`` trigger filter — moved in-script so the
    workflow runs on EVERY PR and its required status check always reports.
    (A required check gated by ``paths:`` never fires on out-of-scope PRs, which
    leaves them stuck "Expected — waiting" forever.)"""
    f = f.replace("\\", "/")
    low = f.lower()
    if "secret" in low:  # **/*secret* / **/*Secret*
        return True
    if "cert-manager" in low or "cloudflare" in low:  # **/*cert-manager* etc.
        return True
    if f.startswith("infrastructure/"):  # infrastructure/** incl k8s base/overlays
        return True
    if f.startswith(".github/workflows/"):
        return True
    if f.startswith("shared/secrets_manager/"):
        return True
    if f.startswith("auth/") or "/auth/" in f:  # **/auth/**
        return True
    if f.endswith((".tf", ".tfvars")):
        return True
    if "nginx" in low and f.endswith(".conf"):  # **/*nginx*.conf
        return True
    if f.endswith(".py") and "/core/auth" in f:  # services/**/core/auth*.py
        return True
    if f.endswith("/api/device_auth.py"):
        return True
    return False


def _changed_files(pr_number: int) -> list[str] | None:
    """Changed file paths for the PR. In CI: ``gh api pulls/{n}/files``. Locally:
    ``git diff`` against the merge-base with origin/main. Returns None when it
    cannot be determined, so the caller can fail CLOSED (treat as sensitive)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{repo}/pulls/{pr_number}/files", "--paginate", "--jq", ".[].filename"],
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if out.returncode == 0:
            return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        return None
    # Local fallback: diff against origin/main's merge base.
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if out.returncode == 0:
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    return None


def _resolve_pr_number(arg_pr: str | None) -> int | None:
    if arg_pr and arg_pr.isdigit():
        return int(arg_pr)
    env = os.environ.get("PR_NUMBER")
    if env and env.isdigit():
        return int(env)
    # Local fallback: ask gh for the PR on the current branch.
    try:
        out = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, ValueError):
        return None


def _has_security_label(pr_number: int) -> bool:
    """True if the PR carries the security-reviewed label. Best-effort: any gh
    error (no gh, no token, offline) returns False so the record check decides."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    api_path = f"repos/{repo}/pulls/{pr_number}" if repo else f"pulls/{pr_number}"
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                api_path,
                "--jq",
                f'[.labels[].name] | index("{SECURITY_LABEL}") != null',
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _record_has_security_decision(record_path: Path) -> bool:
    if not record_path.is_file():
        return False
    try:
        rec = parse_record(record_path)
    except (RecordError, OSError):
        return False
    for dec in rec.agent_decisions:
        if isinstance(dec, dict) and str(dec.get("agent", "")).strip() == SECURITY_AGENT:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", nargs="?", help="PR number (else $PR_NUMBER, else gh)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    pr_number = _resolve_pr_number(args.pr)
    if pr_number is None:
        print(
            "security-review: FAIL\n"
            "  -> could not resolve a PR number (pass <pr>, set PR_NUMBER, or run "
            "where `gh pr view` works)."
        )
        return 1

    # Path scoping: the gate runs on every PR (so the required check always
    # reports) but only ENFORCES on security-sensitive changes. A PR that
    # touches nothing sensitive passes immediately.
    files = _changed_files(pr_number)
    if files is None:
        print(
            f"security-review: could not determine changed files for PR #{pr_number} "
            "— treating as security-sensitive (fail-closed)."
        )
    elif not any(_is_sensitive_path(f) for f in files):
        print(
            f"security-review: PASS — PR #{pr_number} touches no security-sensitive "
            f"paths ({len(files)} file(s) changed)."
        )
        return 0

    cfg = load_config()
    record = cfg.record_path(pr_number)
    rel = record.relative_to(cfg.repo_root).as_posix()
    print(f"security-review: PR #{pr_number}, record {rel}")

    if _has_security_label(pr_number):
        print(f"security-review: PASS — PR carries the '{SECURITY_LABEL}' label.")
        return 0

    if _record_has_security_decision(record):
        print(f"security-review: PASS — {rel} has an 'agent: security' decision.")
        return 0

    print("\nsecurity-review: FAIL")
    print(
        f"  -> No security review recorded for PR #{pr_number}.\n"
        "     This PR touches a security-sensitive area (infrastructure, workflows,\n"
        "     secrets, auth, k8s overlays, cert-manager/cloudflare) and must have an\n"
        "     independent security pass on record before it can merge.\n"
        "\n"
        "Fix (any ONE):\n"
        "  1. Run the Security agent over the changes, then record its findings:\n"
        f"       py tools/harness/context-harness/ctx/ctx.py decide --pr {pr_number} \\\n"
        '         --agent security --decision "..." --rationale "..."\n'
        f"     (writes an 'agent: security' decision into {rel}).\n"
        f"  2. After a human-reviewed security pass, apply the '{SECURITY_LABEL}' "
        "label to the PR."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
