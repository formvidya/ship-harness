"""Recall index for query (optional & deferrable).

``ctx query`` works on its own (lexical substring matching over the structured
fold). This module builds an optional, **gitignored, regenerable** index that
gives ``query`` better recall — including synonym recall for the curated topic
vocabulary — without making the portable harness depend on anything heavy.

Two backends, chosen automatically:

  - **lexical (default, zero-dependency):** a BM25 inverted index over each
    decision's text/rationale/topics/service, plus query-time **alias
    expansion** from ``topic_aliases`` in the config (curated: "throttling"
    -> "rate-limiting"). Deterministic. This is what ships and what CI exercises.

  - **embeddings (optional):** if ``sentence-transformers`` is importable, also
    store vectors for true semantic similarity. Never required — if the library
    is absent the index is lexical-only and ``query`` degrades gracefully to it,
    then to plain substring matching if there is no index at all.

The index is a cache, never a source of truth: the committed records (Tier 1)
and the derived Tier-2 are authoritative. ``ctx index`` rebuilds it; deleting
``<index_dir>/`` simply turns recall back to lexical-substring.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from reduce import _read_ref_arch, collect, load_all_records

from config import Config

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_INDEX_FILE = "index.json"


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _doc_text(d) -> str:
    return " ".join([d.text, d.rationale, " ".join(d.topics), " ".join(d.services)])


# ── build ────────────────────────────────────────────────────────────────────
def build_index(cfg: Config, backend: str = "auto") -> dict:
    records = load_all_records(cfg)
    c = collect(records, cfg, _read_ref_arch(cfg))
    decisions = c.decisions

    docs: list[dict] = []
    postings: dict[str, list[list[int]]] = {}  # term -> [[doc_idx, tf], ...]
    doc_len: list[int] = []
    for i, d in enumerate(decisions):
        toks = _tokenize(_doc_text(d))
        doc_len.append(len(toks))
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, n in tf.items():
            postings.setdefault(t, []).append([i, n])
        docs.append({"decision_id": d.decision_id, "ctx_id": d.ctx_id, "pr": d.pr})

    n = len(docs)
    df = {t: len(p) for t, p in postings.items()}
    index = {
        "version": 1,
        "backend": "lexical",
        "N": n,
        "avgdl": (sum(doc_len) / n) if n else 0.0,
        "doc_len": doc_len,
        "docs": docs,
        "df": df,
        "postings": postings,
        "aliases": {str(k).lower(): str(v).lower() for k, v in (cfg.raw.get("topic_aliases") or {}).items()},
    }

    if backend in ("auto", "embeddings"):
        vectors = _try_embeddings([_doc_text(d) for d in decisions])
        if vectors is not None:
            index["backend"] = "embeddings"
            index["vectors"] = vectors

    return index


def _try_embeddings(texts: list[str]):
    """Return list[list[float]] if sentence-transformers is available, else None."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        return None
    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        return [v.tolist() for v in model.encode(texts, normalize_embeddings=True)]
    except Exception:  # noqa: BLE001 - any model/runtime failure -> fall back to lexical
        return None


def write_index(cfg: Config, index: dict) -> Path | None:
    if not cfg.ledger.index_dir:
        return None
    out = cfg.repo_root / cfg.ledger.index_dir / _INDEX_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
    return out


def load_index(cfg: Config) -> dict | None:
    if not cfg.ledger.index_dir:
        return None
    p = cfg.repo_root / cfg.ledger.index_dir / _INDEX_FILE
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ── score (used by query when an index is present) ───────────────────────────
def expand_terms(index: dict, terms: list[str]) -> list[str]:
    aliases = index.get("aliases", {})
    out = list(terms)
    for t in terms:
        canon = aliases.get(t.lower())
        if canon:
            out.extend(_tokenize(canon))
    return out


def bm25_scores(index: dict, terms: list[str], k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    """BM25 score per decision_id for the given (already alias-expanded) terms."""
    n = index.get("N", 0)
    if not n:
        return {}
    avgdl = index.get("avgdl", 0.0) or 1.0
    df = index.get("df", {})
    postings = index.get("postings", {})
    doc_len = index.get("doc_len", [])
    docs = index.get("docs", [])
    scores: dict[int, float] = {}
    for term in {t.lower() for t in terms}:
        plist = postings.get(term)
        if not plist:
            continue
        idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
        for doc_idx, tf in plist:
            dl = doc_len[doc_idx] if doc_idx < len(doc_len) else avgdl
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * (tf * (k1 + 1)) / denom
    return {docs[i]["decision_id"]: s for i, s in scores.items() if i < len(docs)}


# ── command ──────────────────────────────────────────────────────────────────
def run_index(cfg: Config, backend: str = "auto") -> int:
    index = build_index(cfg, backend=backend)
    out = write_index(cfg, index)
    if out is None:
        print("ctx index: no ledger.index_dir configured; nothing written.")
        return 1
    rel = out.relative_to(cfg.repo_root).as_posix()
    print(
        f"ctx index: built {index['backend']} index over {index['N']} decision(s) "
        f"-> {rel} ({len(index.get('aliases', {}))} alias(es))"
    )
    if index["backend"] == "lexical" and backend == "embeddings":
        print("  note: sentence-transformers not installed -> lexical index only (query still improves).")
    return 0
