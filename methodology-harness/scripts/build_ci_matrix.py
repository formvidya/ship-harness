#!/usr/bin/env python3
"""Generic CI test-matrix builder + dependency-graph resolver.

Two responsibilities, one config source (``.context/config.yml``):

1. **Matrix** (default / ``--selective``): emit the GitHub Actions test matrix
   from ``services[]`` so a project gets a per-service test job WITHOUT
   hand-maintaining a YAML matrix (the review's "4x duplicated Python jobs"
   finding). The workflow consumes the JSON via ``fromJson`` and adds the
   ``ci-success`` fan-in -- the single stable required check name.

2. **Dependency graph** (``--selective`` / ``--affected``): resolve which
   services a change actually touches, so CI runs only the affected jobs. The
   graph -- which repo folders map to which services/jobs -- lives in config,
   not hand-maintained in workflow YAML::

       ci:
         shared_groups:
           py-shared: { paths: ["shared/**", "libs/**"] }   # or a plain glob list
         full_matrix_on: [".github/workflows/ci.yml", ".context/config.yml"]
       services:
         - { name: billing-service, ci_key: billing, lang: python,
             path: services/billing-service, depends_on: [py-shared], needs: [mongo] }

   A service's trigger set is ``<path>/**`` plus the (transitively resolved)
   globs of every group in its ``depends_on``. A change matching any
   ``full_matrix_on`` glob forces the FULL set (broad blast radius). Path
   matching reuses the config loader's ``_glob_match`` (handles ``**`` and
   dotfile paths like ``.github/...`` correctly).

Modes (``main``):

* (default)    full matrix              -> ``services=<json>``  (push / safe fallback)
* ``--selective``  matrix filtered to affected -> ``services=<json>``  (generic dynamic-matrix template)
* ``--affected``   per-service booleans        -> ``<ci_key>=true|false`` lines  (bespoke static jobs)

Changed files come from ``git diff --name-only --no-renames BASE...HEAD`` using
``BASE_SHA``/``HEAD_SHA`` from the env (the pull_request base/head). When either
is absent (push to main, workflow_dispatch, shallow first commit) selection is
not possible and the builder falls back to the FULL set -- the safe direction.
"""

from __future__ import annotations

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


# ── service identity ────────────────────────────────────────────────────────
def service_key(svc: dict) -> str:
    """The CI job's gating output name. Falls back to ``name`` when ``ci_key``
    is absent so a project can keep it terse."""
    return svc.get("ci_key") or svc["name"]


# ── dependency graph ────────────────────────────────────────────────────────
def _group_globs(name: str, groups: dict, _seen: frozenset[str] | None = None) -> list[str]:
    """Resolve a shared_group to its full glob list, following group->group
    ``depends_on`` edges (transitive closure). A group is either a plain list of
    globs or a map ``{paths: [...], depends_on: [...]}``. A cycle is a hard error."""
    _seen = _seen or frozenset()
    if name in _seen:
        raise ValueError(f"cycle in ci.shared_groups via '{name}'")
    spec = groups.get(name)
    if spec is None:
        return []  # unknown group -> contributes nothing (the service still has its own path)
    if isinstance(spec, list):
        return list(spec)
    globs = list(spec.get("paths", []) or [])
    for dep in spec.get("depends_on", []) or []:
        globs.extend(_group_globs(dep, groups, _seen | {name}))
    return globs


def service_triggers(svc: dict, cfg: Config) -> list[str]:
    """Globs that mark this service affected: its own ``<path>/**`` plus the
    resolved globs of every group it ``depends_on``. A missing/root path yields
    ``**`` (always affected) -- the SAFE direction: a mis-declared service
    over-tests rather than silently never running."""
    groups = (cfg.raw.get("ci", {}) or {}).get("shared_groups", {}) or {}
    path = str(svc.get("path") or ".").rstrip("/")
    triggers = ["**" if path in ("", ".") else f"{path}/**"]
    for g in svc.get("depends_on", []) or []:
        triggers.extend(_group_globs(g, groups))
    return triggers


def affected_extra(cfg: Config, changed_files: list[str]) -> dict[str, bool]:
    """Non-service CI jobs declared in ``ci.extra_jobs`` -- e.g. the harness
    self-test that guards this very resolver. Each job is ``{paths: [...]}`` (or
    a plain glob list) and is affected when a changed file matches its paths, or
    when any ``full_matrix_on`` glob matches (broad blast radius)."""
    ci = cfg.raw.get("ci", {}) or {}
    extra = ci.get("extra_jobs", {}) or {}
    full_on = ci.get("full_matrix_on", []) or []
    files = [f for f in changed_files if f and f.strip()]
    full = any(_glob_match(g, f) for g in full_on for f in files)
    out: dict[str, bool] = {}
    for name, spec in extra.items():
        paths = spec.get("paths", []) if isinstance(spec, dict) else (spec or [])
        out[name] = full or any(_glob_match(g, f) for g in (paths or []) for f in files)
    return out


def affected_services(cfg: Config, changed_files: list[str]) -> list[str]:
    """ci_keys of the services a change touches, via the dependency graph.

    Any changed file matching a ``full_matrix_on`` glob -> every service (broad
    blast radius). Otherwise a service is affected iff any changed file matches
    any of its triggers. Order follows ``services[]`` declaration order."""
    ci = cfg.raw.get("ci", {}) or {}
    services = cfg.raw.get("services", []) or []
    full_on = ci.get("full_matrix_on", []) or []
    files = [f for f in changed_files if f and f.strip()]

    if any(_glob_match(g, f) for g in full_on for f in files):
        return [service_key(s) for s in services]

    hit: list[str] = []
    for svc in services:
        triggers = service_triggers(svc, cfg)
        if any(_glob_match(g, f) for g in triggers for f in files):
            hit.append(service_key(svc))
    return hit


# ── matrix ──────────────────────────────────────────────────────────────────
def build_matrix(cfg: Config, only: set[str] | None = None) -> list[dict]:
    """One matrix entry per service: name, ci_key, lang, path, runtime, test, needs.

    When ``only`` (a set of ci_keys) is given, restrict to those services -- the
    selective matrix. ``None`` means the full matrix."""
    languages = {lang["id"]: lang for lang in (cfg.raw.get("languages", []) or []) if "id" in lang}
    entries: list[dict] = []
    for svc in cfg.raw.get("services", []) or []:
        key = service_key(svc)
        if only is not None and key not in only:
            continue
        lang_id = svc.get("lang", "")
        lang = languages.get(lang_id, {})
        path = svc.get("path", ".")
        test = svc.get("test") or lang.get("test", "")
        test = test.replace("{dir}", path).replace("{cov}", str(lang.get("cov", 0)))
        entries.append(
            {
                "name": svc["name"],
                "ci_key": key,
                "lang": lang_id,
                "path": path,
                "runtime": str(lang.get("runtime", "")),
                "test": test,
                "needs": svc.get("needs", []),
            }
        )
    return entries


# ── changed files ───────────────────────────────────────────────────────────
def changed_files_from_git(base: str, head: str) -> list[str]:
    """Files changed in the PR: ``git diff --name-only --no-renames BASE...HEAD``.

    Three-dot = changes on the PR branch since the merge-base (what "files
    changed in this PR" means; matches dorny/paths-filter). ``--no-renames``
    surfaces a rename as delete(old)+add(new) so BOTH the source and destination
    directories fan out -- the conservative direction for CI. Run via argv (no
    shell) -> attacker-controlled PR filenames can't inject."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _selection(argv: list[str]) -> tuple[bool, list[str] | None]:
    """Returns (selective_possible, changed_files). Selection needs a usable
    BASE_SHA + HEAD_SHA. Falls back to the full set (selective_possible=False)
    when either is missing (dispatch), the base is the all-zero null SHA (first
    push to a new branch), or the diff can't be computed (unreachable base on a
    shallow clone) -- the safe direction is always FULL, never silently empty."""
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    if not (base and head) or set(base) == {"0"}:
        return False, None
    try:
        return True, changed_files_from_git(base, head)
    except subprocess.CalledProcessError:
        return False, None


def _write_output(text: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    print(text)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_config()

    if "--affected" in argv:
        # Per-job booleans for bespoke static jobs: "<key>=true|false". Covers
        # services[] (by ci_key) plus ci.extra_jobs (non-service jobs).
        svc_keys = [service_key(s) for s in (cfg.raw.get("services", []) or [])]
        extra_keys = list((cfg.raw.get("ci", {}) or {}).get("extra_jobs", {}) or {})
        possible, changed = _selection(argv)
        if possible:
            live = set(affected_services(cfg, changed))
            extra = affected_extra(cfg, changed)
        else:  # unselectable -> full: every job true
            live = set(svc_keys)
            extra = {k: True for k in extra_keys}
        rows = [(k, k in live) for k in svc_keys] + [(k, extra.get(k, False)) for k in extra_keys]
        _write_output("\n".join(f"{k}={'true' if v else 'false'}" for k, v in rows))
        return 0

    only = None
    if "--selective" in argv:
        possible, changed = _selection(argv)
        if possible:
            only = set(affected_services(cfg, changed))
    matrix = build_matrix(cfg, only=only)
    _write_output(f"services={json.dumps(matrix)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
