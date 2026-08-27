#!/usr/bin/env python3
"""Render the agent fleet from a single source.

The methodology review's finding: ``.claude/agents/*.md`` (what the Agent tool
runs) and ``docs/agents/*_AGENT.md`` (human-readable rules) were TWO copies of
the same agent facts and drifted -- e.g. the two files gave a service two
different port numbers, and one named a logging library the project's own
conventions file had already replaced. Plus every agent re-pasted the same
ledger boilerplate and a stale "check AGENT_REPORTS.md" instruction.

The fix (a config source plus rendered outputs): ONE source of truth per agent, with
the shared, drift-prone scaffolding owned by the harness:

  .context/agents/<name>.md          # SOURCE: frontmatter (name/role/description/
                                     #   tools[/docs_path]) + the project body
  templates/agents/_scaffold.md.tmpl # SHARED: ledger section + "query the ledger,
                                     #   not AGENT_REPORTS" + conventions ({{tokens}})
        |
        v   render_agents.py (this)
  .claude/agents/<name>.md           # RENDERED: frontmatter + body + scaffold
  docs/agents/<NAME>_AGENT.md        # RENDERED pointer (no independent facts -> no drift)

``--check`` re-renders in memory and reports any file out of sync (a CI gate, so
a hand-edit of a rendered file turns the build red instead of silently drifting).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, load_config  # noqa: E402

_AGENTS_TMPL = Path(__file__).resolve().parents[1] / "templates" / "agents"
_SCAFFOLD = _AGENTS_TMPL / "_scaffold.md.tmpl"
_SUPPORT_DIR = _AGENTS_TMPL / "support"  # generic support-agent body STARTERS
_FM_KEYS = ("name", "description", "tools", "model")  # frontmatter the Agent tool consumes (role is meta)


def _methodology(cfg: Config) -> dict:
    return cfg.raw.get("methodology", {}) or {}


def support_tokens(cfg: Config) -> dict[str, str]:
    """Project-grounding {{tokens}} the generic support-agent STARTERS reference
    (substituted once, at --seed time). Everything else in a starter is generic
    prose -- so the harness ships a useful support squad without baking in any
    one project's stack (engineer agents stay project-authored)."""
    m = _methodology(cfg)
    raw = cfg.raw
    proj = raw.get("project", {}) or {}
    langs = raw.get("languages", []) or []
    primary = langs[0] if langs else {}
    lang_ids = [str(s.get("id", "")) for s in langs if s.get("id")]
    return {
        "PROJECT_NAME": str(proj.get("name") or "the project"),
        "PROJECT_DESC": str(proj.get("description") or ""),
        "LANGUAGES": ", ".join(lang_ids) or "the project's languages",
        "CODE_ROOTS": ", ".join(raw.get("code_roots", []) or []) or "the code roots",
        "PRD_DIR": m.get("prd_dir", "docs/prd"),
        "CTX_CMD": m.get("ctx_cmd", "python tools/harness/context-harness/ctx/ctx.py"),
        "LINT_CMD": str(primary.get("lint") or "the configured linter"),
        "FORMAT_CMD": str(primary.get("format") or "the configured formatter"),
        "TEST_CMD": str(primary.get("test") or "the configured test command"),
    }


def seed_support_agents(cfg: Config, check_only: bool = False) -> list[str]:
    """Copy the generic support-agent STARTERS into the project's source dir
    (.context/agents/<name>.md), substituting support_tokens. IDEMPOTENT and
    non-destructive: a starter is skipped if that agent source already exists, so
    a project's own richer agents are never overwritten. Returns the seeded
    (or, in check mode, would-seed) paths."""
    if not _SUPPORT_DIR.is_dir():
        return []
    src_dir = cfg.repo_root / _methodology(cfg).get("agents_src", ".context/agents")
    tokens = support_tokens(cfg)
    out: list[str] = []
    for tmpl in sorted(_SUPPORT_DIR.glob("*.md.tmpl")):
        name = tmpl.name[: -len(".md.tmpl")]
        dest = src_dir / f"{name}.md"
        if dest.exists():
            continue  # never clobber a project's own agent source
        rel = dest.relative_to(cfg.repo_root).as_posix()
        out.append(rel)
        if not check_only:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_substitute(tmpl.read_text(encoding="utf-8"), tokens), encoding="utf-8")
    return out


def render_tokens(cfg: Config, agent: dict) -> dict[str, str]:
    """The {{tokens}} the scaffold may reference: config-derived + per-agent."""
    m = _methodology(cfg)
    return {
        "AGENT_NAME": agent.get("name", "?"),
        "AGENT_ROLE": agent.get("role", "support"),
        "PRD_DIR": m.get("prd_dir", "docs/prd"),
        "CTX_CMD": m.get("ctx_cmd", "python tools/harness/context-harness/ctx/ctx.py"),
        "RECORDS_DIR": cfg.ledger.records_dir,
        "LEDGER_CONTRACT": m.get("ledger_contract", "docs/agents/CONTEXT_LEDGER.md"),
    }


def _substitute(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def parse_source(text: str) -> tuple[dict, str]:
    """Split a source agent file into (frontmatter dict, body str).

    Frontmatter is parsed line-by-line on the FIRST ``:`` -- agent descriptions
    routinely contain a colon (``Use when: ...``), which strict YAML rejects but
    the Agent tool tolerates. Values are plain strings (``tools`` is a comma
    list, not a YAML sequence), so a tiny key/value split is correct and robust."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            fm: dict = {}
            for line in parts[1].strip().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                fm[key.strip()] = value.strip()
            return fm, parts[2].lstrip("\n")
    return {}, text


def render_agent(cfg: Config, scaffold: str, fm: dict, body: str) -> str:
    """frontmatter (Agent-tool keys only) + body + rendered shared scaffold."""
    lines = ["---"]
    for k in _FM_KEYS:
        if fm.get(k) is not None:
            lines.append(f"{k}: {fm[k]}")
    lines.append("---\n")
    head = "\n".join(lines)
    scaffold_rendered = _substitute(scaffold, render_tokens(cfg, fm))
    return head + "\n" + body.rstrip() + "\n" + scaffold_rendered


def render_pointer(cfg: Config, fm: dict, src_rel: str, claude_rel: str) -> str:
    name = fm.get("name", "?")
    title = name.replace("-", " ").title()
    return (
        f"# {title} Agent\n\n"
        f"> **Generated pointer — do not edit.** This agent has a single source of truth; "
        f"editing here would re-introduce the drift this file used to cause.\n\n"
        f"- **Definition + project body:** `{src_rel}`\n"
        f"- **Shared scaffold (ledger, conventions):** "
        f"`tools/harness/methodology-harness/templates/agents/_scaffold.md.tmpl`\n"
        f"- **Rendered operational agent:** `{claude_rel}`\n\n"
        f"Re-render after editing the source:\n\n"
        f"```\npython tools/harness/methodology-harness/scripts/render_agents.py\n```\n"
    )


def render_all(cfg: Config, check_only: bool = False) -> list[str]:
    """Render every .context/agents/<name>.md. Returns dest paths written (or, in
    check mode, those OUT OF SYNC)."""
    scaffold = _SCAFFOLD.read_text(encoding="utf-8")
    src_dir = cfg.repo_root / _methodology(cfg).get("agents_src", ".context/agents")
    out: list[str] = []
    if not src_dir.is_dir():
        return out

    for src in sorted(src_dir.glob("*.md")):
        fm, body = parse_source(src.read_text(encoding="utf-8"))
        name = fm.get("name") or src.stem
        fm.setdefault("name", name)
        src_rel = src.relative_to(cfg.repo_root).as_posix()
        claude_rel = f".claude/agents/{name}.md"

        renders = [(claude_rel, render_agent(cfg, scaffold, fm, body))]
        if fm.get("docs_path"):
            renders.append((fm["docs_path"], render_pointer(cfg, fm, src_rel, claude_rel)))

        for dest_rel, content in renders:
            dest = cfg.repo_root / dest_rel
            if check_only:
                current = dest.read_text(encoding="utf-8") if dest.is_file() else ""
                if current != content:
                    out.append(dest_rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            out.append(dest_rel)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render .claude/agents + docs pointers from .context/agents.")
    ap.add_argument("--check", action="store_true", help="verify rendered files match the source; do not write")
    ap.add_argument(
        "--seed",
        action="store_true",
        help="seed generic support-agent STARTERS into .context/agents (idempotent; never overwrites), then render",
    )
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.seed:
        seeded = seed_support_agents(cfg)
        if seeded:
            print(f"render-agents --seed: seeded {len(seeded)} support agent starter(s) (existing untouched):")
            for s in seeded:
                print(f"  {s}")
            print("  Edit them for your project, then re-run render_agents.py.")
        else:
            print("render-agents --seed: no starters to add (every support agent already has a source).")

    touched = render_all(cfg, check_only=args.check)

    if args.check:
        if touched:
            print(f"render-agents --check: {len(touched)} file(s) OUT OF SYNC with the source:")
            for t in touched:
                print(f"  {t}")
            print("Re-run: python tools/harness/methodology-harness/scripts/render_agents.py")
            return 1
        print("render-agents --check: all rendered agents are in sync with .context/agents.")
        return 0

    print(f"render-agents: rendered {len(touched)} file(s) from .context/agents.")
    for t in touched:
        print(f"  {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
