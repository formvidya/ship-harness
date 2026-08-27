"""Agent-template rendering for `ctx bootstrap`.

The harness ships generic agent templates with ``{{tokens}}``. Bootstrap
substitutes them from ``.context/config.yml`` and writes the result into the
repo's canonical agent paths. This is the "config-source + rendered" choice:
the config is the single source of truth; the rendered ``## Project Profile``
block in each agent cannot drift because re-running bootstrap regenerates it.

Substitution is a deliberately tiny ``{{KEY}}`` -> value replace (no Jinja, no
logic) so the render is deterministic and auditable.
"""

from __future__ import annotations

from config import Config

# Template -> destination, both repo-root relative. Generic in; project-specific
# path out. Add new agent templates here as later workstreams introduce them.
_TEMPLATE_MAP = {
    "tools/harness/context-harness/templates/agents/context-keeper.md": ".claude/agents/context-keeper.md",
    "tools/harness/context-harness/templates/agents/CONTEXT_LEDGER.md": "docs/agents/CONTEXT_LEDGER.md",
}

_TOKEN_OPEN = "{{"
_TOKEN_CLOSE = "}}"


def _render_context(cfg: Config) -> dict[str, str]:
    """The fixed token map. Everything a template may reference, from config."""
    risk_rows = "\n".join(
        f"| {level} | {pol.get('horizon', '?')} | {pol.get('substance_review', '?')} |"
        for level, pol in cfg.risk_policy.items()
    )
    role_rows = "\n".join(f"| {role} | {', '.join(agents)} |" for role, agents in cfg.role_bindings.items())
    return {
        "project.name": cfg.name,
        "project.slug": cfg.slug,
        "project.description": cfg.description,
        "project.languages": ", ".join(cfg.languages),
        "code_roots": ", ".join(cfg.code_roots),
        "exempt_globs": ", ".join(cfg.exempt_globs),
        "reference_architecture": cfg.reference_architecture or "(none configured)",
        "ledger.records_dir": cfg.ledger.records_dir,
        "ledger.registry": cfg.ledger.registry or "(none)",
        "ledger.open_loops": cfg.ledger.open_loops or "(none)",
        "ledger.digests_dir": cfg.ledger.digests_dir or "(none)",
        "topic_seeds": ", ".join(cfg.topic_seeds) if cfg.topic_seeds else "(none seeded yet)",
        "skip_marker": cfg.skip_marker,
        "risk_table": risk_rows,
        "role_table": role_rows,
        "historian_agent": ", ".join(cfg.role_bindings.get("historian", ("context-keeper",))),
    }


def render_text(template: str, cfg: Config) -> str:
    ctx = _render_context(cfg)
    out = template
    for key, value in ctx.items():
        out = out.replace(f"{_TOKEN_OPEN}{key}{_TOKEN_CLOSE}", value)
    return out


def render_agent_templates(cfg: Config, check_only: bool = False) -> list[str]:
    """Render every mapped template. Returns repo-relative dest paths that were
    written (or, in check mode, that are OUT OF SYNC with config)."""
    touched: list[str] = []
    for tmpl_rel, dest_rel in _TEMPLATE_MAP.items():
        tmpl = cfg.repo_root / tmpl_rel
        dest = cfg.repo_root / dest_rel
        if not tmpl.is_file():
            continue
        rendered = render_text(tmpl.read_text(encoding="utf-8"), cfg)
        if check_only:
            current = dest.read_text(encoding="utf-8") if dest.is_file() else ""
            if current != rendered:
                touched.append(dest_rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        touched.append(dest_rel)
    return touched
