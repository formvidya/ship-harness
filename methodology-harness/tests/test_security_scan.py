"""Unit tests for the security-scan command builder.

The gate is a thin builder around the semgrep CLI; these lock the argv assembly
(diff baseline, config-driven rulesets incl. the p/secrets default, severities,
SARIF) without running semgrep.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_security_scan as ss  # noqa: E402


def _cfg(security=None):
    gates = {"security": security} if security is not None else {}
    return SimpleNamespace(raw={"gates": gates}, repo_root=Path("."))


def test_defaults_include_p_secrets():
    argv = ss.build_semgrep_argv(_cfg(), base="abc", sarif_path=None)
    assert argv[:2] == ["semgrep", "scan"]
    assert "--error" in argv and "--metrics=off" in argv
    # p/secrets is a default (the review's improvement)
    assert "p/secrets" in argv
    assert "p/python" in argv and "p/owasp-top-ten" in argv


def test_diff_baseline_and_severities():
    argv = ss.build_semgrep_argv(_cfg(), base="BASESHA", sarif_path=None)
    i = argv.index("--baseline-commit")
    assert argv[i + 1] == "BASESHA"
    assert "ERROR" in argv and "WARNING" in argv


def test_config_overrides_rulesets():
    argv = ss.build_semgrep_argv(_cfg({"configs": ["p/python"]}), base=None, sarif_path=None)
    assert "p/python" in argv
    assert "p/owasp-top-ten" not in argv  # overridden, not appended
    assert "--baseline-commit" not in argv  # no base -> full scan


def test_sarif_output_when_path_given():
    argv = ss.build_semgrep_argv(_cfg(), base="abc", sarif_path="out.sarif")
    assert "--sarif" in argv
    j = argv.index("--output")
    assert argv[j + 1] == "out.sarif"


def test_no_sarif_when_path_none():
    argv = ss.build_semgrep_argv(_cfg(), base="abc", sarif_path=None)
    assert "--sarif" not in argv


def test_custom_severities():
    argv = ss.build_semgrep_argv(_cfg({"severities": ["ERROR"]}), base="abc", sarif_path=None)
    assert "ERROR" in argv and "WARNING" not in argv
