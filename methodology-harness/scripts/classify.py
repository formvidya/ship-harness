#!/usr/bin/env python3
"""Generic change-risk classifier.

Extracts the 170-line *inline* change-review Python that used to live in the
workflow YAML into a tested, config-driven module (the review flagged the
inline-script smell). Classifies a
PR's diff as LOW (auto-approve) or HIGH (needs a non-author release-approved
clearance). Every risk rule comes from ``.context/config.yml`` --
``gates.change_review.risk`` -- so there are no hard-coded service names or paths.

    gates:
      change_review:
        risk:
          protected_paths: ["services/*/src/core/auth.py", "**/*.tf", "infrastructure/k8s/base/**"]
          safe_nonservice:  ["docs/**", "tools/**", ".github/**", ".context/**", "**/*.md"]
          scope_roots:      ["services/**"]     # multi-<unit> changes escalate
          max_risk_files:   10
          signals:
            - {name: secrets,        path_glob: "**",              added_regex: "(JWT_SECRET|AWS_SECRET).*="}
            - {name: schema,         path_glob: "**/models/*.py",  added_regex: "Field\\(|Column\\(", removed_regex: "Field\\(|Column\\("}
            - {name: network_policy, path_glob: "**",              added_regex: "kind:\\s*NetworkPolicy"}
            - {name: dockerfile_port,path_glob: "**/Dockerfile",   added_regex: "EXPOSE\\s"}

``added_regex``/``removed_regex`` are CONTENT patterns (no line-sign anchor);
the classifier matches them on added (``+``) / removed (``-``) diff lines.

The result is a verdict + reasons; ``main()`` prints them and exits 1 for HIGH so
the gate workflow blocks. The workflow owns the *clearance* logic (non-author +
SHA-bound label); this module owns only the risk classification.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, _glob_match, load_config  # noqa: E402

_DEFAULT_SAFE = ["docs/**", "tools/**", ".github/**", ".context/**", ".claude/**", "**/*.md"]


@dataclass
class Verdict:
    level: str  # "LOW" | "HIGH"
    reasons: list[str]


def _risk_cfg(cfg: Config) -> dict:
    return ((cfg.raw.get("gates", {}) or {}).get("change_review", {}) or {}).get("risk", {}) or {}


def classify(cfg: Config, changed: list[str], diff_for) -> Verdict:
    """Pure classification. ``diff_for(path) -> str`` returns the unified diff for
    a path (injected so tests don't need git)."""
    rc = _risk_cfg(cfg)
    protected = rc.get("protected_paths", [])
    safe = rc.get("safe_nonservice", _DEFAULT_SAFE)
    scope_roots = rc.get("scope_roots") or list(cfg.code_roots)
    max_files = rc.get("max_risk_files", 10)
    signals = rc.get("signals", [])
    reasons: list[str] = []

    if not changed:
        return Verdict("LOW", ["no files changed"])

    # 1. Per-file content signals (protected paths + configured regex signals).
    for f in changed:
        if any(_glob_match(g, f) for g in protected):
            reasons.append(f"protected path: {f}")
        # Content signals are CODE concerns: a docs/markdown/tooling file that
        # merely *mentions* a secret keyword (e.g. an agent doc with a
        # `JWT_SECRET = ...` example) is not a secrets change. Skip the regex
        # signals for safe-nonservice paths -- protected_paths above still apply.
        if any(_glob_match(g, f) for g in safe):
            continue
        for sig in signals:
            if not _glob_match(sig.get("path_glob", "**"), f):
                continue
            added = sig.get("added_regex")
            removed = sig.get("removed_regex")
            if not (added or removed):
                reasons.append(f"{sig.get('name', 'signal')}: {f}")
                continue
            diff = diff_for(f)
            hit = (added and _diff_has(diff, added, "+")) or (removed and _diff_has(diff, removed, "-"))
            if hit:
                reasons.append(f"{sig.get('name', 'signal')}: {f}")

    # 2. Scope: >1 scoped unit, or an unclassified non-safe non-scope path.
    units: set[str] = set()
    unclassified: list[str] = []
    for f in changed:
        root = _scoped_unit(f, scope_roots)
        if root:
            units.add(root)
        elif not any(_glob_match(g, f) for g in safe):
            unclassified.append(f)
    if len(units) > 1 and not reasons:
        reasons.append(f"changes span multiple units: {', '.join(sorted(units))}")
    if unclassified and not reasons:
        shown = ", ".join(unclassified[:5]) + (" ..." if len(unclassified) > 5 else "")
        reasons.append(f"unclassified non-safe path(s): {shown}")

    # 3. Blast radius: risk-bearing files only (scoped + unclassified, not docs).
    risky = [f for f in changed if _scoped_unit(f, scope_roots) or f in unclassified]
    if len(risky) > max_files and not reasons:
        reasons.append(f"{len(risky)} risk-bearing files (threshold {max_files})")

    return Verdict("HIGH" if reasons else "LOW", reasons or ["single-unit / docs-tooling scope, no protected paths"])


def _scoped_unit(path: str, scope_roots: list[str]) -> str | None:
    """The 2nd path segment under a scope root (e.g. services/<name>/...), else None."""
    for root in scope_roots:
        prefix = root.split("*")[0].strip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            parts = path.split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return None


def _diff_has(diff: str, pattern: str, sign: str) -> bool:
    return bool(re.search(rf"^\{sign}.*(?:{pattern})", diff, re.MULTILINE))


# ── git-backed entry point ───────────────────────────────────────────────────
def _git(cfg: Config, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cfg.repo_root, capture_output=True, text=True, check=True).stdout


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    cfg = load_config()
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    if not base or not head:
        raise SystemExit("change-review classifier requires BASE_SHA and HEAD_SHA.")
    rng = f"{base}...{head}"  # 3-dot = changes on the PR branch since the merge-base
    changed = [ln.strip() for ln in _git(cfg, "diff", "--name-only", rng).splitlines() if ln.strip()]

    def diff_for(path: str) -> str:
        try:
            return _git(cfg, "diff", rng, "--", path)
        except subprocess.CalledProcessError:
            return ""

    v = classify(cfg, changed, diff_for)
    print(f"Changed files ({len(changed)}):")
    for f in changed:
        print(f"  {f}")
    print(f"\nRISK LEVEL: {v.level}")
    for r in v.reasons:
        print(f"  - {r}")
    if v.level == "HIGH":
        print("\nACTION: a NON-AUTHOR must add the 'release-approved' label at the current commit.")
        return 1
    print("\nAuto-approved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
