#!/usr/bin/env python3
"""Generic SCA (dependency-CVE) gate -- security pipeline Layer 1.

The Semgrep gate scans YOUR code (SAST); this scans your DEPENDENCY TREE for
known CVEs (the ``fastapi-0.137`` class of break). Runs ``osv-scanner`` -- one
pinned binary that understands many ecosystems (PyPI / npm / pub / ...) -- and
BLOCKS when a vulnerability at or above a configured severity is present. It is
the third deterministic line the pipeline design called for: ``p/secrets`` and
``p/security-audit`` are SAST rulesets, not SCA, and Dependabot only raises
async PRs, never a blocking check.

Config-driven (``.context/config.yml``), nothing hard-coded::

    gates:
      security_sca:
        enabled: true
        required: true
        check_name: "Security / sca (dependency CVEs)"
        engine: osv-scanner
        scan_root: "."
        fail_on: [high, critical]   # severities that block; lower ones advise
        block_unknown: true         # a vuln with no severity data still blocks
        manifests:                  # a scan runs only when one of these changes
          - "**/requirements*.txt"
          - "**/package-lock.json"
          - "**/pubspec.lock"
          - "**/poetry.lock"
          - "**/pyproject.toml"
        sarif_upload: true

Manifest-gated: the job ALWAYS runs (so the required check always reports) but
only SCANS when a dependency manifest changed in ``BASE...HEAD`` -- an unrelated
PR passes instantly. Newly-disclosed CVEs on unchanged deps are a scheduled-scan
concern, not a per-PR block.

Severity comes from osv-scanner's own ``groups[].max_severity`` (a CVSS base
score it computes) -> we never re-implement CVSS.

Override: ``[skip-security-sca]`` in any commit message in the range (logged).
"""

from __future__ import annotations

import argparse
import json
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
from config import Config, _glob_match, load_config  # noqa: E402

SKIP_MARKER = "[skip-security-sca]"
_DEFAULT_MANIFESTS = [
    "**/requirements*.txt",
    "**/package-lock.json",
    "**/pubspec.lock",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/go.sum",
    "**/Cargo.lock",
    "**/pyproject.toml",
]
_DEFAULT_FAIL_ON = ["high", "critical"]
# CVSS v3 base-score bands (FIRST.org). osv-scanner reports the float; we label.
_SEVERITY_ORDER = ["none", "low", "medium", "high", "critical"]


def sca_cfg(cfg: Config) -> dict:
    return (cfg.raw.get("gates", {}) or {}).get("security_sca", {}) or {}


def build_scan_argv(cfg: Config, output_path: str, fmt: str = "json") -> list[str]:
    """The osv-scanner command. Deterministic + unit-tested; the engine name and
    scan root come from config so a project can swap osv-scanner for another
    tool with the same CLI shape."""
    sca = sca_cfg(cfg)
    engine = sca.get("engine") or "osv-scanner"
    root = sca.get("scan_root") or "."
    return [engine, "--format", fmt, "--output", output_path, "--recursive", root]


def severity_label(score: float | None) -> str:
    """CVSS v3 base score -> band. None/0 -> 'none' (osv emits '' when a vuln
    carries no CVSS vector)."""
    if score is None:
        return "none"
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _to_score(raw) -> float | None:
    try:
        s = float(raw)
    except (TypeError, ValueError):
        return None
    return s if s > 0 else None


def parse_report(report_json: str) -> list[dict]:
    """Flatten osv-scanner JSON into one entry per vulnerability group:
    {ids, package, version, ecosystem, source, score, severity}."""
    try:
        data = json.loads(report_json or "{}")
    except json.JSONDecodeError:
        return []
    findings: list[dict] = []
    for result in data.get("results", []) or []:
        source = (result.get("source", {}) or {}).get("path", "")
        for pkg in result.get("packages", []) or []:
            info = pkg.get("package", {}) or {}
            groups = pkg.get("groups") or []
            # Fall back to one synthetic group if osv didn't group (older output).
            if not groups:
                vulns = pkg.get("vulnerabilities", []) or []
                groups = [{"ids": [v.get("id") for v in vulns]}] if vulns else []
            for grp in groups:
                score = _to_score(grp.get("max_severity"))
                findings.append(
                    {
                        "ids": [i for i in (grp.get("ids") or []) if i],
                        "package": info.get("name", ""),
                        "version": info.get("version", ""),
                        "ecosystem": info.get("ecosystem", ""),
                        "source": source,
                        "score": score,
                        "severity": severity_label(score),
                    }
                )
    return findings


def blocking_findings(findings: list[dict], fail_on: list[str], block_unknown: bool) -> list[dict]:
    """Findings that should fail the gate: severity in fail_on, or unknown
    ('none' band = no CVSS data) when block_unknown is set."""
    wanted = {s.lower() for s in fail_on}
    out = []
    for f in findings:
        sev = f["severity"]
        if sev in wanted or (sev == "none" and block_unknown):
            out.append(f)
    return out


# ── changed-manifest gate ────────────────────────────────────────────────────
def _changed_files(cfg: Config, base: str | None, head: str | None) -> list[str] | None:
    """Files changed in BASE...HEAD, or None when we can't tell (-> scan anyway)."""
    if not (base and head) or set(base) == {"0"}:
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
            cwd=cfg.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def manifests_changed(cfg: Config, base: str | None, head: str | None) -> bool:
    """True if a dependency manifest changed (or if we can't compute the diff,
    in which case we scan -- the safe direction)."""
    changed = _changed_files(cfg, base, head)
    if changed is None:
        return True
    globs = sca_cfg(cfg).get("manifests") or _DEFAULT_MANIFESTS
    return any(_glob_match(g, f) for g in globs for f in changed)


def _emit_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    print(f"{key}={value}")


def should_scan(cfg: Config, base, head) -> bool:
    """Whether a scan is warranted: a manifest changed AND no skip marker. The
    workflow runs this CHEAP precheck (python + git, no osv-scanner) first, so
    the expensive osv-scanner install only happens when a scan will actually
    run -- an unrelated PR pays nothing."""
    if SKIP_MARKER in _commit_messages(cfg, base, head):
        return False
    return manifests_changed(cfg, base, head)


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


def _report(blocking: list[dict], advisory: list[dict]) -> None:
    if blocking:
        print(f"::error::SCA found {len(blocking)} blocking dependency vulnerabilit(ies):")
        for f in blocking:
            ids = ", ".join(f["ids"]) or "?"
            print(f"  [{f['severity'].upper()}] {f['package']} {f['version']} ({f['source']}) -> {ids}")
    if advisory:
        print(f"SCA advisory ({len(advisory)} below fail_on threshold):")
        for f in advisory:
            ids = ", ".join(f["ids"]) or "?"
            print(f"  [{f['severity']}] {f['package']} {f['version']} -> {ids}")
    if not blocking and not advisory:
        print("SCA: no known-CVE dependencies found.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SCA dependency-CVE gate (osv-scanner).")
    ap.add_argument("--sarif", help="also write SARIF here (for the Security tab)")
    ap.add_argument("--print-argv", action="store_true", help="print the scan command and exit")
    ap.add_argument(
        "--should-scan",
        action="store_true",
        help="emit scan=true|false to $GITHUB_OUTPUT (cheap precheck; no osv-scanner) and exit",
    )
    args = ap.parse_args(argv)

    cfg = load_config()
    sca = sca_cfg(cfg)
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")

    if args.print_argv:
        print(" ".join(build_scan_argv(cfg, "osv.json")))
        return 0

    if args.should_scan:
        _emit_output("scan", "true" if should_scan(cfg, base, head) else "false")
        return 0

    if SKIP_MARKER in _commit_messages(cfg, base, head):
        print(f"security-sca: SKIPPED via {SKIP_MARKER} (logged)")
        return 0

    if not manifests_changed(cfg, base, head):
        print("security-sca: no dependency-manifest changes in this PR -- skipping scan (pass).")
        return 0

    out_json = "osv-report.json"
    cmd = build_scan_argv(cfg, out_json, "json")
    print("security-sca: " + " ".join(cmd))
    try:
        # osv-scanner exits 1 when vulns are found; we make our OWN decision from
        # the report against fail_on, so a non-zero exit here is expected/ignored.
        subprocess.run(cmd, cwd=cfg.repo_root, capture_output=True, text=True)
    except FileNotFoundError:
        print("::error::osv-scanner is not installed. The workflow must provide it (pinned).")
        return 1

    report_path = cfg.repo_root / out_json
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else "{}"
    findings = parse_report(report)
    fail_on = sca.get("fail_on") or _DEFAULT_FAIL_ON
    block_unknown = sca.get("block_unknown", True)
    blocking = blocking_findings(findings, fail_on, block_unknown)
    advisory = [f for f in findings if f not in blocking]
    _report(blocking, advisory)

    if args.sarif and sca.get("sarif_upload"):
        # Second, cheap pass purely for the Security tab; never affects the gate.
        subprocess.run(build_scan_argv(cfg, args.sarif, "sarif"), cwd=cfg.repo_root, capture_output=True, text=True)

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
