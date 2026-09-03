"""Context record schema + linter.

A context record is one markdown file per change unit (per PR), with a YAML
frontmatter block followed by prose sections. This module parses that
frontmatter and validates it against the lifecycle-aware required-field rules
for the context-record data model (see ``docs/SCHEMA.md``).

The validation is intentionally a *floor*, not a quality judge:
it kills empty shells (a ``merged`` record with no decisions, no test results,
no risk level) but cannot judge whether a rationale is substantive — that is the
substance funnel's job, layered on top later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("context-harness requires PyYAML: pip install pyyaml") from exc


# Lifecycle states a record moves through. Each adds required fields.
LIFECYCLE = ("open", "in_review", "merged", "deployed", "reconciled")

VALID_RISK = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
VALID_VERDICT = ("pending", "good", "bad", "mixed", "superseded", "unreconciled")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Record:
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @property
    def pr_number(self) -> int | None:
        v = self.frontmatter.get("pr_number")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "open"))

    @property
    def services_affected(self) -> list[str]:
        v = self.frontmatter.get("services_affected") or []
        return list(v) if isinstance(v, list) else [v]

    @property
    def agent_decisions(self) -> list[dict[str, Any]]:
        v = self.frontmatter.get("agent_decisions") or []
        return list(v) if isinstance(v, list) else []


class RecordError(ValueError):
    pass


# ── YAML loader selection ────────────────────────────────────────────────────
# Use libyaml's C loader when the installed PyYAML was built with it.
#
# WHY THIS EXISTS, MEASURED 2026-09-01. Folding all 369 records took 4.7s, and
# 99% of that was this one call -- the fold itself (decisions, topics, patterns)
# is 13ms. `ctx query` runs on a BLOCKING pre-edit hook, so that 4.7s is paid
# before the first edit on every branch, and it grows linearly: ~13s at 1,000
# records, ~32s at 2,500.
#
# Same corpus, same parse, measured back to back:
#     pure-Python SafeLoader   4.55s
#     C CSafeLoader            0.25s      <- 18x
#
# INSTALLING LIBYAML IS NOT ENOUGH ON ITS OWN, which is the trap worth naming:
# `yaml.safe_load()` hardcodes the pure-Python SafeLoader whatever is installed.
# The C extension was present and unused, and the timing was unchanged, until
# the code opted in here. A "fix" applied only to the environment would have
# been inert with nothing to show for it.
#
# Falls back silently when the C extension is absent -- CI pins pyyaml==6.0.1,
# and a wheel without the extension must still work, just slower. Correctness is
# identical: CSafeLoader is the same YAML 1.1 safe subset, implemented in C.
try:
    from yaml import CSafeLoader as _Loader  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - depends on how PyYAML was built
    from yaml import SafeLoader as _Loader


def load_yaml(text: str):
    """Parse a YAML document with the fastest available safe loader."""
    return yaml.load(text, Loader=_Loader)


def parse_record(path: Path) -> Record:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise RecordError(f"{path}: no YAML frontmatter block (expected leading '---').")
    try:
        fm = load_yaml(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise RecordError(f"{path}: frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise RecordError(f"{path}: frontmatter must be a mapping.")
    body = text[m.end() :]
    return Record(path=path, frontmatter=fm, body=body)


# ── the one floor vocabulary ────────────────────────────────────────────────
# Both entry points -- ``ctx lint`` and ``check_context_record.py`` -- resolve
# ``--floor`` through the constants below and share the requirement tables that
# follow. Neither keeps its own list.
#
# Why this is centralized: two PRs each burned a CI cycle because the
# two paths reached *different floors by default*. ``ctx lint --pr N`` defaulted
# to the record's own ``status`` -- ``open`` for every pre-merge record, since
# ``lifecycle-sync`` only flips to ``merged`` after the fact -- while the CI gate
# defaulted to ``merged``. The gap was four frontmatter fields
# (``services_affected``/``agent_decisions``/``test_results``/``risk_level``) and
# two body sections (``## What Was Done``/``## Architecture Used``), so the
# documented local command said PASS on a record CI then rejected. The two
# ``--floor`` vocabularies also had only ``merged`` in common: the CLI accepted
# ``in_review``/``deployed``/``reconciled`` and had no spelling for
# record-native, the gate accepted only ``status``/``merged``.

#: ``--floor status`` means "impose no floor -- lint at the record's own status".
RECORD_NATIVE_FLOOR = "status"

#: The floor to validate against wherever a record is checked *for merge*.
#: Substance is due before merge, not after.
DEFAULT_GATE_FLOOR = "merged"

#: The single ``--floor`` choice list. Both argparsers import this.
FLOOR_CHOICES = (RECORD_NATIVE_FLOOR, *LIFECYCLE)

#: The agent whose decision, present in a record, is the deterministic trace
#: that the Historian read it. The Historian (``.claude/agents/context-keeper``)
#: is the ledger's only author-INDEPENDENT reader: every other decision in a
#: record is filed by the same agent that made the change it describes, so a
#: record with no ``context-keeper`` entry has been checked by nobody but its
#: author.
#:
#: WHAT REQUIRING THIS PROVES, AND WHAT IT DOES NOT. It does not prove a review
#: happened. Nothing deterministic can: the check reads a YAML field and cannot
#: tell who typed it. One observed record is the case in point -- it passed
#: ``lint_record`` cleanly while carrying four material claims that flattered
#: the change and were false, every one of them found by a reading agent and
#: none of them findable here. What the requirement does buy is narrower and
#: still worth the line: skipping the Historian stops being a SILENT OMISSION
#: -- the actual observed failure, where the author simply never invokes it and
#: nothing notices -- and becomes a deliberate act of writing a false
#: attribution into a file that is itself under review.
HISTORIAN_AGENT = "context-keeper"

#: The opening words of the problem :func:`lint_record` emits when that decision
#: is absent. Callers that need to treat the historian axis differently from
#: every other lint problem -- the Context Check gate demotes it to a warning on
#: DRAFT pushes, because the Historian runs at the END of a change and a red
#: that is expected on every draft push is exactly the always-red noise nobody
#: reads -- must be able to pick it out of the flat problem list. The message is
#: BUILT from this constant rather than merely starting with it by convention,
#: so :func:`is_historian_problem` cannot silently stop matching if the wording
#: is reworded later.
HISTORIAN_PROBLEM_PREFIX = f"no agent_decisions entry from '{HISTORIAN_AGENT}'"


# ── lifecycle-aware required fields ─────────────────────────────────────────
# (field name, human description). Sections may live in frontmatter (structured)
# or be required as a non-empty markdown heading in the body.
_REQUIRED_FRONTMATTER = {
    "open": [
        ("ctx_id", "stable record id, of the form CTX-<n> (zero-padded change number)"),
        ("pr_number", "the PR this record belongs to"),
        ("title", "one-line change title"),
        ("status", f"one of {LIFECYCLE}"),
    ],
    "merged": [
        ("services_affected", "code areas this change touched"),
        ("agent_decisions", ">=1 decision with agent + rationale"),
        ("test_results", "structured test outcome"),
        ("risk_level", f"one of {VALID_RISK}"),
    ],
}
# Body sections required as non-empty markdown headings, by lifecycle.
_REQUIRED_SECTIONS = {
    "open": ["Intent"],
    "merged": ["What Was Done", "Architecture Used"],
}


def _lifecycle_at_or_before(status: str) -> list[str]:
    """All lifecycle states up to and including ``status``."""
    if status not in LIFECYCLE:
        return ["open"]
    idx = LIFECYCLE.index(status)
    return list(LIFECYCLE[: idx + 1])


def normalize_floor(floor: str | None) -> str | None:
    """Map a CLI ``--floor`` value to the ``lint_record(floor=...)`` argument.

    ``None`` and ``"status"`` both mean "impose no floor"; every other value is
    a lifecycle state passed straight through. Callers must route through this
    rather than special-casing the sentinel themselves -- that special-casing
    living in only one of the two CLIs is how the floors drifted.
    """
    if floor is None or floor == RECORD_NATIVE_FLOOR:
        return None
    return floor


def requirements_for(state: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The cumulative ``(frontmatter fields, body sections)`` required at ``state``.

    The one public accessor over the requirement tables, so "what does floor X
    require" has exactly one answer in the codebase. ``state`` must be a
    lifecycle state -- pass :data:`RECORD_NATIVE_FLOOR` through
    :func:`normalize_floor` first, since what it requires depends on the record.
    """
    if state not in LIFECYCLE:
        raise ValueError(f"{state!r} is not a lifecycle state; expected one of {LIFECYCLE}")
    fields: list[str] = []
    sections: list[str] = []
    for s in _lifecycle_at_or_before(state):
        fields.extend(name for name, _ in _REQUIRED_FRONTMATTER.get(s, []))
        sections.extend(_REQUIRED_SECTIONS.get(s, []))
    return tuple(fields), tuple(sections)


# A line that is nothing but the ``ctx init`` scaffold placeholder. Without
# this, the scaffold satisfied its own floor: ``_TODO: problem, goals..._`` is
# non-whitespace, so every record passed lint while still being the template
# (observed in the field on an entire 16-record backlog at once).
_PLACEHOLDER_LINE = re.compile(r"^[\s*_>-]*TODO(?=$|[^A-Za-z0-9]).*$", re.IGNORECASE)


def _has_nonempty_section(body: str, heading: str) -> bool:
    # Matches "## Heading" then requires some non-placeholder, non-whitespace
    # content before the next heading or EOF.
    pat = re.compile(
        rf"^#{{1,3}}\s+{re.escape(heading)}\b.*?\n(.*?)(?=^#{{1,3}}\s|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    m = pat.search(body)
    if not m:
        return False
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    return any(not _PLACEHOLDER_LINE.match(ln) for ln in lines)


def lint_record(path: Path, floor: str | None = None, *, require_historian: bool = False) -> list[str]:
    """Return a list of human-readable problems. Empty list == valid.

    ``floor`` optionally names a lifecycle state whose requirements apply even
    if the record's own ``status`` is earlier. The PR gate passes
    ``floor="merged"`` so a record must be substance-complete *before* merge —
    otherwise the merged-level floors only ever bind after the fact (or, as
    observed, never, because nothing advanced ``status``).

    ``require_historian`` additionally demands an :data:`HISTORIAN_AGENT`
    decision. It is a keyword flag rather than a merged-floor entry in
    :data:`_REQUIRED_FRONTMATTER` on purpose: the requirement is about the
    process a NEW change goes through, not a property the 351 records written
    before it existed should be retroactively judged against. Folding it into
    the floor would have put ~347 of them into ``lifecycle-sync``'s curation
    queue and turned a bare ``ctx lint`` from 325/351 to 4/351 -- a report
    nobody would read twice, which is how a gate becomes decoration. It still
    only bites once the effective lifecycle reaches ``merged``, so "a lower
    floor is a strictly weaker check" stays true.
    """
    try:
        rec = parse_record(path)
    except RecordError as e:
        return [str(e)]

    problems: list[str] = []
    fm = rec.frontmatter
    status = rec.status
    if status not in LIFECYCLE:
        problems.append(f"status '{status}' is not one of {LIFECYCLE}")

    # Effective lifecycle = the further of (record status, caller-imposed floor).
    effective = status
    if floor in LIFECYCLE:
        cur_idx = LIFECYCLE.index(status) if status in LIFECYCLE else 0
        if LIFECYCLE.index(floor) > cur_idx:
            effective = floor
    floor_note = f" (floor '{floor}')" if effective != status else ""

    # Frontmatter floors, cumulative across lifecycle.
    for state in _lifecycle_at_or_before(effective):
        for fieldname, desc in _REQUIRED_FRONTMATTER.get(state, []):
            val = fm.get(fieldname)
            if val is None or (isinstance(val, (list, str, dict)) and len(val) == 0):
                problems.append(
                    f"missing/empty required field '{fieldname}' ({desc}) for status '{status}'{floor_note}"
                )

    # Body section floors. A section whose content is only the ctx-init
    # placeholder (_TODO..._) counts as empty.
    for state in _lifecycle_at_or_before(effective):
        for heading in _REQUIRED_SECTIONS.get(state, []):
            if not _has_nonempty_section(rec.body, heading):
                problems.append(f"missing/empty required section '## {heading}' for status '{status}'{floor_note}")

    # Targeted value checks.
    if "risk_level" in fm and str(fm["risk_level"]) not in VALID_RISK:
        problems.append(f"risk_level '{fm['risk_level']}' is not one of {VALID_RISK}")

    for dec in rec.agent_decisions:
        if not isinstance(dec, dict):
            problems.append("each agent_decisions entry must be a mapping")
            continue
        for k in ("decision_id", "agent", "decision", "rationale"):
            if not dec.get(k):
                problems.append(f"agent_decision {dec.get('decision_id', '?')} missing '{k}'")

    # Historian review. Gated on the merged floor so the weaker-floor invariant
    # holds; see the flag's rationale in the docstring and the constant's
    # comment for the (narrow) claim this actually supports.
    if require_historian and "merged" in _lifecycle_at_or_before(effective):
        if not _has_historian_decision(rec.agent_decisions):
            problems.append(
                f"{HISTORIAN_PROBLEM_PREFIX}: this record has been read by "
                f"nobody but its author. Run the context-keeper agent over the PR and let it file "
                f"its own decision -- do not write one on its behalf."
            )

    # Closed-loop outcome verdict sanity.
    outcome = fm.get("outcome") or {}
    for d in outcome.get("decisions") or []:
        v = (d or {}).get("verdict")
        if v is not None and v not in VALID_VERDICT:
            problems.append(f"outcome decision verdict '{v}' is not one of {VALID_VERDICT}")

    # A decision_id must name exactly one decision. `ctx decide` can no longer
    # mint a duplicate, but a hand-edit or a bad merge still can, and the damage
    # is invisible in a working tree: the Tier-2 fold keys verdicts by
    # decision_id and every reader resolves the id to whichever entry came last,
    # so the earlier decision is present in the file yet unreachable.
    problems.extend(_duplicate_id_problems(rec.agent_decisions, "agent_decisions"))
    problems.extend(_duplicate_id_problems(outcome.get("decisions") or [], "outcome.decisions"))

    return problems


def is_historian_problem(problem: str) -> bool:
    """True if ``problem`` is the missing-Historian-decision problem.

    Lets a caller split the historian axis out of ``lint_record``'s flat list
    without re-deriving when the requirement bites -- the floor gating above is
    the single definition, and a second copy of it in the gate is exactly the
    local-vs-CI drift centralising that definition here was meant to prevent.
    """
    return problem.startswith(HISTORIAN_PROBLEM_PREFIX)


def _has_historian_decision(decisions: Any) -> bool:
    """True if any decision is attributed to :data:`HISTORIAN_AGENT`.

    Case- and whitespace-insensitive: the ledger already carries both ``gm``
    and ``GM``, so an exact-match test would fail open on a capitalisation.
    Deliberately NOT alias-tolerant -- one canonical spelling, the agent's
    registered name, or the set of accepted spellings drifts the way the
    ``flutter`` / ``flutter-engineer`` / ``mobile-engineer`` attributions did.
    """
    for dec in decisions if isinstance(decisions, list) else []:
        if isinstance(dec, dict) and str(dec.get("agent") or "").strip().lower() == HISTORIAN_AGENT:
            return True
    return False


def _duplicate_id_problems(entries: Any, where: str) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("decision_id"):
            continue
        ident = str(e["decision_id"])
        if ident in seen and ident not in dupes:
            dupes.append(ident)
        seen.add(ident)
    return [f"duplicate decision_id '{d}' in {where} -- each id must name exactly one decision" for d in dupes]
