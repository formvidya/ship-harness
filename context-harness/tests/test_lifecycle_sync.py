"""Integration tests for ``ctx lifecycle-sync`` (fix/ctx-substance-floors).

Builds a throwaway git repo with a minimal .context/config.yml, a merged-PR
style commit (subject containing "(#N)"), and records in various states, then
runs the real CLI via subprocess. Verifies:
  * an ``open`` record whose PR merged flips to ``merged``
  * a record whose PR has NOT merged stays ``open``
  * ``--dry-run`` writes nothing
  * already-advanced records (``deployed``) are untouched

Run: python -m pytest tools/harness/context-harness/tests/ -q
"""

import subprocess
import sys
import textwrap
from pathlib import Path

CTX_PY = Path(__file__).resolve().parents[1] / "ctx" / "ctx.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".context").mkdir(parents=True)
    (repo / ".context" / "config.yml").write_text(
        textwrap.dedent(
            """\
            project:
              name: TestProj
              languages: [python]
            code_roots:
              - "src/**"
            ledger:
              records_dir: docs/context/records
            """
        ),
        encoding="utf-8",
    )
    (repo / "docs" / "context" / "records").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def _record(repo: Path, pr: int, status: str) -> Path:
    path = repo / "docs" / "context" / "records" / f"CTX-{pr:04d}.md"
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            ctx_id: CTX-{pr:04d}
            pr_number: {pr}
            title: record {pr}
            status: {status}
            ---
            ## Intent
            Something real.
            """
        ),
        encoding="utf-8",
    )
    return path


def _run_sync(repo: Path, *extra: str) -> str:
    result = subprocess.run(
        [sys.executable, str(CTX_PY), "lifecycle-sync", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def _status_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no status line in {path}")


def test_merged_pr_record_flips(tmp_path):
    repo = _make_repo(tmp_path)
    rec = _record(repo, 7, "open")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: something shipped (#7)")

    out = _run_sync(repo)
    assert "open -> merged" in out
    assert _status_of(rec) == "merged"


def test_unmerged_pr_record_stays_open(tmp_path):
    repo = _make_repo(tmp_path)
    rec = _record(repo, 9, "open")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: no pr marker here")

    _run_sync(repo)
    assert _status_of(rec) == "open"


def test_dry_run_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    rec = _record(repo, 7, "open")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: something shipped (#7)")

    out = _run_sync(repo, "--dry-run")
    assert "would flip" in out
    assert _status_of(rec) == "open"


def test_body_only_reference_does_not_flip(tmp_path):
    repo = _make_repo(tmp_path)
    rec = _record(repo, 8, "open")
    _git(repo, "add", "-A")
    # The "(#N)" marker appears only in the commit BODY — a reference, not a merge.
    _git(repo, "commit", "-q", "-m", "feat: unrelated work", "-m", "builds on the groundwork from (#8)")

    _run_sync(repo)
    assert _status_of(rec) == "open"


def test_advanced_records_untouched(tmp_path):
    repo = _make_repo(tmp_path)
    rec = _record(repo, 7, "deployed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: something shipped (#7)")

    _run_sync(repo)
    assert _status_of(rec) == "deployed"


def test_flipped_hollow_record_lands_in_curation_queue(tmp_path):
    repo = _make_repo(tmp_path)
    # Real Intent but nothing else: passes open floor, fails merged floor.
    _record(repo, 7, "open")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: something shipped (#7)")

    out = _run_sync(repo)
    assert "[CURATE]" in out
    assert "What Was Done" in out
