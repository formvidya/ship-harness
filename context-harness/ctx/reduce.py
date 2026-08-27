"""Tier-2 synthesis.

The per-PR records (Tier 1) are the immutable event log. This module derives the
**bounded** Tier-2 layer you actually query:

  - per-service digests   docs/context/digests/<service>.md  (current patterns + standing decisions + open loops)
  - decision registry     DECISION_REGISTRY.md               (topic-keyed, deduped, closed-loop)
  - open-loops board       OPEN_LOOPS.md                      (open carry-forwards + architecture-drift flags)
  - topics                 TOPICS.md                          (seed vocab + off-list curation queue)

Determinism: :func:`derive` is a pure function of (records, config,
reference-architecture text). Same inputs -> byte-identical output. That is what
lets ``reconcile`` re-derive Tier 2 from scratch and self-heal a bad fold.
``reconcile`` adds the only time-dependent step — flagging decisions
``unreconciled`` once they pass their risk-tuned horizon.

Bounded by subtraction: superseded decisions and closed carry-forwards drop out
of the active digests (they remain in the Tier-1 archive). So Tier 2 scales with
the number of services and live topics, never with the number of PRs.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from schema import parse_record

from config import Config

UNTAGGED = "(untagged)"
_VERDICT_LABEL = {
    "good": "GOOD",
    "bad": "BAD",
    "mixed": "MIXED",
    "superseded": "SUPERSEDED",
    "pending": "pending",
    "unreconciled": "UNRECONCILED",
}


@dataclass
class Decision:
    decision_id: str
    agent: str
    text: str
    rationale: str
    ctx_id: str
    pr: int
    topics: tuple[str, ...]
    services: tuple[str, ...]
    verdict: str = "pending"
    superseded_by: str | None = None
    supersedes: str | None = None


@dataclass
class OpenLoop:
    item: str
    owner: str
    source: str  # ctx_id
    kind: str = "carry_forward"  # or "architecture_drift"


@dataclass
class Tier2:
    registry: str
    open_loops: str
    topics: str
    digests: dict[str, str] = field(default_factory=dict)  # service -> markdown
    # reconcile extras (non-derived / advisory)
    off_list_topics: tuple[str, ...] = ()
    unreconciled: tuple[str, ...] = ()
    drift_count: int = 0


# ── reading Tier 1 ───────────────────────────────────────────────────────────
def load_all_records(cfg: Config) -> list:
    rec_dir = cfg.records_dir()
    if not rec_dir.is_dir():
        return []
    out = []
    for p in sorted(rec_dir.glob("CTX-*.md")):
        try:
            out.append(parse_record(p))
        except Exception:  # noqa: BLE001 - a malformed record must not break synthesis
            continue
    # Stable order: by pr_number, then filename.
    return sorted(out, key=lambda r: (r.pr_number or 0, r.path.name))


def _fm_get(fm: dict, dotted: str, default=None):
    cur = fm
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _as_list(value) -> tuple[str, ...]:
    """Coerce a frontmatter field to a tuple of strings, defensively.

    A malformed record (e.g. ``topics`` stored as a bare string instead of a
    list) must never explode into per-character 'topics'. A string becomes a
    single-element tuple; a list is filtered to truthy strings; anything else
    is empty.
    """
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None and str(v).strip())
    return ()


# ── the pure derivation ──────────────────────────────────────────────────────
@dataclass
class Collected:
    """Structured fold of the Tier-1 records. Shared by derive() and query()."""

    decisions: list[Decision]
    open_loops: list[OpenLoop]
    service_patterns: dict[str, list[tuple[int, str, str]]]
    all_topics: set[str]


def collect(records: list, cfg: Config, ref_arch_text: str = "") -> Collected:
    """Fold records into structured data (decisions, loops, patterns, topics).

    Deterministic. derive() renders Tier-2 markdown from this; query() filters
    and ranks it — both off the same single fold, so they can never disagree.
    """
    decisions: list[Decision] = []
    verdict_of: dict[str, str] = {}
    open_loops: list[OpenLoop] = []
    service_patterns: dict[str, list[tuple[int, str, str]]] = {}  # svc -> [(pr, pattern, arch_ref)]
    all_topics: set[str] = set()

    for rec in records:
        fm = rec.frontmatter
        ctx_id = str(fm.get("ctx_id", f"CTX-{rec.pr_number or 0:04d}"))
        pr = rec.pr_number or 0
        topics = _as_list(fm.get("topics"))
        services = _as_list(rec.services_affected)
        all_topics.update(topics)

        # verdicts (closed-loop), keyed by decision_id
        for d in _fm_get(fm, "outcome.decisions", []) or []:
            if isinstance(d, dict) and d.get("decision_id"):
                verdict_of[str(d["decision_id"])] = str(d.get("verdict") or "pending")

        for dec in rec.agent_decisions:
            if not isinstance(dec, dict) or not dec.get("decision_id"):
                continue
            decisions.append(
                Decision(
                    decision_id=str(dec["decision_id"]),
                    agent=str(dec.get("agent", "?")),
                    text=str(dec.get("decision", "")),
                    rationale=str(dec.get("rationale", "")),
                    ctx_id=ctx_id,
                    pr=pr,
                    topics=topics,
                    services=services,
                    supersedes=(str(dec["supersedes"]) if dec.get("supersedes") else None),
                )
            )

        # per-service architecture patterns
        pattern = _fm_get(fm, "architecture_used.pattern_ref")
        arch_ref = _fm_get(fm, "architecture_used.arch_doc_ref", "")
        if pattern:
            for svc in services or (UNTAGGED,):
                service_patterns.setdefault(svc, []).append((pr, str(pattern), str(arch_ref or "")))

        # open carry-forwards
        for cf in _fm_get(fm, "build_retro.carry_forward", []) or []:
            if isinstance(cf, dict) and str(cf.get("status", "open")) == "open" and cf.get("item"):
                open_loops.append(OpenLoop(str(cf["item"]), str(cf.get("owner", "?")), ctx_id))

        # architecture-drift detector
        drift = (
            _fm_get(fm, "architecture_used.establishes_pattern")
            or _fm_get(fm, "architecture_used.changes_pattern")
            or _fm_get(fm, "architecture_used.touches_trust_boundary")
        )
        if drift and pattern and ref_arch_text and str(pattern) not in ref_arch_text:
            open_loops.append(
                OpenLoop(
                    item=f"{ctx_id} (PR #{pr}) introduced/changed pattern '{pattern}' "
                    f"not reflected in {cfg.reference_architecture or 'the reference architecture'}",
                    owner="docs",
                    source=ctx_id,
                    kind="architecture_drift",
                )
            )

    # resolve verdicts + supersession (orthogonal axes: a decision can be BOTH
    # bad AND superseded — we must not lose the "don't repeat this" verdict when
    # a later decision replaces it; that lesson is the whole point of `query`).
    by_id = {d.decision_id: d for d in decisions}
    for d in decisions:
        d.verdict = verdict_of.get(d.decision_id, "pending")
    for d in decisions:
        if d.supersedes and d.supersedes in by_id:
            by_id[d.supersedes].superseded_by = d.decision_id

    return Collected(decisions, open_loops, service_patterns, all_topics)


def derive(records: list, cfg: Config, ref_arch_text: str = "") -> Tier2:
    c = collect(records, cfg, ref_arch_text)
    return Tier2(
        registry=_render_registry(c.decisions),
        open_loops=_render_open_loops(c.open_loops),
        topics=_render_topics(cfg, c.all_topics),
        digests=_render_digests(cfg, c.decisions, c.service_patterns, c.open_loops),
        off_list_topics=tuple(sorted(t for t in c.all_topics if t not in set(cfg.topic_seeds))),
        drift_count=sum(1 for ol in c.open_loops if ol.kind == "architecture_drift"),
    )


# ── renderers (all ASCII; Windows-safe) ──────────────────────────────────────
def _v(d: Decision) -> str:
    return _VERDICT_LABEL.get(d.verdict, d.verdict)


def _render_registry(decisions: list[Decision]) -> str:
    by_topic: dict[str, list[Decision]] = {}
    for d in decisions:
        for t in d.topics or (UNTAGGED,):
            by_topic.setdefault(t, []).append(d)
    lines = [
        "# Decision Registry (Tier 2 — generated by `ctx reduce`/`reconcile`; do not hand-edit)",
        "",
        "Topic-keyed, deduped, closed-loop. Superseded decisions are retained but marked.",
        "",
    ]
    for topic in sorted(by_topic):
        lines.append(f"## {topic}")
        live = [d for d in by_topic[topic] if d.superseded_by is None]
        dead = [d for d in by_topic[topic] if d.superseded_by is not None]
        for d in sorted(live, key=lambda x: x.pr):
            lines.append(f"- **{d.decision_id}** [{_v(d)}] ({d.ctx_id}, {d.agent}): {d.text}")
            if d.rationale:
                lines.append(f"  - why: {d.rationale}")
        for d in sorted(dead, key=lambda x: x.pr):
            # preserve the real verdict (e.g. BAD) alongside the supersession note
            lines.append(f"- ~~{d.decision_id}~~ [{_v(d)}, superseded by {d.superseded_by}] ({d.ctx_id}): {d.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_open_loops(loops: list[OpenLoop]) -> str:
    lines = [
        "# Open Loops (Tier 2 — generated; open items only)",
        "",
        "Closed carry-forwards drop off automatically on the next reduce/reconcile.",
        "",
        "| Item | Owner | Source | Kind |",
        "|------|-------|--------|------|",
    ]
    for ol in sorted(loops, key=lambda x: (x.kind, x.source)):
        item = ol.item.replace("|", "\\|")
        lines.append(f"| {item} | {ol.owner} | {ol.source} | {ol.kind} |")
    if not loops:
        lines.append("| _none_ | | | |")
    return "\n".join(lines) + "\n"


def _render_topics(cfg: Config, found: set[str]) -> str:
    seeds = set(cfg.topic_seeds)
    off_list = sorted(found - seeds)
    lines = [
        "# Topics (Tier 2 — seed vocabulary + curation queue)",
        "",
        "## Canonical (seed)",
    ]
    lines += [f"- {t}" for t in sorted(seeds)] or ["- _none configured_"]
    lines += ["", "## Off-list — pending curation (promote / merge-as-alias / keep)"]
    lines += [f"- {t}" for t in off_list] or ["- _none_"]
    return "\n".join(lines) + "\n"


def _render_digests(cfg, decisions, service_patterns, open_loops) -> dict[str, str]:
    services = set(service_patterns) | {s for d in decisions for s in (d.services or ())}
    services.discard(UNTAGGED)
    ref = cfg.reference_architecture or "(reference architecture not configured)"
    out: dict[str, str] = {}
    for svc in sorted(services):
        pats = service_patterns.get(svc, [])
        latest: dict[str, tuple[int, str]] = {}
        for pr, pattern, arch_ref in pats:
            if pattern not in latest or pr >= latest[pattern][0]:
                latest[pattern] = (pr, arch_ref)
        svc_decisions = [d for d in decisions if svc in (d.services or ()) and d.superseded_by is None]
        svc_loops = [ol for ol in open_loops if any(svc in d.services for d in decisions if d.ctx_id == ol.source)]

        lines = [
            f"# {svc} — context digest (Tier 2 — generated; do not hand-edit)",
            "",
            f"Reference architecture: see `{ref}` (the canonical 'what IS our architecture'; "
            "this digest is the 'how we got here').",
            "",
            "## Current patterns in use",
        ]
        if latest:
            for pattern, (pr, arch_ref) in sorted(latest.items()):
                link = f" — {arch_ref}" if arch_ref else ""
                lines.append(f"- {pattern} (since PR #{pr}){link}")
        else:
            lines.append("- _none recorded_")
        lines += ["", "## Standing decisions (non-superseded)"]
        if svc_decisions:
            for d in sorted(svc_decisions, key=lambda x: (x.verdict != "bad", x.pr)):
                lines.append(f"- **{d.decision_id}** [{_v(d)}] ({d.agent}): {d.text}")
        else:
            lines.append("- _none recorded_")
        lines += ["", "## Open loops"]
        lines += [f"- {ol.item} (owner: {ol.owner})" for ol in svc_loops] or ["- _none_"]
        out[svc] = "\n".join(lines) + "\n"
    return out


# ── writing ──────────────────────────────────────────────────────────────────
def write_tier2(cfg: Config, t2: Tier2) -> list[str]:
    written: list[str] = []

    def _w(relpath: str | None, content: str):
        if not relpath:
            return
        path = cfg.repo_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relpath)

    _w(cfg.ledger.registry, t2.registry)
    _w(cfg.ledger.open_loops, t2.open_loops)
    _w(cfg.ledger.topics, t2.topics)
    if cfg.ledger.digests_dir:
        for svc, md in t2.digests.items():
            safe = svc.replace("/", "-")
            _w(f"{cfg.ledger.digests_dir}/{safe}.md", md)
    return written


def _read_ref_arch(cfg: Config) -> str:
    if not cfg.reference_architecture:
        return ""
    p = cfg.repo_root / cfg.reference_architecture
    return p.read_text(encoding="utf-8") if p.is_file() else ""


# ── command entry points (called from ctx.py) ────────────────────────────────
def run_reduce(cfg: Config, pr: int | None) -> int:
    records = load_all_records(cfg)
    if pr is not None:
        if not cfg.record_path(pr).is_file():
            print(f"ctx reduce: no record for PR {pr} (run `ctx init --pr {pr}` first).")
            return 1
    t2 = derive(records, cfg, _read_ref_arch(cfg))
    written = write_tier2(cfg, t2)
    print(f"ctx reduce: folded {len(records)} record(s) -> {len(written)} Tier-2 file(s)")
    if t2.drift_count:
        print(f"  [!] {t2.drift_count} architecture-drift flag(s) in OPEN_LOOPS (owner: docs)")
    return 0


def run_reconcile(cfg: Config, today: _dt.date | None = None) -> int:
    records = load_all_records(cfg)
    t2 = derive(records, cfg, _read_ref_arch(cfg))
    write_tier2(cfg, t2)
    today = today or _dt.date.today()
    unrec = _flag_unreconciled(cfg, records, today)
    print(f"ctx reconcile: re-derived Tier 2 from {len(records)} record(s)")
    print(
        f"  off-list topics pending curation: {len(t2.off_list_topics)}"
        + (f" ({', '.join(t2.off_list_topics)})" if t2.off_list_topics else "")
    )
    print(f"  architecture-drift flags: {t2.drift_count}")
    print(f"  decisions past horizon -> unreconciled: {len(unrec)}")
    for u in unrec:
        print(f"    - {u}")
    return 0


def _horizon_days(cfg: Config, risk: str) -> int:
    spec = (cfg.risk_policy.get(risk) or {}).get("horizon", "merge+30d")
    # spec like "deploy+14d" / "merge+30d"; we use the day count for a coarse check
    try:
        return int(spec.split("+")[1].rstrip("d"))
    except (IndexError, ValueError):
        return 30


def _flag_unreconciled(cfg: Config, records: list, today: _dt.date) -> list[str]:
    out: list[str] = []
    for rec in records:
        fm = rec.frontmatter
        risk = str(fm.get("risk_level", "LOW"))
        horizon = _horizon_days(cfg, risk)
        base_date = fm.get("date_merged") or fm.get("date_opened")
        if not base_date:
            continue
        try:
            d0 = _dt.date.fromisoformat(str(base_date))
        except ValueError:
            continue
        if (today - d0).days < horizon:
            continue
        for d in _fm_get(fm, "outcome.decisions", []) or []:
            if isinstance(d, dict) and str(d.get("verdict")) == "pending":
                out.append(
                    f"{d.get('decision_id')} ({fm.get('ctx_id')}, risk={risk}, "
                    f"{(today - d0).days}d > {horizon}d horizon)"
                )
    return out
