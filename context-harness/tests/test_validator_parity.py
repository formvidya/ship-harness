"""Parity tests for the two context-record validators.

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
# The Historian's decision. Kept separate so a test can withhold it: the
# `require_historian` flag is the one requirement whose absence used to be
# invisible in both validators.
_HISTORIAN_DEC = textwrap.dedent(
    f"""    - decision_id: DEC-{_PR}-2
      agent: context-keeper
      decision: read the record against the diff and corrected two claims
      rationale: the author is the only other reader of their own record, so nothing else catches a plausible false claim
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


def _write_record(repo: Path, *, merged_fields: bool, historian: bool = True) -> Path:
    rec = repo / "docs" / "context" / "records" / f"{_CTX_ID}.md"
    rec.parent.mkdir(parents=True, exist_ok=True)
    fm = _RECORD_HEAD + (_HISTORIAN_DEC if historian else "") + (_MERGED_ONLY_FM if merged_fields else "")
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

    def commit_record(self, *, merged_fields: bool, historian: bool = True) -> None:
        """Write the record (with or without the merged-only frontmatter, with
        or without the Historian's decision), commit it alongside a code-root
        change, and point HEAD_SHA at it."""
        self._n += 1
        (self.root / "src" / "app.py").write_text(f"BASE = {self._n}\n", encoding="utf-8")
        _write_record(self.root, merged_fields=merged_fields, historian=historian)
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
    monkeypatch.setenv("PR_NUMBER", str(_PR))
    monkeypatch.setenv("BASE_SHA", _git(tmp_path, "rev-parse", "HEAD"))
    return _Fixture(tmp_path, monkeypatch)


def _ctx_lint(*floor_args: str) -> int:
    return ctx_cli.main(["lint", "--pr", str(_PR), *floor_args])


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


# ── the historian requirement, in both validators ────────────────────────
def test_missing_historian_decision_fails_both_validators(repo, capsys):
    """A record complete in every other respect, read by nobody but its author."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _ctx_lint("--floor", "merged") == 1
    ctx_out = capsys.readouterr().out
    assert _gate("--floor", "merged") == 1
    gate_out = capsys.readouterr().out

    for out in (ctx_out, gate_out):
        assert schema.HISTORIAN_AGENT in out


def test_missing_historian_decision_fails_both_at_the_default_floor(repo, capsys):
    """The defaults must agree, not just the explicit flag -- that was the
    shape of the local-vs-CI drift this suite exists to prevent, and a new
    requirement can reproduce it."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _ctx_lint() == 1
    ctx_out = capsys.readouterr().out
    assert _gate() == 1
    gate_out = capsys.readouterr().out

    for out in (ctx_out, gate_out):
        assert schema.HISTORIAN_AGENT in out


def test_the_gate_goes_green_once_the_historian_has_filed(repo, capsys):
    """The mirror: a gate that never passes is not a gate, it is an outage."""
    repo.commit_record(merged_fields=True, historian=True)

    assert _ctx_lint("--floor", "merged") == 0
    assert _gate("--floor", "merged") == 0
    capsys.readouterr()


def test_staged_mode_does_not_demand_the_historian(repo, capsys):
    """Drafting stays unblocked -- the Historian reads a change at the END, so
    a pre-commit hook demanding it would block the very commits that produce
    the work under review. `--floor merged` isolates the axis: only the staged
    default for require_historian can make the first assertion pass."""
    repo.commit_record(merged_fields=True, historian=False)
    (repo / "src" / "app.py").write_text("BASE = staged", encoding="utf-8")
    _git(repo.root, "add", "-A")

    assert _gate("--staged", "--floor", "merged") == 0
    assert _gate("--staged", "--floor", "merged", "--require-historian") == 1
    assert schema.HISTORIAN_AGENT in capsys.readouterr().out


def test_ctx_lint_demands_the_historian_only_when_targeting_a_pr(repo, capsys):
    """`--pr N` is the pre-merge question and must match CI. The bare sweep is
    a historical health report over records written before the rule existed;
    defaulting it on there prints ~347 failures and trains everyone to ignore
    the command."""
    repo.commit_record(merged_fields=True, historian=False)

    assert ctx_cli.main(["lint", "--pr", str(_PR)]) == 1
    assert schema.HISTORIAN_AGENT in capsys.readouterr().out

    assert ctx_cli.main(["lint"]) == 0
    assert "historian not required" in capsys.readouterr().out


def test_assemble_will_not_certify_a_record_the_historian_never_read(repo, capsys):
    """CONTEXT-OK is what the release-manager reads as 'the record is good'."""
    repo.commit_record(merged_fields=True, historian=False)

    assert ctx_cli.main(["assemble", "--pr", str(_PR)]) == 1
    out = capsys.readouterr().out
    assert "CONTEXT-INCOMPLETE" in out and schema.HISTORIAN_AGENT in out
    assert not (repo / ".claude" / f"context-recorded-{_PR}").exists()

    repo.commit_record(merged_fields=True, historian=True)
    assert ctx_cli.main(["assemble", "--pr", str(_PR)]) == 0
    assert (repo / ".claude" / f"context-recorded-{_PR}").is_file()
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
    args = ctx_cli.build_parser().parse_args(["lint", "--pr", str(_PR)])
    assert args.floor == schema.DEFAULT_GATE_FLOOR


def test_ctx_assemble_validates_at_the_gate_floor(repo, capsys):
    """A CONTEXT-OK marker must mean 'this will pass Context Check'. Assemble
    used to lint at the record's own status and green-lit records CI rejected."""
    repo.commit_record(merged_fields=False)
    assert ctx_cli.main(["assemble", "--pr", str(_PR)]) == 1
    out = capsys.readouterr().out
    assert "CONTEXT-INCOMPLETE" in out
    assert "test_results" in out
    assert not (repo / ".claude" / f"context-recorded-{_PR}").exists()

    repo.commit_record(merged_fields=True)
    assert ctx_cli.main(["assemble", "--pr", str(_PR)]) == 0
    assert (repo / ".claude" / f"context-recorded-{_PR}").is_file()
    capsys.readouterr()


# ── the draft carve-out: one axis deferred, every other axis still fatal ─────
# `--require-historian` on a required check that is NOT draft-gated went red on
# every draft push of every code-root PR, because context-keeper reads a change
# at the END of its lifecycle. Always-red is how a broken CI lane stays
# invisible -- nobody reads a signal that is red every time -- and here it would
# bury the very failures the no-draft-gating decision exists to surface early.
# `--historian-advisory` is what the workflow passes while the PR is a draft.
def test_draft_advisory_defers_the_historian_instead_of_failing(repo, capsys):
    """The pair, on one record: advisory PASSES where the strict gate FAILS."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _gate("--floor", "merged") == 1
    capsys.readouterr()
    assert _gate("--floor", "merged", "--historian-advisory") == 0


def test_draft_advisory_still_names_the_outstanding_decision(repo, capsys):
    """A green draft run must never read as 'the Historian has been here'.

    That would put back the SILENT SKIP this gate removed, wearing a draft.
    The axis stays reported: the agent is named, the run says the wait is
    EXPECTED so a reviewer does not read it as a broken PR, and the summary
    line itself carries the deferral rather than an unqualified PASS."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _gate("--floor", "merged", "--historian-advisory") == 0
    out = capsys.readouterr().out
    assert schema.HISTORIAN_AGENT in out
    assert "EXPECTED" in out
    assert "Ready for review" in out
    assert "::warning" in out
    assert "context-check: PASS (1 deferred to Ready-flip" in out


def test_draft_advisory_relaxes_the_historian_axis_and_nothing_else(repo, capsys):
    """The load-bearing negative. If the flag relaxed the whole record lint,
    every one of these tests would still pass while the gate stopped gating --
    a record hollow at the merged floor must STILL fail on a draft. That is the
    entire reason this job is not draft-gated in the first place."""
    repo.commit_record(merged_fields=False, historian=False)

    assert _gate("--floor", "merged", "--historian-advisory") == 1
    out = capsys.readouterr().out
    assert "test_results" in out and "risk_level" in out
    assert "context-check: FAIL" in out
    # ...and the deferred axis is still reported alongside the failure, not
    # swallowed by it: the reader needs the whole state, not the fatal half.
    assert schema.HISTORIAN_AGENT in out


def test_draft_advisory_is_quiet_when_the_historian_has_filed(repo, capsys):
    """The mirror. A warning printed unconditionally is decoration, and would
    make every assertion above satisfiable by a `print()` with no logic."""
    repo.commit_record(merged_fields=True, historian=True)

    assert _gate("--floor", "merged", "--historian-advisory") == 0
    out = capsys.readouterr().out
    assert "::warning" not in out
    assert "deferred" not in out
    assert out.rstrip().endswith("context-check: PASS")


def test_advisory_looks_at_the_axis_even_under_no_require_historian(repo, capsys):
    """`--historian-advisory` sets the SEVERITY, not whether we look. Reporting
    and not failing is strictly more informative than not looking, so the
    explicit off-switch must not silence it into a bare green."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _gate("--floor", "merged", "--no-require-historian") == 0
    assert schema.HISTORIAN_AGENT not in capsys.readouterr().out

    assert _gate("--floor", "merged", "--no-require-historian", "--historian-advisory") == 0
    assert schema.HISTORIAN_AGENT in capsys.readouterr().out


def test_the_merge_verdict_is_untouched_by_the_draft_carve_out(repo, capsys):
    """The gate's founding criterion -- `ctx lint --pr N` is the documented
    local equivalent of the gate -- survives. The carve-out is a CI TIMING concession
    keyed to draft state, not a second standard: the moment the flag is absent
    (Ready-flip, merge_group, and the local command at any time) both validators
    reach the same verdict on the same record."""
    repo.commit_record(merged_fields=True, historian=False)

    assert _gate("--floor", "merged", "--historian-advisory") == 0
    capsys.readouterr()

    assert _ctx_lint("--floor", "merged") == 1
    assert schema.HISTORIAN_AGENT in capsys.readouterr().out
    assert _gate("--floor", "merged") == 1
    assert schema.HISTORIAN_AGENT in capsys.readouterr().out


def test_staged_mode_is_unaffected_by_the_advisory_flag(repo, capsys):
    """Pre-commit was already off; the new flag must not turn it into a source
    of noise on every commit a builder makes."""
    repo.commit_record(merged_fields=True, historian=False)
    (repo / "src" / "app.py").write_text("BASE = staged", encoding="utf-8")
    _git(repo.root, "add", "-A")

    assert _gate("--staged", "--floor", "merged") == 0
    assert schema.HISTORIAN_AGENT not in capsys.readouterr().out
