"""Context-harness configuration loader.

The harness is generic. EVERY project-specific value lives in one fill-once
file, ``.context/config.yml`` at the repo root. This module loads and validates
it and exposes a typed :class:`Config` so the rest of the engine never hard-codes
a path, glob, topic, or role.

Design rule: nothing in ``tools/harness/context-harness/`` may contain a
project-specific string. If you find yourself wanting to write ``services/`` or
your own product's name in engine code, it belongs in ``.context/config.yml``
instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency surfaced to the user
    raise SystemExit("context-harness requires PyYAML. Install it: pip install pyyaml") from exc


CONFIG_RELPATH = ".context/config.yml"

# Keys every config must define. Kept small on purpose — the harness should be
# usable by a new project after filling ~8 fields.
_REQUIRED_TOP_KEYS = ("project", "code_roots", "ledger")
_REQUIRED_PROJECT_KEYS = ("name", "languages")
_REQUIRED_LEDGER_KEYS = ("records_dir",)


@dataclass(frozen=True)
class LedgerPaths:
    records_dir: str
    digests_dir: str | None
    registry: str | None
    open_loops: str | None
    topics: str | None
    overrides_log: str | None
    index_dir: str | None


@dataclass(frozen=True)
class Config:
    """The loaded, validated project profile. Repo-root relative paths."""

    repo_root: Path
    name: str
    slug: str
    description: str
    languages: tuple[str, ...]
    code_roots: tuple[str, ...]
    exempt_globs: tuple[str, ...]
    reference_architecture: str | None
    ledger: LedgerPaths
    topic_seeds: tuple[str, ...]
    risk_policy: dict[str, dict[str, str]]
    role_bindings: dict[str, tuple[str, ...]]
    skip_marker: str
    change_unit_key: str
    ci_check_name: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # ── derived helpers ────────────────────────────────────────────────────
    def records_dir(self) -> Path:
        return self.repo_root / self.ledger.records_dir

    def record_path(self, pr_number: int | str) -> Path:
        """Canonical per-change record path. Zero-padded for stable sort."""
        return self.records_dir() / f"CTX-{int(pr_number):04d}.md"

    def is_exempt(self, rel_path: str) -> bool:
        return any(_glob_match(g, rel_path) for g in self.exempt_globs)

    def matched_code_root(self, rel_path: str) -> str | None:
        """Return the first code_root glob a path falls under, else None."""
        for root in self.code_roots:
            if _glob_match(root, rel_path):
                return root
        return None


# ── glob matching ──────────────────────────────────────────────────────────
# A small, predictable glob dialect (POSIX-ish):
#   **  matches any run of characters including "/"
#   *   matches any run of characters except "/"
#   ?   matches a single character except "/"
# Paths are always compared with forward slashes, repo-root relative.
def _glob_to_regex(glob: str) -> re.Pattern[str]:
    i, n, out = 0, len(glob), ["^"]
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                out.append(".*")  # ** → across directories
                i += 2
                # swallow a trailing slash after ** so "a/**" matches "a/b"
                if i < n and glob[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_match(glob: str, rel_path: str) -> bool:
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith("./"):  # strip a leading "./" only -- NOT leading dots
        rel_path = rel_path[2:]  # (lstrip("./") would mangle .github, .context, ...)
    pat = _GLOB_CACHE.get(glob)
    if pat is None:
        pat = _glob_to_regex(glob)
        _GLOB_CACHE[glob] = pat
    if pat.match(rel_path):
        return True
    # A bare-directory glob like "services/**" should also match the dir itself
    # and any nested file even when authored without a trailing slash variant.
    if glob.endswith("/**") and rel_path.startswith(glob[:-3] + "/"):
        return True
    return False


# ── loading ──────────────────────────────────────────────────────────────
def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from CWD looking for .context/config.yml, then a .git dir."""
    cur = (start or Path.cwd()).resolve()
    for cand in (cur, *cur.parents):
        if (cand / CONFIG_RELPATH).is_file():
            return cand
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return cand
    return cur


class ConfigError(SystemExit):
    pass


def load_config(repo_root: Path | None = None) -> Config:
    root = repo_root or find_repo_root()
    cfg_path = root / CONFIG_RELPATH
    if not cfg_path.is_file():
        raise ConfigError(
            f"No {CONFIG_RELPATH} found under {root}. "
            f"Copy tools/harness/context-harness/templates/config.example.yml to "
            f"{CONFIG_RELPATH} and fill it in."
        )
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    _validate(data, cfg_path)

    project = data["project"]
    ledger_raw = data["ledger"]
    ledger = LedgerPaths(
        records_dir=ledger_raw["records_dir"],
        digests_dir=ledger_raw.get("digests_dir"),
        registry=ledger_raw.get("registry"),
        open_loops=ledger_raw.get("open_loops"),
        topics=ledger_raw.get("topics"),
        overrides_log=ledger_raw.get("overrides_log"),
        index_dir=ledger_raw.get("index_dir"),
    )
    enforcement = data.get("enforcement", {}) or {}
    role_bindings = {
        role: tuple(agents if isinstance(agents, list) else [agents])
        for role, agents in (data.get("role_bindings", {}) or {}).items()
    }
    return Config(
        repo_root=root,
        name=project["name"],
        slug=project.get("slug", _slugify(project["name"])),
        description=project.get("description", ""),
        languages=tuple(project["languages"]),
        code_roots=tuple(data["code_roots"]),
        exempt_globs=tuple(data.get("exempt_globs", [])),
        reference_architecture=data.get("reference_architecture"),
        ledger=ledger,
        topic_seeds=tuple(data.get("topic_seeds", [])),
        risk_policy=data.get("risk_policy", {}) or {},
        role_bindings=role_bindings,
        skip_marker=enforcement.get("skip_marker", "[skip-context]"),
        change_unit_key=enforcement.get("change_unit_key", "pr_number"),
        ci_check_name=enforcement.get("ci_check_name", "Context Check / per-change record present & valid"),
        raw=data,
    )


def _validate(data: dict[str, Any], cfg_path: Path) -> None:
    errs: list[str] = []
    for k in _REQUIRED_TOP_KEYS:
        if k not in data:
            errs.append(f"missing top-level key: {k}")
    if isinstance(data.get("project"), dict):
        for k in _REQUIRED_PROJECT_KEYS:
            if k not in data["project"]:
                errs.append(f"missing project.{k}")
    if isinstance(data.get("ledger"), dict):
        for k in _REQUIRED_LEDGER_KEYS:
            if k not in data["ledger"]:
                errs.append(f"missing ledger.{k}")
    if isinstance(data.get("code_roots"), list) and not data["code_roots"]:
        errs.append("code_roots must list at least one glob")
    if errs:
        raise ConfigError(f"Invalid {cfg_path}:\n  - " + "\n  - ".join(errs))


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
