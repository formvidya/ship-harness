"""Query-before-dev.

The payoff the whole ledger exists for: before an engineer (human or agent)
touches a service, ``ctx query`` returns a bounded, ranked briefing of the
context that should shape the change — the current architecture pattern, the
decisions that touched this area (with their good/bad verdicts), and the open
loops — so nobody re-derives settled context or repeats a decision that already
failed.

Retrieval (hybrid, the mem0 lesson — pure recency is not enough):
  1. Filter the structured fold (reduce.collect) to the target service + topics
     + any decision whose text/rationale hits the intent/file terms.
  2. Rank: pin [BAD] verdicts and open loops to the top, then by recency.
  3. Compress: a <=400-word briefing (Select + Compress, never a raw dump).

The committed Tier-2 (reduce) is the source of truth; this reads the same fold,
so query and the digests can never disagree. ``--json`` returns the structured
form for an agent's context window.

Side effect: running a query writes ``.claude/context-consulted-{branch}`` so the
pre-dev hook can tell the well-behaved path (queried first) from the one that
skipped it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from reduce import _read_ref_arch, collect, load_all_records

from config import Config

_WORD_BUDGET = 400

# Per-decision caps, so ONE verbose decision cannot eat the whole briefing.
# Measured cause: a 600-word context-keeper audit consumed the entire budget and
# silently truncated the other seven hits, including the only relevant one.
_PER_DECISION_WORDS = 45
_PER_RATIONALE_WORDS = 35

# Relevance weights. A service match is worth more than any single term, but a
# decision from ANOTHER service can still outrank it by matching several rare
# terms -- which is the case that matters most: querying billing-service about
# refresh-token revocation must surface checkout-service's /api/v1/refresh gap.
_SERVICE_WEIGHT = 3.0
_TOPIC_WEIGHT = 3.0

# A cross-service decision needs real lexical signal, not one common word: it
# must match at least this many INFORMATIVE query terms (capped by how many were
# supplied, so a one-word query can still match on one word).
_CROSS_SERVICE_MIN_TERMS = 2

# "Informative" means the term appears in fewer than half the decisions --
# idf > log(2). A word present in most of the corpus ("token", "service",
# "test") cannot discriminate, so it must not be able to admit a hit on its own.
#
# DELIBERATELY A RATIO, NOT AN ABSOLUTE SCORE. The first version of this gate
# was a raw idf floor of 4.0, which quietly meant different things at different
# corpus sizes: it admitted a single rare term against today's 2,234 decisions
# and rejected two rare terms in a 33-decision fixture. A threshold whose
# behaviour depends on how big the ledger happens to be is a threshold that will
# drift out from under this file as the ledger grows.
_INFORMATIVE_IDF = 0.6931471805599453  # math.log(2)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _branch(cfg: Config) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cfg.repo_root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = out or "detached"
    except OSError:
        branch = "unknown"
    # Flatten path-unsafe chars (branch names contain "/") so the marker is a
    # single flat file, not a nested directory. Reader uses the same rule.
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch)


def _write_consulted_marker(cfg: Config) -> None:
    marker = cfg.repo_root / ".claude" / f"context-consulted-{_branch(cfg)}"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except OSError:
        pass  # the marker is an optimisation; never fail the query on it


def _terms(intent: str | None, files: list[str] | None) -> list[str]:
    """Query terms from the intent string and any file paths.

    THE LENGTH FLOOR IS 2, NOT 4. It used to be ``len(w) > 3``, which silently
    discarded exactly the vocabulary this ledger is written in: jti, otp, jwt,
    ttl, pii, csp, k8s, ws, sms, ip, s3. Asking about "jti rotation" searched
    for "rotation" alone. The floor existed to keep stopwords ("the", "and",
    "for") from matching everything -- a job idf now does properly, and does
    better, because it weighs by how common a word actually is in THIS corpus
    rather than by how many letters it has.
    """
    terms: list[str] = []
    if intent:
        terms += [w.lower() for w in intent.split() if len(w) >= 2]
    for f in files or []:
        terms.append(Path(f).stem.lower())
    return terms


def _tokens(text: str) -> set[str]:
    """Word tokens, not substrings.

    ``"token" in "SENTRY_AUTH_TOKEN"`` is true and meaningless. Splitting on
    non-alphanumerics makes the unit of matching a word, which is what idf below
    can then weigh honestly.
    """
    return set(_WORD_RE.findall(text.lower()))


def _idf(decisions) -> dict[str, float]:
    """Inverse document frequency over the decision corpus.

    THE FIX THIS FILE EXISTS FOR. Matching used to be ``any(term in haystack)``,
    which treats every query word as equally informative. It is not: "token"
    appears across hundreds of decisions and carries almost no signal, while
    "revocation" appears in a handful and is nearly a pointer. Without this, a
    query for "refresh token revocation session" is decided by whichever record
    happens to be newest among everything containing the word "token" -- which
    is how a gait GA-gate record became the top hit for an auth question.

    Computed from the fold we are already loading, so it cannot go stale and
    needs no index file on disk.
    """
    import math

    df: dict[str, int] = {}
    n = 0
    for d in decisions:
        n += 1
        for tok in _tokens(f"{d.text} {d.rationale}"):
            df[tok] = df.get(tok, 0) + 1
    if not n:
        return {}
    return {tok: math.log(n / (1 + c)) for tok, c in df.items()}


def _score(d, service: str | None, topics: list[str], terms: list[str], idf: dict[str, float]) -> float:
    """Relevance score. 0 means "not a hit" -- see _keep for the gate."""
    score = 0.0
    if service and service in d.services:
        score += _SERVICE_WEIGHT
    if topics:
        score += _TOPIC_WEIGHT * len(set(topics) & set(d.topics))
    score += _term_score(d, terms, idf)
    return score


def _term_score(d, terms: list[str], idf: dict[str, float]) -> float:
    if not terms:
        return 0.0
    hay = _tokens(f"{d.text} {d.rationale}")
    # DISTINCT terms only: repeating a word in the record must not inflate it.
    return sum(max(idf.get(t, 0.0), 0.0) for t in set(terms) if t in hay)


def _informative_matches(d, terms: list[str], idf: dict[str, float]) -> int:
    """How many DISTINCT query terms this decision matches that carry signal."""
    if not terms:
        return 0
    hay = _tokens(f"{d.text} {d.rationale}")
    return sum(1 for t in set(terms) if t in hay and idf.get(t, 0.0) >= _INFORMATIVE_IDF)


def _keep(d, service: str | None, topics: list[str], terms: list[str], idf: dict[str, float]) -> bool:
    """The filter gate. AND-ish, where the old one was a plain OR.

    The old ``_relevant`` returned True on ANY of service / topic / substring, so
    passing --service did not narrow anything and adding --intent WIDENED the
    result set. Here a requested service is a real constraint: a decision from a
    different service is kept only when it earns its place on topic or on
    several rare terms.
    """
    if _score(d, service, topics, terms, idf) <= 0:
        return False
    if not service:
        return True
    if service in d.services:
        return True
    if topics and set(topics) & set(d.topics):
        return True
    needed = min(_CROSS_SERVICE_MIN_TERMS, len({t for t in terms}))
    return needed > 0 and _informative_matches(d, terms, idf) >= needed


def _rank_key(d):
    # BAD first (don't-repeat is the highest-value signal), then unreconciled,
    # then by recency (newer PR first).
    #
    # NOTE ON WHY THIS IS NOT ENOUGH ON ITS OWN, measured 2026-08-31: of 2,234
    # decisions in this ledger, 2,220 are 'pending', 10 'good', 3 'superseded'
    # and exactly ONE is 'bad'. So this tier is inert for 99.95% of the corpus
    # and, before the relevance score above existed, ranking collapsed to pure
    # recency. Keep it -- a 'bad' verdict really is the highest-value signal when
    # one exists -- but it cannot be the primary key.
    sev = 0 if d.verdict == "bad" else (1 if d.verdict == "unreconciled" else 2)
    return (sev, -d.pr)


def build_briefing(
    cfg: Config, service: str | None, files: list[str] | None, intent: str | None, topics: list[str]
) -> dict:
    records = load_all_records(cfg)
    c = collect(records, cfg, _read_ref_arch(cfg))
    terms = _terms(intent, files)

    # Optional recall index: improves matching + synonym recall via the
    # curated alias map. Absent index -> pure lexical substring matching below.
    bm25: dict[str, float] = {}
    try:
        import index as _index

        idx = _index.load_index(cfg)
        if idx:
            expanded = _index.expand_terms(idx, terms + topics)
            bm25 = _index.bm25_scores(idx, expanded)
    except Exception:  # noqa: BLE001 - the index is a cache; never break query on it
        bm25 = {}

    # Candidates: all non-superseded decisions, PLUS superseded-but-BAD ones —
    # "don't repeat fixed-window" stays the highest-value signal even after a
    # better decision replaced it (PRD section 5).
    candidates = [d for d in c.decisions if d.superseded_by is None or d.verdict == "bad"]

    # idf over the CANDIDATE set, so term weights reflect the corpus actually
    # being searched.
    idf = _idf(candidates)

    hits = [d for d in candidates if _keep(d, service, topics, terms, idf) or bm25.get(d.decision_id, 0) > 0]
    # BAD first, then RELEVANCE, then recency. Relevance is the middle key that
    # was missing: without it the sort fell through to -d.pr and returned the
    # newest record that contained any query word.
    hits.sort(
        key=lambda d: (
            _rank_key(d)[0],
            -(_score(d, service, topics, terms, idf) + bm25.get(d.decision_id, 0.0)),
            -d.pr,
        )
    )

    patterns = []
    if service and service in c.service_patterns:
        latest: dict[str, int] = {}
        for pr, pat, _ref in c.service_patterns[service]:
            latest[pat] = max(latest.get(pat, 0), pr)
        patterns = [f"{pat} (since PR #{pr})" for pat, pr in sorted(latest.items())]

    src_ids = {d.ctx_id for d in hits}
    loops = [ol for ol in c.open_loops if ol.source in src_ids or (service and ol.owner == service)]

    return {
        "service": service,
        "reference_architecture": cfg.reference_architecture,
        "current_patterns": patterns,
        "decisions": [
            {
                "decision_id": d.decision_id,
                "verdict": d.verdict,
                "agent": d.agent,
                "ctx_id": d.ctx_id,
                "decision": d.text,
                "rationale": d.rationale,
                "superseded_by": d.superseded_by,
            }
            for d in hits[:8]
        ],
        "open_loops": [{"item": ol.item, "owner": ol.owner, "kind": ol.kind} for ol in loops[:6]],
        "bad_count": sum(1 for d in hits if d.verdict == "bad"),
    }


def _render_text(b: dict) -> str:
    lines = [f"CONTEXT BRIEFING -- {b['service'] or 'all services'}"]
    if b["reference_architecture"]:
        lines.append(f"Reference architecture: {b['reference_architecture']}")
    if b["current_patterns"]:
        lines.append("Current pattern(s) in use: " + "; ".join(b["current_patterns"]))
    if not b["decisions"] and not b["open_loops"]:
        lines.append("\nNo prior context for this area -- greenfield. Follow platform conventions.")
        return "\n".join(lines)
    if b["decisions"]:
        lines.append("\nKey decisions touching this area (BAD pinned first -- do not repeat):")
        for d in b["decisions"]:
            tag = d["verdict"].upper() if d["verdict"] in ("bad", "good", "mixed", "unreconciled") else d["verdict"]
            mark = "  <-- DO NOT REPEAT" if d["verdict"] == "bad" else ""
            sup = f" (superseded by {d['superseded_by']})" if d.get("superseded_by") else ""
            body = _clip(d["decision"], _PER_DECISION_WORDS)
            lines.append(f"  {d['ctx_id']} {d['decision_id']} [{tag}]{sup} ({d['agent']}): {body}{mark}")
            if d["rationale"]:
                lines.append(f"      why: {_clip(d['rationale'], _PER_RATIONALE_WORDS)}")
    if b["open_loops"]:
        lines.append("\nOpen loops touching this area (you could close one):")
        for ol in b["open_loops"]:
            lines.append(f"  - {ol['item']} (owner: {ol['owner']})")
    text = "\n".join(lines)
    return _compress(text)


def _clip(text: str, budget: int) -> str:
    words = (text or "").split()
    if len(words) <= budget:
        return text or ""
    return " ".join(words[:budget]) + " ..."


def _compress(text: str) -> str:
    """Trim to the word budget WITHOUT destroying line structure.

    The old version did ``" ".join(words[:budget])``, which collapsed the whole
    briefing -- headings, one-decision-per-line, indented rationales -- into a
    single unbroken line the moment it went over budget. Since it was reliably
    over budget, the briefing an agent actually saw was always the unreadable
    form. Dropping whole lines from the end keeps the shape intact.
    """
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        n = len(line.split())
        if used + n > _WORD_BUDGET:
            kept.append("... [truncated; run with --json for the full set]")
            break
        kept.append(line)
        used += n
    return "\n".join(kept)


def run_query(
    cfg: Config, service: str | None, files: list[str] | None, intent: str | None, topics: list[str], as_json: bool
) -> int:
    briefing = build_briefing(cfg, service, files, intent, topics)
    _write_consulted_marker(cfg)
    if as_json:
        print(json.dumps(briefing, indent=2))
    else:
        print(_render_text(briefing))
    return 0
