"""Unit tests for the context-record schema floors.

Covers the two regressions that let a whole run of consecutive records -- 16 of
them, before anyone noticed -- ship hollow:
  * _has_nonempty_section — the ctx-init scaffold placeholder (``_TODO..._``)
    used to count as content, so the template satisfied its own floor.
  * lint_record(floor=...) — the merged-level requirements only bound at
    ``status: merged``, and nothing ever advanced status; the PR gate now
    imposes ``floor="merged"`` so substance is due before merge.

Run: python -m pytest tools/harness/context-harness/tests/ -q
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ctx"))
import schema  # noqa: E402


# ── placeholder detection ────────────────────────────────────────────────────
def test_todo_scaffold_is_empty():
    body = "## Intent\n_TODO: problem, goals, acceptance criteria_\n\n## Next\nx\n"
    assert not schema._has_nonempty_section(body, "Intent")


def test_bare_todo_underscore_is_empty():
    body = "## What Was Done\n_TODO_\n"
    assert not schema._has_nonempty_section(body, "What Was Done")


def test_unwrapped_todo_line_is_empty():
    body = "## Test Results\nTODO: run them\n"
    assert not schema._has_nonempty_section(body, "Test Results")


def test_real_content_after_todo_counts():
    body = "## Intent\n_TODO_\nShip the annotation review gate.\n"
    assert schema._has_nonempty_section(body, "Intent")


def test_todo_prefix_word_is_content():
    # "TODOLIST cleanup shipped" is real prose, not a placeholder.
    body = "## Intent\nTODOLIST cleanup shipped\n"
    assert schema._has_nonempty_section(body, "Intent")


def test_decorated_todo_variants_are_empty():
    # Security-review finding 3: bold, list-item, and quoted TODOs are still
    # placeholders, not substance.
    for variant in ("**TODO**", "- TODO: fill in", "> TODO", "* TODO later", "_ TODO _"):
        body = f"## Intent\n{variant}\n"
        assert not schema._has_nonempty_section(body, "Intent"), variant


def test_none_yet_placeholder_still_counts():
    # "_none yet_" is a legitimate value for optional sections, not a TODO.
    body = "## Feedback Events\n_none yet_\n"
    assert schema._has_nonempty_section(body, "Feedback Events")


def test_missing_heading_is_empty():
    assert not schema._has_nonempty_section("## Other\nx\n", "Intent")


# ── lint floors ──────────────────────────────────────────────────────────────
# One synthetic PR number drives the record id, the filename and the decision
# ids, so the fixture stays internally consistent (the linter cross-checks them).
_PR = 1
_CTX_ID = f"CTX-{_PR:04d}"
_DEC_ID = f"DEC-{_PR}-1"


def _write_record(tmp_path: Path, *, status: str = "open", body: str = "", extra_fm: str = "") -> Path:
    rec = tmp_path / f"{_CTX_ID}.md"
    fm = f"ctx_id: {_CTX_ID}\npr_number: {_PR}\ntitle: test record\nstatus: {status}\n{extra_fm}"
    rec.write_text("---\n" + fm.rstrip() + "\n---\n" + body, encoding="utf-8")
    return rec


_MERGED_FM = textwrap.dedent(
    f"""\
    services_affected: [svc]
    risk_level: LOW
    test_results:
      passed: 3
      failed: 0
    agent_decisions:
    - decision_id: {_DEC_ID}
      agent: backend
      decision: did the thing
      rationale: because reasons
    """
)

_FULL_BODY = "## Intent\nShip it.\n\n## What Was Done\nThe thing.\n\n## Architecture Used\nThe usual pattern.\n"


def test_open_record_with_todo_intent_fails(tmp_path):
    rec = _write_record(tmp_path, body="## Intent\n_TODO: problem, goals, acceptance criteria_\n")
    problems = schema.lint_record(rec)
    assert any("Intent" in p for p in problems)


def test_open_record_with_real_intent_passes(tmp_path):
    rec = _write_record(tmp_path, body="## Intent\nShip the review gate.\n")
    assert schema.lint_record(rec) == []


def test_merged_floor_binds_open_record(tmp_path):
    # status: open, but the PR gate imposes floor=merged -> merged requirements apply.
    rec = _write_record(tmp_path, body="## Intent\nShip it.\n")
    problems = schema.lint_record(rec, floor="merged")
    joined = "\n".join(problems)
    assert "What Was Done" in joined
    assert "Architecture Used" in joined
    assert "test_results" in joined
    assert "risk_level" in joined


def test_merged_floor_passes_complete_record(tmp_path):
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    assert schema.lint_record(rec, floor="merged") == []


def test_floor_never_relaxes_status(tmp_path):
    # A merged record linted with a lower floor still gets merged floors.
    rec = _write_record(tmp_path, status="merged", body="## Intent\nx.\n")
    problems = schema.lint_record(rec, floor="in_review")
    assert any("What Was Done" in p for p in problems)


def test_no_floor_keeps_old_behaviour_for_merged_status(tmp_path):
    rec = _write_record(tmp_path, status="merged", extra_fm=_MERGED_FM, body=_FULL_BODY)
    assert schema.lint_record(rec) == []


# ── historian review (require_historian) ──────────────────────────────────
# Every other decision in a record is filed by the same agent that made the
# change it describes, so before this flag a record could reach merge having
# been read by nobody but its author. The flag exists because of a record that
# did exactly that: it passed lint_record clean while carrying four material
# claims that flattered the change and were false. The flag cannot catch THAT
# -- see HISTORIAN_AGENT for the honest bound on what it does buy -- but it
# does turn skipping the reader from a silent omission into a written false
# attribution.

_HISTORIAN_DEC = textwrap.dedent(
    f"""    - decision_id: DEC-{_PR}-2
      agent: context-keeper
      decision: reconciled the record against the diff before merge
      rationale: two claims in the first draft were not supported by the code they cited
    """
)


def test_merged_record_without_a_historian_decision_fails_when_required(tmp_path):
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    problems = schema.lint_record(rec, floor="merged", require_historian=True)
    assert any(schema.HISTORIAN_AGENT in p for p in problems)


def test_the_same_record_passes_once_the_historian_has_filed_one(tmp_path):
    # The mirror. Without it, an unconditional "always fail" satisfies the
    # test above and blocks every PR forever.
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM + _HISTORIAN_DEC, body=_FULL_BODY)
    assert schema.lint_record(rec, floor="merged", require_historian=True) == []


def test_the_requirement_is_off_unless_the_caller_asks(tmp_path):
    # lifecycle-sync and the bare `ctx lint` sweep must keep their verdict on
    # the 352 records written before the rule existed.
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    assert schema.lint_record(rec, floor="merged") == []


@pytest.mark.parametrize("floor", ["status", "open", "in_review"])
def test_a_lower_floor_stays_a_strictly_weaker_check(tmp_path, floor):
    # `ctx lint` prints its floor precisely because a lower one must never be
    # able to reject what `merged` accepts. Bolting the requirement on outside
    # the lifecycle would have broken that.
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    problems = schema.lint_record(rec, floor=schema.normalize_floor(floor), require_historian=True)
    assert not any(schema.HISTORIAN_AGENT in p for p in problems)


def test_a_record_already_at_merged_is_caught_under_a_lower_floor(tmp_path):
    # The floor is a FLOOR, not a ceiling: a record whose own status is merged
    # gets the requirement even when the caller asks for less.
    rec = _write_record(tmp_path, status="merged", extra_fm=_MERGED_FM, body=_FULL_BODY)
    problems = schema.lint_record(rec, floor="open", require_historian=True)
    assert any(schema.HISTORIAN_AGENT in p for p in problems)


def test_the_attribution_match_ignores_case_and_padding(tmp_path):
    # The ledger already carries both `gm` and `GM`; an exact-match test would
    # fail OPEN on a capitalisation.
    dec = _HISTORIAN_DEC.replace("agent: context-keeper", "agent: Context-Keeper ")
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM + dec, body=_FULL_BODY)
    assert schema.lint_record(rec, floor="merged", require_historian=True) == []


def test_a_near_miss_attribution_does_not_satisfy_the_requirement(tmp_path):
    # One canonical spelling, the agent's registered name. Accepting aliases is
    # how the same engineer became `flutter`, `flutter-engineer` and
    # `mobile-engineer` across three records.
    for wrong in ("historian", "ctx-keeper", "context_keeper", "contextkeeper", "keeper"):
        dec = _HISTORIAN_DEC.replace("agent: context-keeper", f"agent: {wrong}")
        rec = _write_record(tmp_path, extra_fm=_MERGED_FM + dec, body=_FULL_BODY)
        problems = schema.lint_record(rec, floor="merged", require_historian=True)
        assert any(schema.HISTORIAN_AGENT in p for p in problems), wrong


# ── duplicate decision ids ───────────────────────────────────────────────────
# `ctx decide` can no longer mint a duplicate, but a hand-edit or a bad merge
# still can, and the damage is invisible: every reader keys on decision_id and
# resolves it to whichever entry came last, so the earlier decision sits in the
# file unreachable. CI is the only place that sees it.
_DUP_FM = textwrap.dedent(
    f"""\
    agent_decisions:
    - decision_id: {_DEC_ID}
      agent: backend
      decision: first
      rationale: because
    - decision_id: {_DEC_ID}
      agent: security
      decision: second, shadowing the first
      rationale: because
    """
)


def test_duplicate_decision_id_is_a_problem(tmp_path):
    rec = _write_record(tmp_path, extra_fm=_DUP_FM, body=_FULL_BODY)
    problems = schema.lint_record(rec)
    assert any(f"duplicate decision_id '{_DEC_ID}'" in p and "agent_decisions" in p for p in problems)


def test_duplicate_outcome_decision_id_is_a_problem(tmp_path):
    extra = textwrap.dedent(
        f"""\
        outcome:
          decisions:
          - decision_id: {_DEC_ID}
            verdict: pending
          - decision_id: {_DEC_ID}
            verdict: good
        """
    )
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM + extra, body=_FULL_BODY)
    problems = schema.lint_record(rec)
    assert any(f"duplicate decision_id '{_DEC_ID}'" in p and "outcome.decisions" in p for p in problems)


def test_distinct_decision_ids_are_fine(tmp_path):
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    assert not any("duplicate" in p for p in schema.lint_record(rec, floor="merged"))


# ── the historian problem must stay separable from every other problem ───────
# The Context Check gate splits this ONE axis out of `lint_record`'s
# flat problem list so it can be a warning on a draft. The split is by
# predicate, so if the message is ever reworded and the predicate is not, the
# gate silently stops deferring -- back to red on every draft push -- or, worse,
# starts deferring a DIFFERENT problem and lets a hollow record through.
def test_is_historian_problem_matches_the_message_lint_record_actually_emits(tmp_path):
    """Pinned to the produced string, not to the constant. Asserting the
    predicate against `HISTORIAN_PROBLEM_PREFIX` would be the tautology: both
    sides would move together and the gate would still break."""
    rec = _write_record(tmp_path, extra_fm=_MERGED_FM, body=_FULL_BODY)
    problems = schema.lint_record(rec, floor="merged", require_historian=True)

    matched = [p for p in problems if schema.is_historian_problem(p)]
    assert len(matched) == 1, f"expected exactly one historian problem in {problems}"
    assert schema.HISTORIAN_AGENT in matched[0]


def test_is_historian_problem_rejects_every_other_lint_problem(tmp_path):
    """The other half: a predicate that matched broadly would demote unrelated
    fatal problems to draft warnings, and the gate would go green on a hollow
    record. This record lacks the merged frontmatter AND the Historian, so both
    kinds of problem are in one list and the split has to be real."""
    rec = _write_record(tmp_path, body=_FULL_BODY)
    problems = schema.lint_record(rec, floor="merged", require_historian=True)

    others = [p for p in problems if not schema.is_historian_problem(p)]
    assert others, "fixture must produce non-historian problems too"
    assert any("test_results" in p for p in others)
    assert all(not schema.is_historian_problem(p) for p in others)
    assert any(schema.is_historian_problem(p) for p in problems)
