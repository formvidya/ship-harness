"""Unit tests for the SCA dependency-CVE gate in the security pipeline.

Locks the osv-scanner argv builder, the CVSS-band labeling, the osv JSON parser
(incl. the no-groups fallback), the fail_on / block_unknown blocking decision,
and the changed-manifest gate.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_sca_scan as sca  # noqa: E402


def _cfg(security_sca=None):
    gates = {"security_sca": security_sca} if security_sca is not None else {}
    return SimpleNamespace(raw={"gates": gates}, repo_root=Path("."))


# ── argv builder ─────────────────────────────────────────────────────────────
def test_build_scan_argv_defaults():
    argv = sca.build_scan_argv(_cfg(), "osv.json")
    assert argv == ["osv-scanner", "--format", "json", "--output", "osv.json", "--recursive", "."]


def test_build_scan_argv_config_overrides():
    argv = sca.build_scan_argv(_cfg({"engine": "trivy", "scan_root": "services"}), "out.sarif", fmt="sarif")
    assert argv[0] == "trivy"
    assert argv[2] == "sarif"
    assert argv[-1] == "services"


# ── CVSS band labeling ───────────────────────────────────────────────────────
def test_severity_label_bands():
    assert sca.severity_label(9.8) == "critical"
    assert sca.severity_label(9.0) == "critical"
    assert sca.severity_label(7.5) == "high"
    assert sca.severity_label(7.0) == "high"
    assert sca.severity_label(5.0) == "medium"
    assert sca.severity_label(4.0) == "medium"
    assert sca.severity_label(3.9) == "low"
    assert sca.severity_label(0.0) == "none"
    assert sca.severity_label(None) == "none"


# ── osv JSON parsing ─────────────────────────────────────────────────────────
_REPORT = json.dumps(
    {
        "results": [
            {
                "source": {"path": "services/billing-service/requirements.txt", "type": "lockfile"},
                "packages": [
                    {
                        "package": {"name": "fastapi", "version": "0.137.0", "ecosystem": "PyPI"},
                        "vulnerabilities": [{"id": "GHSA-high"}],
                        "groups": [{"ids": ["GHSA-high"], "max_severity": "7.5"}],
                    },
                    {
                        "package": {"name": "lowpkg", "version": "1.0", "ecosystem": "PyPI"},
                        "groups": [{"ids": ["GHSA-low"], "max_severity": "3.1"}],
                    },
                    {
                        "package": {"name": "nosev", "version": "2.0", "ecosystem": "PyPI"},
                        "vulnerabilities": [{"id": "GHSA-nosev"}],
                        "groups": [{"ids": ["GHSA-nosev"], "max_severity": ""}],
                    },
                ],
            }
        ]
    }
)


def test_parse_report_extracts_severity_and_package():
    f = {x["package"]: x for x in sca.parse_report(_REPORT)}
    assert f["fastapi"]["severity"] == "high" and f["fastapi"]["score"] == 7.5
    assert f["fastapi"]["source"] == "services/billing-service/requirements.txt"
    assert f["lowpkg"]["severity"] == "low"
    assert f["nosev"]["severity"] == "none" and f["nosev"]["score"] is None
    assert f["nosev"]["ids"] == ["GHSA-nosev"]


def test_parse_report_no_groups_fallback():
    # Older osv output: vulnerabilities but no groups -> one synthetic group.
    rpt = json.dumps(
        {
            "results": [
                {
                    "source": {"path": "requirements.txt"},
                    "packages": [{"package": {"name": "x", "version": "1"}, "vulnerabilities": [{"id": "GHSA-z"}]}],
                }
            ]
        }
    )
    out = sca.parse_report(rpt)
    assert len(out) == 1 and out[0]["ids"] == ["GHSA-z"] and out[0]["severity"] == "none"


def test_parse_report_garbage_is_empty():
    assert sca.parse_report("not json") == []
    assert sca.parse_report("") == []


# ── blocking decision ────────────────────────────────────────────────────────
def test_blocking_respects_fail_on_and_block_unknown():
    findings = sca.parse_report(_REPORT)
    block = sca.blocking_findings(findings, ["high", "critical"], block_unknown=True)
    pkgs = {f["package"] for f in block}
    assert pkgs == {"fastapi", "nosev"}  # high blocks; unknown blocks; low does not


def test_block_unknown_false_drops_unscored():
    findings = sca.parse_report(_REPORT)
    block = sca.blocking_findings(findings, ["high", "critical"], block_unknown=False)
    assert {f["package"] for f in block} == {"fastapi"}  # nosev (no CVSS) no longer blocks


def test_fail_on_medium_includes_more():
    findings = sca.parse_report(_REPORT)
    block = sca.blocking_findings(findings, ["medium", "high", "critical"], block_unknown=False)
    # still only fastapi here (lowpkg is 'low', below medium)
    assert {f["package"] for f in block} == {"fastapi"}


# ── changed-manifest gate ────────────────────────────────────────────────────
def test_manifests_changed_true_on_lockfile(monkeypatch):
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["services/x/requirements.txt", "docs/y.md"])
    assert sca.manifests_changed(_cfg(), "a", "b") is True


def test_manifests_changed_false_on_docs_only(monkeypatch):
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["docs/y.md", "README.md"])
    assert sca.manifests_changed(_cfg(), "a", "b") is False


def test_manifests_changed_true_when_undeterminable(monkeypatch):
    # Can't compute the diff (push, shallow) -> scan anyway (safe direction).
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: None)
    assert sca.manifests_changed(_cfg(), None, None) is True


def test_manifests_changed_custom_globs(monkeypatch):
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["go.sum"])
    assert sca.manifests_changed(_cfg({"manifests": ["**/requirements*.txt"]}), "a", "b") is False
    assert sca.manifests_changed(_cfg({"manifests": ["**/go.sum"]}), "a", "b") is True


# ── precheck (gates the expensive osv-scanner install in the workflow) ───────
def test_should_scan_true_when_manifest_changed(monkeypatch):
    monkeypatch.setattr(sca, "_commit_messages", lambda c, b, h: "normal commit")
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["services/x/requirements.txt"])
    assert sca.should_scan(_cfg(), "a", "b") is True


def test_should_scan_false_on_docs_only(monkeypatch):
    monkeypatch.setattr(sca, "_commit_messages", lambda c, b, h: "docs")
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["README.md"])
    assert sca.should_scan(_cfg(), "a", "b") is False


def test_should_scan_false_on_skip_marker(monkeypatch):
    # Skip marker short-circuits even if a manifest changed.
    monkeypatch.setattr(sca, "_commit_messages", lambda c, b, h: "fix [skip-security-sca]")
    monkeypatch.setattr(sca, "_changed_files", lambda c, b, h: ["requirements.txt"])
    assert sca.should_scan(_cfg(), "a", "b") is False
