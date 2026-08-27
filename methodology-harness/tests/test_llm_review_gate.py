"""Unit tests for the LLM-reviewer clearance gate.

This gate is the whole safety argument of the predictive layer, so its pure
decision function is tested exhaustively: no-escalation pass, uncleared fail,
the SELF-CLEAR reject, the STALE-SHA reject, valid clearance, and the policy
toggles (require_non_author / bind_to_sha). Run:
    python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import llm_review_gate as g  # noqa: E402

ESC = "security-review-required"
CLR = "security-cleared"
HEAD_TIME = "2026-06-19T05:00:00Z"


def _decide(labels, event, head_time=HEAD_TIME, author="alice", require_non_author=True, bind_to_sha=True):
    return g.decide(
        labels,
        event,
        head_time,
        author,
        esc_label=ESC,
        clr_label=CLR,
        require_non_author=require_non_author,
        bind_to_sha=bind_to_sha,
    )


def test_no_escalation_label_passes():
    passed, why = _decide(["other-label"], None)
    assert passed and "found nothing blocking" in why


def test_escalation_without_clearance_fails():
    passed, why = _decide([ESC], None)
    assert not passed and "uncleared" in why


def test_valid_nonauthor_clearance_after_head_passes():
    ev = {"actor": "bob", "created_at": "2026-06-19T05:30:00Z"}  # after head
    passed, why = _decide([ESC, CLR], ev, author="alice")
    assert passed and "valid clearance by bob" in why


# ── the two make-or-break rejects ────────────────────────────────────────────
def test_self_clear_by_author_is_rejected():
    ev = {"actor": "alice", "created_at": "2026-06-19T05:30:00Z"}  # author self-clears
    passed, why = _decide([ESC, CLR], ev, author="alice")
    assert not passed and "self-clear" in why.lower()


def test_stale_clearance_before_head_is_rejected():
    ev = {"actor": "bob", "created_at": "2026-06-19T04:00:00Z"}  # BEFORE head -> stale
    passed, why = _decide([ESC, CLR], ev, author="alice")
    assert not passed and "stale" in why.lower()


def test_clearance_label_but_no_event_fails_closed():
    passed, why = _decide([ESC, CLR], None)
    assert not passed and "no labeling event" in why


# ── policy toggles ───────────────────────────────────────────────────────────
def test_require_non_author_false_allows_self_clear():
    ev = {"actor": "alice", "created_at": "2026-06-19T05:30:00Z"}
    passed, _ = _decide([ESC, CLR], ev, author="alice", require_non_author=False)
    assert passed  # solo mode: author may clear


def test_bind_to_sha_false_allows_stale_clearance():
    ev = {"actor": "bob", "created_at": "2026-06-19T04:00:00Z"}  # before head
    passed, _ = _decide([ESC, CLR], ev, author="alice", bind_to_sha=False)
    assert passed  # freshness not enforced


def test_clearance_exactly_at_head_time_passes():
    ev = {"actor": "bob", "created_at": HEAD_TIME}  # == head, not before
    passed, _ = _decide([ESC, CLR], ev, author="alice")
    assert passed


def test_head_time_none_skips_freshness():
    ev = {"actor": "bob", "created_at": ""}  # no timestamp, but head_time None
    passed, _ = _decide([ESC, CLR], ev, head_time=None, author="alice")
    assert passed  # non-author holds; freshness un-checkable -> not enforced


# ── freshness uses INSTANTS, not string compare (the offset-vs-Z bug) ────────
def test_instant_parses_z_and_offset_equivalently():
    assert g._instant("2026-06-20T08:30:00+09:00") == g._instant("2026-06-19T23:30:00Z")
    assert g._instant("garbage") is None and g._instant("") is None and g._instant(None) is None


def test_freshness_uses_instants_not_lexicographic_strings():
    # clearance at 23:31Z is REAL-AFTER a head at 23:30Z written in +01:00 offset
    # form ("2026-06-20T00:30:00+01:00"). A naive string compare would wrongly
    # FAIL ('...23:31:00Z' < '...00:30:00+01:00'); instants give the right PASS.
    ev = {"actor": "bob", "created_at": "2026-06-19T23:31:00Z"}
    passed, _ = _decide([ESC, CLR], ev, head_time="2026-06-20T00:30:00+01:00", author="alice")
    assert passed


def test_freshness_fails_closed_on_unparseable_head():
    ev = {"actor": "bob", "created_at": "2026-06-19T23:31:00Z"}
    passed, why = _decide([ESC, CLR], ev, head_time="not-a-timestamp", author="alice")
    assert not passed and "stale or unverifiable" in why


# ── config plumbing ──────────────────────────────────────────────────────────
def test_approval_cfg_reads_nested_block():
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        raw={"gates": {"security_llm": {"approval": {"escalation_label": "x", "require_non_author": False}}}}
    )
    ap = g.approval_cfg(cfg)
    assert ap["escalation_label"] == "x" and ap["require_non_author"] is False
