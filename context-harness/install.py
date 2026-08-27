#!/usr/bin/env python3
"""Turnkey installer for the context-harness.

Drops the harness into any repo. Copy this folder (``tools/harness/context-harness/``)
into a target repo, then from the repo root run:

    python tools/harness/context-harness/install.py

It scaffolds everything a project needs and is **idempotent** — re-running it
never clobbers your filled-in config and only adds what is missing:

  1. ``.context/config.yml``         from the template (skipped if present)
  2. ``.github/workflows/context-check.yml``   the required CI gate
  3. ``.claude/hooks/pre-dev-context.sh``      the query-before-dev nudge
  4. ``.claude/settings.json``       merges in the Edit|Write PreToolUse hook
  5. ``.github/pull_request_template.md``      the context checklist (if absent)
  6. ``.gitignore``                  ignores ephemeral markers + the index dir
  7. ``ctx bootstrap``               renders the context-keeper agent + ledger
                                      contract from your config

Then it prints the two manual steps it cannot do for you (make Context Check a
required status check; optionally install the pre-commit hook).

Flags:
  --target DIR   install into DIR instead of the detected repo root
  --force        overwrite .context/config.yml from the template
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent

_GITIGNORE_BLOCK = [
    "# context-harness ephemeral markers + regenerable index",
    ".claude/context-recorded-*",
    ".claude/context-consulted-*",
    "docs/context/index/",
]


def _find_repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    return start


def _copy_if_missing(src: Path, dst: Path, force: bool = False) -> str:
    if dst.exists() and not force:
        return "skip (exists)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return "wrote"


def _ensure_gitignore(repo: Path) -> str:
    gi = repo / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if ".claude/context-consulted-*" in existing:
        return "skip (present)"
    block = ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(_GITIGNORE_BLOCK) + "\n"
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return "appended"


def _merge_settings(repo: Path) -> str:
    """Add the Edit|Write -> pre-dev-context.sh PreToolUse hook, preserving any
    existing settings.json content."""
    path = repo / ".claude" / "settings.json"
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "skip (unparseable settings.json — wire the hook manually)"
    hooks = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    cmd = ".claude/hooks/pre-dev-context.sh $TOOL_INPUT"
    already = any(h.get("command") == cmd for grp in hooks for h in grp.get("hooks", []))
    if already:
        return "skip (hook present)"
    hooks.append({"matcher": "Edit|Write", "hooks": [{"type": "command", "command": cmd}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "merged hook"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="repo root to install into (default: auto-detect)")
    ap.add_argument("--force", action="store_true", help="overwrite .context/config.yml")
    args = ap.parse_args(argv)

    repo = Path(args.target).resolve() if args.target else _find_repo_root(Path.cwd())
    # Where does the harness live relative to the repo? (for the rendered paths)
    try:
        harness_rel = HARNESS.relative_to(repo).as_posix()
    except ValueError:
        harness_rel = "tools/context-harness"

    print(f"context-harness installer -> {repo}")
    steps: list[tuple[str, str]] = []

    steps.append(
        (
            ".context/config.yml",
            _copy_if_missing(
                HARNESS / "templates" / "config.example.yml", repo / ".context" / "config.yml", args.force
            ),
        )
    )
    steps.append(
        (
            ".github/workflows/context-check.yml",
            _copy_if_missing(
                HARNESS / "templates" / "workflows" / "context-check.yml",
                repo / ".github" / "workflows" / "context-check.yml",
            ),
        )
    )
    hook_dst = repo / ".claude" / "hooks" / "pre-dev-context.sh"
    steps.append(
        (
            ".claude/hooks/pre-dev-context.sh",
            _copy_if_missing(HARNESS / "templates" / "hooks" / "pre-dev-context.sh", hook_dst),
        )
    )
    steps.append((".claude/settings.json", _merge_settings(repo)))
    pr_tmpl = HARNESS / "templates" / "pull_request_template.md"
    if pr_tmpl.is_file():
        steps.append(
            (
                ".github/pull_request_template.md",
                _copy_if_missing(pr_tmpl, repo / ".github" / "pull_request_template.md"),
            )
        )
    steps.append((".gitignore", _ensure_gitignore(repo)))

    for name, status in steps:
        print(f"  [{status}] {name}")

    # Render the agent + ledger contract from the (now-present) config.
    print("  [run] ctx bootstrap")
    rc = subprocess.run(
        [sys.executable, str(HARNESS / "ctx" / "ctx.py"), "bootstrap"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    for line in (rc.stdout or rc.stderr).splitlines():
        print(f"        {line}")

    print("\nDone. Two manual steps remain (the installer cannot do these):")
    print("  1. pip install pyyaml   (the only runtime dependency)")
    print("  2. In branch protection, add the required status check:")
    print("       'Context Check / per-change record present & valid'")
    print("\n  Edit .context/config.yml to match your project, then re-run:")
    print(f"       python {harness_rel}/ctx/ctx.py bootstrap")
    print("  Optional: add the pre-commit hook (see tools/harness/context-harness/docs/INSTALL.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
