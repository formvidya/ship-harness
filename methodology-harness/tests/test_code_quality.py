"""Unit tests for the generic code-quality engine.

Validates the language-adapter logic (diff filtering by detect globs, {files}/
{dir} substitution, per-language pass/fail aggregation, tool-missing handling)
with controllable fake commands -- no dependency on ruff/prettier/flutter.
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_code_quality as cq  # noqa: E402


@pytest.fixture()
def repo(tmp_path):
    """A tiny git repo with a base commit; returns (path, base_sha)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / ".context").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    # A fake checker invoked as a plain argv (no path backslashes for shlex):
    # exits 1 iff any arg file contains the token "BAD".
    (tmp_path / "fake_check.py").write_text(
        "import sys\nsys.exit(1 if any('BAD' in open(f).read() for f in sys.argv[1:]) else 0)\n"
    )
    return tmp_path


def _write_config(repo: Path, fmt_cmd: str):
    # Build via yaml.safe_dump so the command string is escaped correctly.
    cfg = {
        "project": {"name": "T", "languages": ["python"]},
        "code_roots": ["src/**"],
        "exempt_globs": ["**/*.md"],
        "ledger": {"records_dir": "docs/ctx"},
        "languages": [{"id": "python", "detect": ["**/*.py"], "format": fmt_cmd}],
    }
    (repo / ".context" / "config.yml").write_text(yaml.safe_dump(cfg, sort_keys=False))


def _commit(repo: Path, msg: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def _run(repo, base, head, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    return cq.main([])


# Invoke the repo-local fake checker as a plain argv (no shell, no path
# backslashes). Uses the running interpreter by basename so PATH resolves it.
_FAKE = f"{Path(sys.executable).name} fake_check.py {{files}}"


def test_clean_python_passes(repo, monkeypatch):
    _write_config(repo, _FAKE)
    base = _commit(repo, "base")
    (repo / "src" / "a.py").write_text("x = 2  # clean\n")
    head = _commit(repo, "edit")
    assert _run(repo, base, head, monkeypatch) == 0


def test_dirty_python_fails(repo, monkeypatch):
    _write_config(repo, _FAKE)
    base = _commit(repo, "base")
    (repo / "src" / "a.py").write_text("x = 2  # BAD\n")
    head = _commit(repo, "edit")
    assert _run(repo, base, head, monkeypatch) == 1


def test_no_matching_files_is_noop(repo, monkeypatch):
    _write_config(repo, _FAKE)
    base = _commit(repo, "base")
    (repo / "README.md").write_text("docs only\n")
    head = _commit(repo, "docs")
    assert _run(repo, base, head, monkeypatch) == 0  # python language has no files


def test_missing_tool_fails(repo, monkeypatch):
    _write_config(repo, "definitely-not-a-real-tool-xyz --check {files}")
    base = _commit(repo, "base")
    (repo / "src" / "a.py").write_text("x = 3\n")
    head = _commit(repo, "edit")
    assert _run(repo, base, head, monkeypatch) == 1  # tool-missing is a failure, not a silent pass


def test_skip_marker(repo, monkeypatch):
    _write_config(repo, _FAKE)
    base = _commit(repo, "base")
    (repo / "src" / "a.py").write_text("x = 2  # BAD\n")
    head = _commit(repo, "edit [skip-quality-check]")
    assert _run(repo, base, head, monkeypatch) == 0  # bypassed despite the BAD token
