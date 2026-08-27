"""Unit tests for the context-record schema floors (fix/ctx-substance-floors).

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
