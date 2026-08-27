#!/usr/bin/env python3
"""Generic version-bump gate.

The portable version of the hand-rolled, repo-specific version-bump checker this
was extracted from. A PR that changes a versioned component's source must also
bump that component's version. Everything project-specific comes from
``.context/config.yml`` -- the checker hard-codes no path, no service name, no
version-file convention.

Improvement over the original (the review's "reader registry"): version sources
are pluggable, so one gate covers ``VERSION`` files AND ``pubspec.yaml`` (Flutter)
AND ``pyproject.toml`` / ``package.json`` -- not just ``services/**/VERSION``.

Config (``version_check.components``)::

    version_check:
      components:
        - {discover: glob,     root: services, version_file: VERSION, format: semver}
        - {discover: explicit, path: apps/mobile/pubspec.yaml, version_field: version, format: "semver+build"}
        - {discover: glob,     root: libs, version_file: pyproject.toml, version_field: "project.version"}
      changelog: {path: VERSIONS.md, require_entry: false}

Modes: CI (``BASE_SHA``/``HEAD_SHA`` env) and pre-commit (``--staged``). Override:
``[skip-version-check]`` in any commit message in the range (logged).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - environment dependent
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Reuse the context-harness config loader (glob dialect, code_roots, exempt).
_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, load_config  # noqa: E402

SKIP_MARKER = "[skip-version-check]"


@dataclass(frozen=True)
class Component:
    """One versioned unit: a directory whose source is governed by a version."""

    dir_rel: str  # repo-relative dir that this version governs
    version_path_rel: str  # repo-relative path to the version-bearing file
    version_field: str | None  # dotted key inside the file, or None for a plain VERSION
    fmt: str  # "semver" | "semver+build"


# ── version reading ──────────────────────────────────────────────────────────
def _read_version(cfg: Config, comp: Component) -> str | None:
    p = cfg.repo_root / comp.version_path_rel
    if not p.is_file():
        return None
    name = p.name.lower()
    text = p.read_text(encoding="utf-8")
    if comp.version_field is None or name == "version":
        return text.strip() or None
    if name.endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text) or {}
        return _dotted(data, comp.version_field)
    if name == "pyproject.toml":
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - py<3.11
            return None
        return _dotted(tomllib.loads(text), comp.version_field)
    if name.endswith(".json"):
        return _dotted(json.loads(text), comp.version_field)
    return text.strip() or None


def _dotted(data: dict, key: str):
    cur = data
    for k in key.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return str(cur) if cur is not None else None


def _parse(version: str | None, fmt: str) -> tuple | None:
    """Parse to a comparable tuple. Returns None if unparseable."""
    if not version:
        return None
    core = version.split("+", 1)
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", core[0].strip())
    if not m:
        return None
    semver = tuple(int(x) for x in m.groups())
    if fmt == "semver+build":
        build = 0
        if len(core) > 1:
            bm = re.match(r"(\d+)", core[1])
            build = int(bm.group(1)) if bm else 0
        return semver + (build,)
    return semver


# ── component discovery ──────────────────────────────────────────────────────
def discover_components(cfg: Config) -> list[Component]:
    vc = cfg.raw.get("version_check", {}) or {}
    specs = vc.get("components")
    comps: list[Component] = []
    if not specs:
        # Sensible default: every code_root's <root>/*/VERSION (the original behaviour).
        specs = [{"discover": "glob", "root": "services", "version_file": "VERSION", "format": "semver"}]
    for spec in specs:
        fmt = spec.get("format", "semver")
        field = spec.get("version_field")
        if spec.get("discover") == "explicit":
            path = spec["path"]
            comps.append(
                Component(
                    dir_rel=str(Path(path).parent.as_posix()),
                    version_path_rel=path,
                    version_field=field,
                    fmt=fmt,
                )
            )
            continue
        root = cfg.repo_root / spec.get("root", "")
        vfile = spec.get("version_file", "VERSION")
        if not root.is_dir():
            continue
        for vf in sorted(root.rglob(vfile)):
            d = vf.parent
            comps.append(
                Component(
                    dir_rel=d.relative_to(cfg.repo_root).as_posix(),
                    version_path_rel=vf.relative_to(cfg.repo_root).as_posix(),
                    version_field=field,
                    fmt=fmt,
                )
            )
    return comps


# ── git plumbing ─────────────────────────────────────────────────────────────
def _git(cfg: Config, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cfg.repo_root, capture_output=True, text=True, check=True).stdout


def _changed_files(cfg: Config, base, head, staged) -> list[str]:
    if staged:
        out = _git(cfg, "diff", "--cached", "--name-only")
    else:
        if not base or not head:
            raise SystemExit("CI mode requires BASE_SHA and HEAD_SHA.")
        out = _git(cfg, "diff", "--name-only", f"{base}..{head}")
    return [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]


def _version_at(cfg: Config, ref: str, comp: Component) -> str | None:
    try:
        text = _git(cfg, "show", f"{ref}:{comp.version_path_rel}")
    except subprocess.CalledProcessError:
        return None
    if comp.version_field is None or Path(comp.version_path_rel).name.lower() == "version":
        return text.strip() or None
    # crude field read from the historical blob (good enough for the gate)
    m = re.search(rf"{re.escape(comp.version_field.split('.')[-1])}\s*[:=]\s*['\"]?([0-9][^'\"\s]*)", text)
    return m.group(1) if m else None


# ── the check ────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    changed = _changed_files(cfg, base, head, args.staged)
    print(f"version-check: mode={'staged' if args.staged else 'CI'}, {len(changed)} file(s) in diff")

    msgs = _commit_messages(cfg, base, head, args.staged)
    if SKIP_MARKER in msgs:
        print(f"  [!] SKIPPED via {SKIP_MARKER} (logged)")
        return 0

    comps = discover_components(cfg)
    failures: list[str] = []
    print("\nPer-component results:")
    for comp in sorted(comps, key=lambda c: c.dir_rel):
        # non-exempt source files under this component's dir, excluding its version file
        prefix = comp.dir_rel.rstrip("/") + "/" if comp.dir_rel else ""
        src_changed = [
            f
            for f in changed
            if (prefix == "" or f.startswith(prefix))
            and f != comp.version_path_rel
            and not cfg.is_exempt(f)
            and not _is_version_file(f, comps)
        ]
        version_changed = comp.version_path_rel in changed
        cur = _read_version(cfg, comp)
        if not src_changed:
            print(f"  [PASS] {comp.dir_rel or '(root)'} ({cur})")
            continue
        if not version_changed:
            failures.append(
                f"{comp.dir_rel}: {len(src_changed)} source file(s) changed but "
                f"{comp.version_path_rel} not bumped. Sample: {', '.join(src_changed[:3])}"
            )
            print(f"  [FAIL] {comp.dir_rel} ({cur}) -- not bumped")
            continue
        old = _parse(_version_at(cfg, base or "HEAD", comp), comp.fmt) if not args.staged else None
        new = _parse(cur, comp.fmt)
        if old is not None and new is not None and new <= old:
            failures.append(f"{comp.dir_rel}: version not increased ({cur}).")
            print(f"  [FAIL] {comp.dir_rel} -- not monotonic")
        else:
            print(f"  [PASS] {comp.dir_rel} (-> {cur})")

    if failures:
        print("\nversion-check: FAIL")
        for f in failures:
            print(f"  -> {f}")
        print(f"\nBump the version, OR add '{SKIP_MARKER}' to a commit message (logged).")
        return 1
    print("\nversion-check: PASS")
    return 0


def _is_version_file(path: str, comps: list[Component]) -> bool:
    return any(path == c.version_path_rel for c in comps)


def _commit_messages(cfg: Config, base, head, staged) -> str:
    try:
        if staged:
            return _git(cfg, "log", "-1", "--format=%B")
        return _git(cfg, "log", "--format=%B", f"{base}..{head}")
    except subprocess.CalledProcessError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
