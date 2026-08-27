"""Unit tests for the agent-fleet renderer.

Locks the single-source render: frontmatter is reduced to the Agent-tool keys
(role is dropped), the shared scaffold's {{tokens}} are substituted from config +
per-agent, docs pointers carry no independent facts, and --check detects drift.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_agents as ra  # noqa: E402


def _cfg(repo_root, methodology=None):
    return SimpleNamespace(
        raw={"methodology": methodology or {}},
        ledger=SimpleNamespace(records_dir="docs/context/records"),
        repo_root=Path(repo_root),
    )


_SCAFFOLD = "## Ledger\n{{CTX_CMD}} decide --agent {{AGENT_NAME}}\nrecords: {{RECORDS_DIR}} prd: {{PRD_DIR}}\n"


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_parse_source_splits_frontmatter_and_body():
    fm, body = ra.parse_source("---\nname: backend\nrole: engineer\n---\n\n# Body\ncontent\n")
    assert fm["name"] == "backend" and fm["role"] == "engineer"
    assert body.startswith("# Body")


def test_parse_source_no_frontmatter():
    fm, body = ra.parse_source("# just a body")
    assert fm == {} and body == "# just a body"


def test_substitute_replaces_known_tokens():
    out = ra._substitute("a {{X}} b {{Y}}", {"X": "1", "Y": "2"})
    assert out == "a 1 b 2"


def test_render_tokens_from_config_and_agent():
    t = ra.render_tokens(_cfg("."), {"name": "backend", "role": "engineer"})
    assert t["AGENT_NAME"] == "backend" and t["AGENT_ROLE"] == "engineer"
    assert t["RECORDS_DIR"] == "docs/context/records" and t["PRD_DIR"] == "docs/prd"


def test_render_tokens_honor_methodology_overrides():
    t = ra.render_tokens(_cfg(".", {"prd_dir": "specs", "ctx_cmd": "ctx"}), {"name": "x"})
    assert t["PRD_DIR"] == "specs" and t["CTX_CMD"] == "ctx"


# ── render_agent: frontmatter reduced, body kept, scaffold substituted ───────
def test_render_agent_drops_role_keeps_tool_keys_and_substitutes():
    fm = {"name": "backend", "role": "engineer", "description": "Backend eng", "tools": "Read, Edit"}
    out = ra.render_agent(_cfg("."), _SCAFFOLD, fm, "# Backend\nbody here")
    assert "name: backend" in out and "description: Backend eng" in out and "tools: Read, Edit" in out
    assert "role:" not in out  # methodology-only key never reaches the .claude frontmatter
    assert "# Backend\nbody here" in out
    assert "decide --agent backend" in out and "{{" not in out  # all tokens resolved


def test_render_pointer_has_no_facts_only_sources():
    p = ra.render_pointer(
        _cfg("."), {"name": "sre-devops"}, ".context/agents/sre-devops.md", ".claude/agents/sre-devops.md"
    )
    assert "Generated pointer" in p and "do not edit" in p.lower()
    assert ".context/agents/sre-devops.md" in p and ".claude/agents/sre-devops.md" in p


# ── render_all + --check drift guard (real fs) ──────────────────────────────
def _seed(tmp_path, monkeypatch, src_text):
    monkeypatch.setattr(ra, "_SCAFFOLD", tmp_path / "scaffold.tmpl")
    (tmp_path / "scaffold.tmpl").write_text(_SCAFFOLD, encoding="utf-8")
    src = tmp_path / ".context" / "agents"
    src.mkdir(parents=True)
    (src / "backend.md").write_text(src_text, encoding="utf-8")
    return _cfg(tmp_path)


def test_render_all_writes_claude_and_docs_pointer(tmp_path, monkeypatch):
    cfg = _seed(
        tmp_path,
        monkeypatch,
        "---\nname: backend\nrole: engineer\ndescription: d\ntools: Read\ndocs_path: docs/agents/BACKEND_AGENT.md\n---\n\nBODY\n",
    )
    written = ra.render_all(cfg)
    assert ".claude/agents/backend.md" in written
    assert "docs/agents/BACKEND_AGENT.md" in written
    claude = (tmp_path / ".claude/agents/backend.md").read_text(encoding="utf-8")
    assert "BODY" in claude and "decide --agent backend" in claude
    docs = (tmp_path / "docs/agents/BACKEND_AGENT.md").read_text(encoding="utf-8")
    assert "Generated pointer" in docs and "BODY" not in docs  # pointer carries no body/facts


def _seed_cfg(tmp_path):
    return SimpleNamespace(
        raw={
            "project": {"name": "Acme", "description": "Acme widgets"},
            "languages": [{"id": "python", "test": "pytest", "lint": "ruff check", "format": "ruff format --check"}],
            "code_roots": ["src/**"],
            "methodology": {"agents_src": ".context/agents"},
        },
        ledger=SimpleNamespace(records_dir="docs/context/records"),
        repo_root=tmp_path,
    )


def test_support_tokens_from_config():
    t = ra.support_tokens(_seed_cfg(Path(".")))
    assert t["PROJECT_NAME"] == "Acme" and t["LANGUAGES"] == "python"
    assert t["TEST_CMD"] == "pytest" and t["CODE_ROOTS"] == "src/**"


def test_seed_support_agents_resolves_tokens_and_is_idempotent(tmp_path):
    # Uses the REAL support templates that ship with the harness.
    cfg = _seed_cfg(tmp_path)
    seeded = ra.seed_support_agents(cfg)
    assert len(seeded) >= 10  # the support squad (testing/security/change-manager/...)
    body = (tmp_path / ".context/agents/security.md").read_text(encoding="utf-8")
    assert "Acme" in body and "{{" not in body  # project tokens substituted, none left
    # a project's own agent source is NEVER overwritten
    (tmp_path / ".context/agents/testing.md").write_text("MINE", encoding="utf-8")
    again = ra.seed_support_agents(cfg)
    assert "testing.md" not in " ".join(s.split("/")[-1] for s in again)  # skipped (exists)
    assert (tmp_path / ".context/agents/testing.md").read_text(encoding="utf-8") == "MINE"


def test_seeded_support_agents_then_render(tmp_path):
    cfg = _seed_cfg(tmp_path)
    ra.seed_support_agents(cfg)
    written = ra.render_all(cfg)
    rendered = (tmp_path / ".claude/agents/change-manager.md").read_text(encoding="utf-8")
    assert "## Context Ledger" in rendered  # the shared scaffold was appended
    assert any(".claude/agents/" in w for w in written)


def test_check_mode_detects_drift_and_is_clean_after_render(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, "---\nname: backend\ndescription: d\ntools: Read\n---\n\nBODY\n")
    assert ra.render_all(cfg, check_only=True) == [".claude/agents/backend.md"]  # nothing rendered yet -> out of sync
    ra.render_all(cfg)  # render
    assert ra.render_all(cfg, check_only=True) == []  # now in sync
    # hand-edit the rendered file -> drift detected
    (tmp_path / ".claude/agents/backend.md").write_text("tampered", encoding="utf-8")
    assert ra.render_all(cfg, check_only=True) == [".claude/agents/backend.md"]
