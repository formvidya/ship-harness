"""Parity tests for the two context-record validators (fix/ctx-validator-parity).

Two entry points validate the same record:

  * ``ctx lint``                 -- what a builder agent runs locally
  * ``check_context_record.py``  -- what the Context Check CI gate runs

They burned a CI cycle on two successive PRs by disagreeing: ``ctx lint``
defaulted to the record's own ``status`` (``open`` for every pre-merge record,
because ``lifecycle-sync`` only flips to ``merged`` afterwards) while the gate
defaulted to ``merged``. The four merged-only frontmatter fields -- notably
``test_results`` and ``risk_level`` -- were invisible locally and fatal in CI.
Their ``--floor`` vocabularies also had only ``merged`` in common.

These tests drive both CLIs end-to-end against a throwaway git repo, so they
fail if the floors, the defaults, or the requirement tables ever diverge again.

Run: python -m pytest tools/harness/context-harness/tests/ -q
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ctx"))
import check_context_record  # noqa: E402
import ctx as ctx_cli  # noqa: E402
import schema  # noqa: E402

# ── throwaway repo fixture ───────────────────────────────────────────────────
_CONFIG = textwrap.dedent(
    """\
    project:
      name: Parity Fixture
      languages: [python]
    code_roots:
      - "src/**"
    exempt_globs:
      - "**/*.md"
    ledger:
      records_dir: docs/context/records
      overrides_log: docs/context/context-overrides.log
    """
)

# An `open` record that is complete at the open floor and complete in its body
# at the merged floor -- the ONLY thing it lacks is the merged-only structured
# frontmatter. This is the exact shape both records had when CI rejected them.
_PR = 7
_CTX_ID = f"CTX-{_PR:04d}"
_RECORD_HEAD = textwrap.dedent(
    f"""\
    ---
    ctx_id: {_CTX_ID}
    pr_number: {_PR}
    title: parity fixture record
    status: open
    services_affected: [src]
    agent_decisions:
    - decision_id: DEC-{_PR}-1
      agent: backend
      decision: routed both validators through one requirements table
      rationale: two floors that disagree are two gates, and only one of them blocks merge
    """
)
_MERGED_ONLY_FM = textwrap.dedent(
    """\
    risk_level: LOW
    test_results:
      passed: 12
      failed: 0
    """
)
_BODY = textwrap.dedent(
    """\
    ---

    ## Intent
    Prove the two validators agree.

    ## What Was Done
    Shared one requirements definition between them.

    ## Architecture Used
    The context-harness shared schema-floor table.
    """
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _write_record(repo: Path, *, merged_fields: bool) -> Path:
    rec = repo / "docs" / "context" / "records" / f"{_CTX_ID}.md"
    rec.parent.mkdir(parents=True, exist_ok=True)
    fm = _RECORD_HEAD + (_MERGED_ONLY_FM if merged_fields else "")
    rec.write_text(fm + _BODY, encoding="utf-8")
    return rec


class _Fixture:
    """The throwaway repo plus a way to (re)commit the record under test."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self._monkeypatch = monkeypatch
        self._n = 0

    def __truediv__(self, other):  # so tests can write `repo / ".claude" / ...`
        return self.root / other

    def commit_record(self, *, merged_fields: bool) -> None:
        """Write the record (with or without the merged-only frontmatter),
        commit it alongside a code-root change, and point HEAD_SHA at it."""
        self._n += 1
        (self.root / "src" / "app.py").write_text(f"BASE = {self._n}\n", encoding="utf-8")
        _write_record(self.root, merged_fields=merged_fields)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", f"change {self._n} (merged_fields={merged_fields})")
        self._monkeypatch.setenv("HEAD_SHA", _git(self.root, "rev-parse", "HEAD"))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo carrying the harness config, a code root, and a ledger dir.

    Both validators are run against it in-process: ``ctx lint`` resolves it via
    ``load_config()`` from the CWD, the gate additionally diffs BASE..HEAD.
    """
    (tmp_path / ".context").mkdir()
    (tmp_path / ".context" / "config.yml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("BASE = 0\n", encoding="utf-8")

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "parity@example.test")
    _git(tmp_path, "config", "user.name", "Parity Fixture")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("BASE_SHA", _git(tmp_path, "rev-parse", "HEAD"))
    return _Fixture(tmp_path, monkeypatch)


def _ctx_lint(*floor_args: str) -> int:
    return ctx_cli.main(["lint", "--pr", "7", *floor_args])


def _gate(*floor_args: str) -> int:
    return check_context_record.main(list(floor_args))


# ── the regression: both validators, one verdict ─────────────────────────────
def test_missing_merged_fields_fails_both_validators(repo, capsys):
    """The original failure mode: local PASS, CI FAIL. Both must FAIL now."""
    repo.commit_record(merged_fields=False)

    assert _ctx_lint("--floor", "merged") == 1
    ctx_out = capsys.readouterr().out
    assert _gate("--floor", "merged") == 1
    gate_out = capsys.readouterr().out

    for out in (ctx_out, gate_out):
        assert "test_results" in out
        assert "risk_level" in out


def test_missing_merged_fields_fails_both_at_default_floor(repo, capsys):
    """Same record, neither CLI given a --floor. The defaults must agree too --
    the drift was in the defaults, not in the explicit flag."""
    repo.commit_record(merged_fields=False)

    assert _ctx_lint() == 1
    ctx_out = capsys.readouterr().out
    assert _gate() == 1
    gate_out = capsys.readouterr().out

    for out in (ctx_out, gate_out):
        assert "test_results" in out
        assert "risk_level" in out


def test_populated_record_passes_both_validators(repo, capsys):
    """Add only test_results + risk_level -- both validators must go green."""
    repo.commit_record(merged_fields=True)

    assert _ctx_lint("--floor", "merged") == 0
    assert _gate("--floor", "merged") == 0
    assert _ctx_lint() == 0
    assert _gate() == 0
    capsys.readouterr()


# ── floors below merged keep their (weaker) behaviour ────────────────────────
@pytest.mark.parametrize("floor", ["status", "open", "in_review"])
def test_floors_below_merged_still_accept_the_sparse_record(repo, capsys, floor):
    """`open` and `in_review` add no merged-level requirements, and `status`
    imposes none at all -- a record missing test_results/risk_level passes both
    validators there, exactly as before."""
    repo.commit_record(merged_fields=False)

    assert _ctx_lint("--floor", floor) == 0
    assert _gate("--floor", floor) == 0
    capsys.readouterr()


@pytest.mark.parametrize("floor", schema.FLOOR_CHOICES)
def test_every_floor_is_accepted_by_both_clis(repo, capsys, floor):
    """One vocabulary. Neither CLI may reject a floor the other accepts --
    previously only `merged` was spelled the same in both."""
    repo.commit_record(merged_fields=True)

    assert _ctx_lint("--floor", floor) in (0, 1)
    assert _gate("--floor", floor) in (0, 1)
    capsys.readouterr()


# ── structural: one requirements definition, not two ─────────────────────────
def test_requirements_for_merged_is_the_shared_table():
    fields, sections = schema.requirements_for(schema.DEFAULT_GATE_FLOOR)
    assert set(fields) == {
        "ctx_id",
        "pr_number",
        "title",
        "status",
        "services_affected",
        "agent_decisions",
        "test_results",
        "risk_level",
    }
    assert set(sections) == {"Intent", "What Was Done", "Architecture Used"}


def test_requirements_are_cumulative_across_the_lifecycle():
    open_fields, open_sections = schema.requirements_for("open")
    merged_fields, merged_sections = schema.requirements_for("merged")
    assert set(open_fields) < set(merged_fields)
    assert set(open_sections) < set(merged_sections)
    # test_results / risk_level are the merged-only pair that broke both PRs.
    assert {"test_results", "risk_level"} <= set(merged_fields) - set(open_fields)


def test_requirements_for_rejects_the_record_native_sentinel():
    # `status` is not a lifecycle state; asking what it requires is meaningless
    # without a record. Silently answering "open-level" is the drift we removed.
    with pytest.raises(ValueError):
        schema.requirements_for(schema.RECORD_NATIVE_FLOOR)


def test_normalize_floor_maps_the_sentinel_to_no_floor():
    assert schema.normalize_floor(None) is None
    assert schema.normalize_floor(schema.RECORD_NATIVE_FLOOR) is None
    assert schema.normalize_floor("merged") == "merged"


def test_gate_default_floor_is_merged():
    assert schema.DEFAULT_GATE_FLOOR == "merged"
    assert schema.FLOOR_CHOICES == (schema.RECORD_NATIVE_FLOOR, *schema.LIFECYCLE)


def test_ctx_lint_default_floor_matches_the_gate():
    """The CLI default is read off the parser, so lowering it fails this test."""
    args = ctx_cli.build_parser().parse_args(["lint", "--pr", "7"])
    assert args.floor == schema.DEFAULT_GATE_FLOOR


def test_ctx_assemble_validates_at_the_gate_floor(repo, capsys):
    """A CONTEXT-OK marker must mean 'this will pass Context Check'. Assemble
    used to lint at the record's own status and green-lit records CI rejected."""
    repo.commit_record(merged_fields=False)
    assert ctx_cli.main(["assemble", "--pr", "7"]) == 1
    out = capsys.readouterr().out
    assert "CONTEXT-INCOMPLETE" in out
    assert "test_results" in out
    assert not (repo / ".claude" / "context-recorded-7").exists()

    repo.commit_record(merged_fields=True)
    assert ctx_cli.main(["assemble", "--pr", "7"]) == 0
    assert (repo / ".claude" / "context-recorded-7").is_file()
    capsys.readouterr()
