"""Unit tests for the generic version-check reader registry.

Covers the version PARSING/comparison and the multi-format reader — the part
that generalizes the gate beyond services/**/VERSION. No git/network.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_version_bumps as vc  # noqa: E402


# ── version parsing / monotonicity ──────────────────────────────────────────
def test_parse_semver():
    assert vc._parse("0.4.8", "semver") == (0, 4, 8)
    assert vc._parse("1.10.0", "semver") == (1, 10, 0)


def test_parse_semver_build():
    assert vc._parse("1.2.0+5", "semver+build") == (1, 2, 0, 5)
    assert vc._parse("1.2.0", "semver+build") == (1, 2, 0, 0)


def test_parse_rejects_garbage():
    assert vc._parse("not-a-version", "semver") is None
    assert vc._parse(None, "semver") is None


def test_monotonicity_ordering():
    assert vc._parse("0.4.9", "semver") > vc._parse("0.4.8", "semver")
    assert vc._parse("1.2.1+6", "semver+build") > vc._parse("1.2.0+5", "semver+build")
    # build number breaks ties at the same semver
    assert vc._parse("1.2.0+6", "semver+build") > vc._parse("1.2.0+5", "semver+build")


# ── multi-format reader ──────────────────────────────────────────────────────
def _cfg(tmp: Path):
    return SimpleNamespace(repo_root=tmp, raw={})


def test_read_plain_version_file(tmp_path):
    (tmp_path / "VERSION").write_text("0.3.4\n")
    comp = vc.Component("svc", "VERSION", None, "semver")
    assert vc._read_version(_cfg(tmp_path), comp) == "0.3.4"


def test_read_pubspec_yaml(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.2.0+5\n")
    comp = vc.Component("app", "pubspec.yaml", "version", "semver+build")
    assert vc._read_version(_cfg(tmp_path), comp) == "1.2.0+5"


def test_read_pyproject_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.4.1"\n')
    comp = vc.Component("lib", "pyproject.toml", "project.version", "semver")
    assert vc._read_version(_cfg(tmp_path), comp) == "0.4.1"


def test_read_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "web", "version": "2.1.0"}')
    comp = vc.Component("web", "package.json", "version", "semver")
    assert vc._read_version(_cfg(tmp_path), comp) == "2.1.0"


def test_discover_default_is_services_glob(tmp_path):
    (tmp_path / "services" / "a").mkdir(parents=True)
    (tmp_path / "services" / "a" / "VERSION").write_text("0.1.0")
    cfg = SimpleNamespace(repo_root=tmp_path, raw={})
    comps = vc.discover_components(cfg)
    assert any(c.dir_rel == "services/a" for c in comps)
