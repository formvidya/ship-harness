"""Unit tests for enforcement_check.expected_checks.

Pure logic — no network / gh calls. Verifies the config -> required-check-set
derivation, which is the heart of the 'enforcement is verifiable' guarantee.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import enforcement_check as ec  # noqa: E402


def _cfg(gates, ci_check="Context Check / per-change record present & valid"):
    return SimpleNamespace(raw={"gates": gates}, ci_check_name=ci_check)


def test_required_gates_plus_ci_and_context():
    cfg = _cfg(
        {
            "ci": {"required_check": "CI Success"},
            "code_quality": {"required": True, "check_name": "Code Quality"},
            "security": {"required": True, "check_name": "Security"},
            "version_check": {"required": True, "check_name": "Version Check"},
            "change_review": {"required": True, "check_name": "change-review"},
        }
    )
    got = ec.expected_checks(cfg)
    assert got == sorted(
        [
            "CI Success",
            "Code Quality",
            "Security",
            "Version Check",
            "change-review",
            "Context Check / per-change record present & valid",
        ]
    )


def test_non_required_gate_is_excluded():
    cfg = _cfg(
        {
            "code_quality": {"required": True, "check_name": "Code Quality"},
            "semantic_overlap": {"required": False, "check_name": "Semantic Overlap"},
        }
    )
    got = ec.expected_checks(cfg)
    assert "Semantic Overlap" not in got
    assert "Code Quality" in got


def test_default_check_name_when_unnamed():
    cfg = _cfg({"code_quality": {"required": True}})
    got = ec.expected_checks(cfg)
    assert ec._DEFAULT_CHECK_NAMES["code_quality"] in got


def test_no_gates_still_requires_context_gate():
    cfg = _cfg({})
    assert ec.expected_checks(cfg) == ["Context Check / per-change record present & valid"]


def test_ledger_disabled_no_context_check():
    cfg = _cfg({"ci": {"required_check": "CI Success"}}, ci_check="")
    assert ec.expected_checks(cfg) == ["CI Success"]
