#!/usr/bin/env python3
"""Deterministic clearance gate for the LLM security reviewer.

THE security-critical piece of the two-layer security pipeline. The model
(run_llm_review.py) only *finds* and *labels*; THIS no-LLM job owns the merge
decision -- so model non-determinism degrades to toil (a human dismissal),
never a flaky red build. It is the required status check.

It reads the PR's CURRENT labels via the GitHub API (not the stale webhook
payload) and decides:

  - no escalation label                       -> PASS  (reviewer found nothing blocking)
  - escalation present, no clearance          -> FAIL  (a non-author must fix or clear)
  - clearance present -> PASS only if BOTH:
        * the clearer is NOT the PR author     (no self-clear), and
        * the clearance was applied AFTER the head commit (any push re-arms;
          a stale clearance can't wave through new code).
    otherwise FAIL ("self-clear forbidden" / "stale clearance").

The non-author + freshness checks ARE the whole safety argument, so they are
written and tested here -- never assumed/"reused". Labels + policy come from
gates.security_llm.approval in .context/config.yml.

(The same SHA/non-author binding now lives in change-review.yml's team mode;
this is the standalone, unit-tested form the design called for.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, load_config  # noqa: E402

_DEFAULT_ESCALATION = "security-review-required"
_DEFAULT_CLEARANCE = "security-cleared"


def _instant(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to a UTC instant. Handles both the events
    API's 'Z' form AND the commits API's offset form (e.g. +09:00 / -07:00) --
    a raw STRING compare of those is wrong because '+'/'-' sort before 'Z'.
    Returns None on missing/unparseable (caller fails closed)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def security_llm_cfg(cfg: Config) -> dict:
    return (cfg.raw.get("gates", {}) or {}).get("security_llm", {}) or {}


def approval_cfg(cfg: Config) -> dict:
    return security_llm_cfg(cfg).get("approval", {}) or {}


# ── the pure decision (heavily tested; no I/O) ───────────────────────────────
def decide(
    labels,
    clearance_event,
    head_time,
    pr_author,
    *,
    esc_label,
    clr_label,
    require_non_author,
    bind_to_sha,
):
    """(passed: bool, reason: str). clearance_event is {actor, created_at} or
    None; head_time is the head commit's ISO timestamp (or None to skip the
    freshness check). Timestamps are compared as parsed INSTANTS (not strings)."""
    labels = set(labels or [])
    if esc_label not in labels:
        return True, f"no '{esc_label}' label -- the LLM reviewer found nothing blocking. PASS."

    if clr_label not in labels:
        who = "A NON-AUTHOR" if require_non_author else "Someone"
        return (
            False,
            f"'{esc_label}' is present and uncleared. {who} must fix the finding "
            f"or apply '{clr_label}' after reviewing it. FAIL.",
        )

    if not clearance_event:
        return (
            False,
            f"'{clr_label}' is present but no labeling event was found -- cannot verify clearer or freshness. FAIL.",
        )

    actor = clearance_event.get("actor") or ""
    if require_non_author and actor and actor == pr_author:
        return False, f"'{clr_label}' was self-applied by the PR author ({actor}) -- self-clear is forbidden. FAIL."

    if bind_to_sha and head_time:
        applied = clearance_event.get("created_at") or ""
        applied_dt, head_dt = _instant(applied), _instant(head_time)
        # Fail CLOSED if either side is missing/unparseable -- never pass a
        # clearance whose freshness we cannot verify. Compare INSTANTS, not
        # strings (offset vs Z forms sort wrong lexicographically).
        if applied_dt is None or head_dt is None or applied_dt < head_dt:
            return (
                False,
                f"'{clr_label}' (applied {applied or '?'}) does not provably post-date the head commit "
                f"({head_time or '?'}) -- stale or unverifiable; re-clear at the current commit. FAIL.",
            )

    return True, f"valid clearance by {actor or '?'} after the head commit. PASS."


# ── isolated GitHub API (mocked in tests) ────────────────────────────────────
def _gh(*args: str, _retries: int = 3) -> str:
    """gh with a small retry so a transient API blip does not make a BLOCKING
    gate fail open. Raises CalledProcessError after the last attempt."""
    last: Exception | None = None
    for i in range(_retries):
        try:
            return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError as exc:  # pragma: no cover - timing
            last = exc
            if i < _retries - 1:
                time.sleep(1 + i)
    raise last  # type: ignore[misc]


def detect_repo() -> str:
    return _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()


def current_labels(repo: str, pr: str) -> list[str] | None:
    """Current label names, or None if the API can't be read (caller fails
    CLOSED -- a blocking gate must not pass just because it couldn't see labels)."""
    try:
        return json.loads(_gh("api", f"repos/{repo}/pulls/{pr}", "--jq", "[.labels[].name]"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def last_clearance_event(repo: str, pr: str, clr_label: str) -> dict | None:
    """The most recent 'labeled' event for the clearance label: {actor, created_at}.
    Uses --slurp so pagination can't truncate, then sorts by time and takes the
    last (a bare `--paginate | last` returns the last-of-EACH-page)."""
    try:
        out = _gh(
            "api",
            f"repos/{repo}/issues/{pr}/events",
            "--paginate",
            "--slurp",
            "--jq",
            f'[.[][] | select(.event=="labeled" and .label.name=="{clr_label}")] | sort_by(.created_at) | last',
        )
        ev = json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    if not ev:
        return None
    return {"actor": (ev.get("actor") or {}).get("login", ""), "created_at": ev.get("created_at", "")}


def commit_time(repo: str, sha: str) -> str | None:
    try:
        return _gh("api", f"repos/{repo}/commits/{sha}", "--jq", ".commit.committer.date").strip() or None
    except subprocess.CalledProcessError:
        return None


def _log_override(cfg: Config, ap: dict, pr: str, event: dict | None) -> None:
    path = ap.get("overrides_log")
    if not path:
        return
    p = cfg.repo_root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    actor = (event or {}).get("actor", "?")
    at = (event or {}).get("created_at", "?")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"PR #{pr}\tcleared_by={actor}\tat={at}\n")


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = approval_cfg(cfg)
    esc = ap.get("escalation_label", _DEFAULT_ESCALATION)
    clr = ap.get("clearance_label", _DEFAULT_CLEARANCE)
    require_non_author = ap.get("require_non_author", True)
    bind_to_sha = ap.get("bind_to_sha", True)

    repo = os.environ.get("REPO") or detect_repo()
    pr = os.environ.get("PR_NUMBER", "")
    pr_author = os.environ.get("PR_AUTHOR", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    labels = current_labels(repo, pr)
    if labels is None:
        # Couldn't read labels after retries -> FAIL CLOSED. A blocking security
        # gate must never pass merely because it couldn't see the label state.
        print(f"::error::security-llm gate: could not read PR #{pr} labels -- failing closed.")
        return 1

    event = last_clearance_event(repo, pr, clr) if clr in labels else None
    head_time = None
    if bind_to_sha and head_sha and esc in labels and clr in labels:
        head_time = commit_time(repo, head_sha)
        if head_time is None:
            print(
                f"::error::security-llm gate: could not read head ({head_sha}) time to verify freshness -- failing closed."
            )
            return 1

    passed, reason = decide(
        labels,
        event,
        head_time,
        pr_author,
        esc_label=esc,
        clr_label=clr,
        require_non_author=require_non_author,
        bind_to_sha=bind_to_sha,
    )
    print(f"security-llm gate: {reason}")
    if passed and esc in labels:
        _log_override(cfg, ap, pr, event)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
