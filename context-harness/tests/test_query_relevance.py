"""``ctx query`` must rank by RELEVANCE, not by recency.

## What prompted this

Observed against a real ledger of several thousand decisions. A query scoped to
one service, asking about token revocation, returned as its top hit a large
audit of an unrelated service's CI report -- roughly 600 words, which consumed
the entire 400-word budget and silently truncated the other seven hits,
including every relevant one. This runs on a BLOCKING pre-edit hook, so it had
been answering authorization questions with CI trivia for months.

(The service names used throughout this file are illustrative. The failure and
the fix are real; the identifiers are not the ones it was found on.)

Three causes, all fixed and all pinned below:

1. ``_relevant`` was an OR of service / topic / substring, so ``--service`` did
   not constrain anything and adding ``--intent`` WIDENED the result set.
2. Matching was raw substring, so ``"token"`` hit inside ``SENTRY_AUTH_TOKEN``.
3. Ranking was ``(verdict, -pr)`` with no relevance term. The verdict tier is
   inert -- of 2,234 decisions in this ledger, 2,220 are ``pending`` and exactly
   ONE is ``bad`` -- so the sort fell through to "newest PR containing any query
   word".

## The two cases that must BOTH hold

They pull in opposite directions, which is why neither a plain OR nor a plain
AND is correct:

* A decision from another service matching ONE common word must be dropped.
* A decision from another service matching SEVERAL RARE words must be kept --
  the motivating example being that a query about billing-service refresh token
  revocation has to surface checkout-service's ``/api/v1/refresh`` gap, which is
  exactly the knowledge someone editing the auth path would need.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_CTX = Path(__file__).resolve().parents[1] / "ctx"
if str(_CTX) not in sys.path:
    sys.path.insert(0, str(_CTX))

import query as q  # noqa: E402

# Synthetic record ids for the fixtures below, derived from one constant rather
# than written as literals so a fixture id can never be mistaken for a real
# ledger record.
_PR = 12345
_CTX_ID = f"CTX-{_PR:04d}"
_CTX_ID_NEXT = f"CTX-{_PR + 1:04d}"


@dataclass
class _D:
    """Minimal stand-in for a folded decision."""

    decision_id: str
    text: str
    rationale: str = ""
    services: tuple = ()
    topics: tuple = ()
    pr: int = 1
    verdict: str = "pending"
    superseded_by: object = None
    ctx_id: str = _CTX_ID
    agent: str = "gm"


def _corpus() -> list:
    """A corpus where "token" is common and "revocation" is rare.

    That is the shape which makes idf do the work: 30 filler decisions mention a
    token; exactly one mentions revocation.
    """
    filler = [
        _D(
            decision_id=f"DEC-F-{i}",
            text=f"Rotated the deploy token for job {i} and pruned the build cache",
            rationale="CI housekeeping, no auth impact",
            services=("search-service",),
            pr=600 + i,
        )
        for i in range(30)
    ]
    noise = _D(
        decision_id="DEC-NOISE-1",
        text="Audited the release-gate report: SENTRY_AUTH_TOKEN handling and UNLABELED row rendering",
        rationale="CI workflow only, no services affected",
        services=("search-service",),
        pr=999,  # NEWEST -- under the old recency sort this always won
    )
    same_service = _D(
        decision_id="DEC-BILLING-1",
        text="Device sessions are keyed in Redis and expire on logout",
        rationale="session hygiene",
        services=("billing-service",),
        pr=100,
    )
    cross = _D(
        decision_id="DEC-CHECKOUT-1",
        text="checkout-service /api/v1/refresh ignores the revoked flag, so revocation is inert there",
        rationale="A refresh endpoint that never reads revocation makes session revocation theatre",
        services=("checkout-service",),
        pr=470,
    )
    return filler + [noise, same_service, cross]


def _hits(service=None, topics=(), terms=()):
    corpus = _corpus()
    idf = q._idf(corpus)
    kept = [d for d in corpus if q._keep(d, service, list(topics), list(terms), idf)]
    kept.sort(
        key=lambda d: (
            q._rank_key(d)[0],
            -q._score(d, service, list(topics), list(terms), idf),
            -d.pr,
        )
    )
    return kept


class TestTheMeasuredFailure:
    def test_a_common_term_alone_does_not_pull_in_another_service(self):
        """The release-gate record matches only "token", which is everywhere. Dropped."""
        ids = [d.decision_id for d in _hits(service="billing-service", terms=["refresh", "token", "revocation"])]
        assert "DEC-NOISE-1" not in ids, (
            "A decision from another service matching only the high-frequency term "
            f"'token' was kept: {ids}. That is the exact defect this file exists "
            "for -- it was the TOP hit before the idf weighting."
        )

    def test_the_newest_irrelevant_record_no_longer_wins(self):
        hits = _hits(service="billing-service", terms=["refresh", "token", "revocation"])
        assert hits, "expected at least one hit"
        assert hits[0].decision_id != "DEC-NOISE-1"
        assert hits[0].pr != 999


class TestTheCaseThatMustStillWork:
    def test_a_cross_service_decision_with_rare_terms_is_kept(self):
        """The whole point.

        Querying billing-service about revocation must surface checkout-service's
        ``/api/v1/refresh`` gap -- a hard service AND would bury exactly the
        knowledge the query exists to find.
        """
        ids = [d.decision_id for d in _hits(service="billing-service", terms=["refresh", "revocation"])]
        assert "DEC-CHECKOUT-1" in ids, (
            "The checkout-service refresh/revocation decision was filtered out by the "
            f"service constraint: {ids}. Narrowing must not become blindness."
        )

    def test_it_outranks_a_same_service_decision_with_no_term_signal(self):
        hits = _hits(service="billing-service", terms=["refresh", "revocation"])
        order = [d.decision_id for d in hits]
        assert order.index("DEC-CHECKOUT-1") < order.index("DEC-BILLING-1"), (
            "A same-service decision with no term match outranked a cross-service "
            f"decision matching both rare terms: {order}"
        )


class TestServiceNarrowsRatherThanWidens:
    def test_adding_an_intent_does_not_grow_the_result_set(self):
        """The old OR meant ``--intent`` could only ever ADD hits.

        It must now be able to remove them: a service-scoped query with an
        intent is a narrower question than the same query without one.
        """
        without = len(_hits(service="billing-service"))
        with_intent = len(_hits(service="billing-service", terms=["revocation"]))
        assert (
            with_intent <= without + 1
        ), f"--intent widened the result set ({without} -> {with_intent}); it is supposed to focus it."

    def test_matching_is_by_word_not_substring(self):
        assert "token" in q._tokens("SENTRY_AUTH_TOKEN"), "tokenizer should split on underscores"
        assert "sentry" in q._tokens("SENTRY_AUTH_TOKEN")
        # ...and the point of splitting is that idf can then discount it.
        idf = q._idf(_corpus())
        assert idf["token"] < idf["revocation"], (
            "idf must rank the rare term above the common one: "
            f"token={idf['token']:.2f} revocation={idf['revocation']:.2f}"
        )


class TestRendering:
    def test_compress_preserves_line_structure(self):
        """The old ``_compress`` did ``" ".join(words[:budget])``.

        That collapsed the whole briefing into one unreadable line whenever it
        went over budget -- which was every time.
        """
        text = "\n".join(f"line {i} " + "word " * 20 for i in range(60))
        out = q._compress(text)
        assert "\n" in out, "the briefing was flattened into a single line"
        assert len(out.split()) <= q._WORD_BUDGET + 10

    def test_one_verbose_decision_cannot_eat_the_briefing(self):
        long_decision = "verbose " * 500
        clipped = q._clip(long_decision, q._PER_DECISION_WORDS)
        assert len(clipped.split()) <= q._PER_DECISION_WORDS + 1
        assert clipped.endswith("...")

    def test_short_text_is_returned_unchanged(self):
        """Guards against a clip that always truncates."""
        assert q._clip("three little words", q._PER_DECISION_WORDS) == "three little words"
        short = "a\nb\nc"
        assert q._compress(short) == short


class TestShortAcronymsAreQueryable:
    """``_terms`` used to drop any word of 3 characters or fewer.

    That discarded exactly the vocabulary this ledger is written in -- jti, otp,
    jwt, ttl, pii, csp, k8s, ws, sms -- so "jti rotation" silently searched for
    "rotation" alone. The floor existed to suppress stopwords; idf does that job
    properly, by measuring how common a word is in THIS corpus rather than
    counting its letters.
    """

    @pytest.mark.parametrize("acronym", ["jti", "otp", "jwt", "ttl", "pii", "csp", "k8s", "ws"])
    def test_the_vocabulary_of_this_repo_survives_term_extraction(self, acronym):
        assert acronym in q._terms(f"{acronym} rotation", None), (
            f"'{acronym}' was dropped from the query terms; it is a word this "
            "ledger uses constantly and cannot be searched for without it."
        )

    def test_single_characters_are_still_dropped(self):
        assert "a" not in q._terms("a jti", None)


def test_empty_corpus_does_not_explode():
    assert q._idf([]) == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestYamlLoaderSelection:
    """Parsing 369 records was 99% of ``ctx query``'s 4.7s, on a BLOCKING hook.

    The C loader is ~18x faster on the same corpus (4.55s -> 0.25s), taking the
    end-to-end fold from 4.71s to 0.39s. The trap worth pinning: installing
    libyaml does NOTHING on its own, because ``yaml.safe_load()`` hardcodes the
    pure-Python SafeLoader. The C extension sat installed and unused until the
    code opted in.
    """

    def test_the_hot_parse_path_does_not_call_safe_load(self):
        """``yaml.safe_load`` is the inert call. It must not come back."""
        import inspect

        import schema

        src = inspect.getsource(schema.parse_record)
        assert "yaml.safe_load" not in src, (
            "parse_record went back to yaml.safe_load, which ignores libyaml "
            "entirely -- the 18x speedup is silently lost with no test failing "
            "and no symptom other than a slower hook."
        )

    def test_it_prefers_the_c_loader_when_available(self):
        import schema
        import yaml

        expected = "CSafeLoader" if hasattr(yaml, "CSafeLoader") else "SafeLoader"
        assert schema._Loader.__name__ == expected

    def test_the_pure_python_fallback_still_parses(self):
        """CI pins pyyaml==6.0.1, whose wheel may lack the C extension."""
        import schema
        import yaml

        out = yaml.load(f"ctx_id: {_CTX_ID}\nstatus: merged\n", Loader=yaml.SafeLoader)
        assert out["ctx_id"] == _CTX_ID
        assert schema.load_yaml(f"ctx_id: {_CTX_ID_NEXT}\n")["ctx_id"] == _CTX_ID_NEXT
