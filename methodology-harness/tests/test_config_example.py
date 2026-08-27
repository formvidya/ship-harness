"""Guard that config.example.yml stays a VALID, COMPLETE fill-once template.

A new project copies this file, so it must parse and carry every section the
harness drives -- otherwise an adopter silently misses a gate. This catches the
exact rot that happened (the example lagged the live config on security_sca /
security_llm / the CI dependency graph / methodology).
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

from pathlib import Path

import yaml

_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yml"


def _cfg():
    return yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))


def test_example_parses():
    assert isinstance(_cfg(), dict)


def test_example_has_every_gate():
    gates = _cfg().get("gates", {})
    assert set(gates) >= {
        "ci",
        "code_quality",
        "security",
        "security_sca",
        "security_llm",
        "version_check",
        "change_review",
    }


def test_example_has_selective_ci_graph():
    ci = _cfg().get("ci", {})
    assert "shared_groups" in ci and "full_matrix_on" in ci
    svc = (_cfg().get("services") or [{}])[0]
    assert "ci_key" in svc and "depends_on" in svc  # the graph fields a service needs


def test_example_has_methodology_and_security_pipeline_shape():
    cfg = _cfg()
    assert {"agents_src", "prd_dir", "ctx_cmd"} <= set(cfg.get("methodology", {}))
    # security_llm ships ADVISORY by default (required:false) in the template.
    assert cfg["gates"]["security_llm"]["required"] is False
    assert "trigger_paths" in cfg["gates"]["security_llm"]["scope"]
