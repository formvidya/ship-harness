"""Unit tests for the generic CI matrix builder.

Locks the services[] x languages[] -> matrix resolution (test command,
{dir}/{cov} substitution, runtime, per-service override, service deps).
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_ci_matrix as bm  # noqa: E402


def _cfg(services, languages):
    return SimpleNamespace(raw={"services": services, "languages": languages})


_LANGS = [
    {"id": "python", "runtime": "3.11", "test": "pytest {dir} --cov-fail-under={cov}", "cov": 80},
    {"id": "node", "runtime": "20", "test": "npm test"},
    {"id": "dart", "runtime": "3.29.0", "test": "flutter test"},
]


def test_python_service_resolves_test_and_runtime():
    m = bm.build_matrix(_cfg([{"name": "identity", "lang": "python", "path": "services/identity"}], _LANGS))
    assert len(m) == 1
    e = m[0]
    assert e["name"] == "identity" and e["runtime"] == "3.11"
    assert e["test"] == "pytest services/identity --cov-fail-under=80"  # {dir} + {cov} substituted


def test_per_service_test_override():
    m = bm.build_matrix(_cfg([{"name": "x", "lang": "python", "path": "p", "test": "pytest p/tests -q"}], _LANGS))
    assert m[0]["test"] == "pytest p/tests -q"  # override wins over language default


def test_service_needs_passthrough():
    m = bm.build_matrix(_cfg([{"name": "x", "lang": "python", "path": "p", "needs": ["mongo", "redis"]}], _LANGS))
    assert m[0]["needs"] == ["mongo", "redis"]


def test_multiple_languages():
    m = bm.build_matrix(
        _cfg(
            [
                {"name": "api", "lang": "python", "path": "a"},
                {"name": "web", "lang": "node", "path": "w"},
                {"name": "app", "lang": "dart", "path": "m"},
            ],
            _LANGS,
        )
    )
    by = {e["name"]: e for e in m}
    assert by["web"]["test"] == "npm test" and by["web"]["runtime"] == "20"
    assert by["app"]["test"] == "flutter test"


def test_empty_services_is_empty_matrix():
    assert bm.build_matrix(_cfg([], _LANGS)) == []


def test_unknown_language_degrades_gracefully():
    # a service whose lang isn't declared -> no test cmd, no runtime, but still listed
    m = bm.build_matrix(_cfg([{"name": "x", "lang": "go", "path": "p"}], _LANGS))
    assert m[0]["name"] == "x" and m[0]["test"] == "" and m[0]["runtime"] == ""
