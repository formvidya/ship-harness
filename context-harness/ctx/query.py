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
    terms: list[str] = []
    if intent:
        terms += [w.lower() for w in intent.split() if len(w) > 3]
    for f in files or []:
        terms.append(Path(f).stem.lower())
    return terms


def _relevant(d, service: str | None, topics: list[str], terms: list[str]) -> bool:
    if service and service in d.services:
        return True
    if topics and set(topics) & set(d.topics):
        return True
    if terms:
        hay = f"{d.text} {d.rationale}".lower()
        if any(t in hay for t in terms):
            return True
    return False


def _rank_key(d):
    # BAD first (don't-repeat is the highest-value signal), then unreconciled,
    # then by recency (newer PR first).
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
    hits = [d for d in candidates if _relevant(d, service, topics, terms) or bm25.get(d.decision_id, 0) > 0]
    # BAD first, then by index relevance (when present), then recency.
    hits.sort(key=lambda d: (_rank_key(d)[0], -bm25.get(d.decision_id, 0.0), -d.pr))

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
            lines.append(f"  {d['ctx_id']} {d['decision_id']} [{tag}]{sup} ({d['agent']}): {d['decision']}{mark}")
            if d["rationale"]:
                lines.append(f"      why: {d['rationale']}")
    if b["open_loops"]:
        lines.append("\nOpen loops touching this area (you could close one):")
        for ol in b["open_loops"]:
            lines.append(f"  - {ol['item']} (owner: {ol['owner']})")
    text = "\n".join(lines)
    return _compress(text)


def _compress(text: str) -> str:
    words = text.split()
    if len(words) <= _WORD_BUDGET:
        return text
    return " ".join(words[:_WORD_BUDGET]) + " ... [truncated; run with --json for full set]"


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
