#!/usr/bin/env python3
"""``ctx`` — the context-harness CLI.

Generic engine; all project specifics come from ``.context/config.yml``.
The event-layer commands create and validate records:

    ctx init   --pr N --title "..." [--prd <spec-id>] [--service X ...]
    ctx set    --pr N <dotted.key>=<value>
    ctx decide --pr N --agent A --decision "..." --rationale "..." [--id DEC-N-k]
    ctx decide --pr N --update DEC-N-k --agent A --decision "..." --rationale "..."
    ctx lint   [--pr N | <path>]

``decide`` only ever APPENDS. Revising an existing decision requires the
explicit ``--update DEC-N-k`` flag; there is no silent update path, because a
record that is edited long after its PR merged (the carry-forward pattern) shows
an in-place overwrite as a clean working tree.

``query / index / reduce / reconcile`` add the Tier-2 synthesis + retrieval
layer on top of those records.

Records are markdown with YAML frontmatter; this CLI edits the frontmatter
structurally (round-tripped through PyYAML) and leaves prose sections intact.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - environment dependent
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("context-harness requires PyYAML: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import (  # noqa: E402
    DEFAULT_GATE_FLOOR,
    FLOOR_CHOICES,
    FRONTMATTER_RE,
    HISTORIAN_AGENT,
    RECORD_NATIVE_FLOOR,
    lint_record,
    normalize_floor,
    requirements_for,
)

from config import Config, load_config  # noqa: E402


# ── record read/write ───────────────────────────────────────────────────────
def _parse(raw: bytes) -> tuple[dict, str]:
    """Split record bytes into (frontmatter, body), normalising line endings.

    Split out from :func:`_read` so a caller holding a byte snapshot can parse
    *those* bytes rather than re-reading the file -- see ``_decide_locked``.

    Newlines are normalised to ``\\n`` here, and :func:`_write` writes ``\\n``
    without platform translation. Two distinct defects make that necessary:

    1. Reading BYTES (which this function must do, so the compare-and-swap
       snapshot and the parse see the same content) skips the universal-newline
       translation ``read_text`` used to apply. Paired with ``write_text``'s
       ``\\n``->``\\r\\n`` translation, a CRLF body round-tripped to ``\\r\\r\\n``
       and then ``\\r\\r\\r\\n``. Measured 129 occurrences in a single record
       after one ``ctx verdict`` -- introduced by the byte-read, not inherited.
    2. Pre-existing and separate: ``write_text`` rewrote pure-LF records to CRLF
       on EVERY write, against this repo's ``.gitattributes`` (``* text=auto
       eol=lf``). Measured 0 -> 5 CR bytes on one pass over an LF record.

    Normalising on read (rather than only fixing the write) makes the repair
    idempotent and self-healing for records already damaged, and keeps a
    record's diff to the lines that actually changed -- a whole-file diff is
    what hides an overwrite, which is the failure this module is about.
    """
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end() :]


def _read(path: Path) -> tuple[dict, str]:
    if not path.is_file():
        return {}, ""
    return _parse(path.read_bytes())


def _write(path: Path, frontmatter: dict, body: str) -> None:
    """Serialise the record atomically: a crash mid-write must not truncate it.

    ``write_text`` truncates first and writes second, so an interrupt between the
    two leaves a half-record. Writing a sibling temp file and ``os.replace``-ing
    it over the target makes the swap atomic on both POSIX and Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # newline="\n": no platform translation. Records are LF by repo policy
        # (.gitattributes `* text=auto eol=lf`), and write_text would otherwise
        # rewrite every one of them to CRLF on each edit.
        tmp.write_text(f"---\n{fm}---\n{body}", encoding="utf-8", newline="\n")
        # Windows raises PermissionError if any process holds the target open
        # (an editor, a sync agent, a virus scanner). Retry briefly, then let the
        # caller's guard report it rather than dying with a traceback.
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            tmp.unlink()


# How long to wait for another process to finish its read-modify-write, and how
# long a lock may sit before it is presumed abandoned by a killed agent.
# Overridable so tests do not have to burn the real timeout, and so an operator
# on a slow filesystem can raise it without editing the tool.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


_LOCK_TIMEOUT_S = _env_float("CTX_LOCK_TIMEOUT_S", 15.0)
_LOCK_STALE_S = _env_float("CTX_LOCK_STALE_S", 120.0)


@contextmanager
def _record_lock(path: Path, timeout: float = _LOCK_TIMEOUT_S):
    """Serialise the whole read-modify-write span for one record.

    A compare-and-swap on its own does NOT prevent lost updates here, and
    measuring it is what showed the difference: two ``ctx decide`` processes
    started together both read the pre-write record, both pass the byte
    comparison (neither has written yet), and the later write silently drops the
    earlier agent's decision. A CAS only catches a writer that lands *between*
    another's read and write -- a narrow window, not the one the parallel
    support squad actually hits. Only holding a lock across read->modify->write
    closes it, so that is what this does; the CAS is kept as a cheap backstop
    against writers that do not take the lock (a hand-edit, an external tool).

    ``O_CREAT | O_EXCL`` is the portable atomic test-and-set. A lock older than
    ``_LOCK_STALE_S`` is presumed abandoned by a killed agent and broken, so an
    interrupted turn cannot wedge the ledger shut.

    Stale locks are broken by ATOMIC RENAME, not by unlink. Two waiters that both
    see the same stale lock would otherwise both unlink and both proceed -- and
    the second unlink would remove the *fresh* lock the first had just taken.
    ``os.replace`` gives the steal a single winner: the loser's rename finds
    nothing and it re-loops. The age test uses ``abs()`` so a future-dated mtime
    (clock skew, a VM snapshot restore, a differently-clocked mount) reads as
    stale rather than wedging the record until the clock catches up.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # holder released it between the open and the stat
            if abs(age) > _LOCK_STALE_S:
                print(f"ctx: breaking stale lock {lock.name} ({int(age)}s old; holder presumed gone)")
                try:
                    os.replace(lock, lock.with_name(f"{lock.name}.stale.{os.getpid()}"))
                    lock.with_name(f"{lock.name}.stale.{os.getpid()}").unlink(missing_ok=True)
                except OSError:
                    pass  # another waiter won the steal; re-loop and contend normally
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{lock.name} held by another process for >{timeout:.0f}s")
            time.sleep(0.02)
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)  # never leak the descriptor if the write fails
        yield
    finally:
        lock.unlink(missing_ok=True)


def _set_dotted(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~"):
        return None
    # YAML/JSON-style lists and maps: `[a, b]` / `{k: v}` round-trip to real
    # structures so `ctx set topics='[a, b]'` stores a list, not a string.
    stripped = value.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return yaml.safe_load(stripped)
        except yaml.YAMLError:
            return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


# ── decision-id allocation ──────────────────────────────────────────────────
# Leading digits of the per-record counter. Hand-curated ids carry suffixes
# (``DEC-<pr>-4b`` is a real shape), so the counter is the leading run of digits
# and the full string is what matters for uniqueness. Bounded at 15 digits
# because an unbounded ``int()`` raises ValueError past CPython's 4300-digit
# conversion limit -- a hand-edited id should be ignored, not crash the tool.
_DEC_COUNTER_RE = re.compile(r"^(\d{1,15})(?!\d)")


def _decision_ids_in_use(fm: dict) -> set[str]:
    """Every decision id the record mentions, gathered from ALL id-bearing blocks.

    Reading ids from ``agent_decisions`` alone is what made allocation unsafe.
    Records are routinely curated after their PR merged, and that editing prunes
    or renumbers entries in ``agent_decisions`` while the matching
    ``outcome.decisions`` verdict row -- and any ``supersedes`` back-reference --
    survives. An id that still appears anywhere is spoken for: re-minting it
    silently re-points every reference that already resolves to it.
    """
    used: set[str] = set()
    for dec in fm.get("agent_decisions") or []:
        if not isinstance(dec, dict):
            continue
        for key in ("decision_id", "supersedes"):
            if dec.get(key):
                used.add(str(dec[key]))
    outcome = fm.get("outcome")
    if isinstance(outcome, dict):
        for d in outcome.get("decisions") or []:
            if isinstance(d, dict) and d.get("decision_id"):
                used.add(str(d["decision_id"]))
    return used


def _next_decision_id(fm: dict, pr: int) -> str:
    """Allocate ``DEC-{pr}-{high-water mark + 1}``, never a positional count.

    The previous allocator was ``len(agent_decisions) + 1`` -- a POSITION, not an
    identity. That is only correct while the list is dense and numbered from 1,
    and it silently re-mints a live id the moment it is not: one curation edit,
    one explicit ``--id`` that skips a number, one decision pruned but still
    carrying a verdict row. Observed in the field: a record left holding the
    counters 1, 2 and 4, so a count of 3 re-mints the live ``-4`` id.
    """
    used = _decision_ids_in_use(fm)
    prefix = f"DEC-{pr}-"
    high = 0
    for ident in used:
        if not ident.startswith(prefix):
            continue
        m = _DEC_COUNTER_RE.match(ident[len(prefix) :])
        if m:
            high = max(high, int(m.group(1)))
    n = high + 1
    # Defensive no-op for well-formed ids: any member equal to f"{prefix}{n}"
    # would have a leading digit run of exactly n and so would already have
    # pushed `high` to n or beyond. Kept as a cheap invariant guard for ids the
    # counter regex declines to parse (>15 digits), which do not move the mark.
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


# ── commands ────────────────────────────────────────────────────────────────
def cmd_init(cfg: Config, args) -> int:
    path = cfg.record_path(args.pr)
    try:
        with _record_lock(path):
            return _init_locked(cfg, args, path)
    except TimeoutError as exc:
        print(f"ctx: could not acquire the record lock -- nothing written ({exc}). Re-run the same command.")
        return 1


def _init_locked(cfg: Config, args, path: Path) -> int:
    # Locked because the exists-check and the write are otherwise a TOCTOU, and
    # `init --force` overwrites the whole record: unlocked, it could erase a
    # decision a locked `decide` had just written, which is the failure this
    # whole line of work exists to close.
    if path.is_file() and not args.force:
        print(f"ctx: record already exists: {path} (use --force to overwrite)")
        return 1
    pr = int(args.pr)
    fm = {
        "ctx_id": f"CTX-{pr:04d}",
        "pr_number": pr,
        "title": args.title,
        "status": "open",
        "services_affected": list(args.service or []),
        "topics": list(args.topic or []),
        "prd_ref": args.prd,
        "adr_refs": [],
        "agent_decisions": [],
        "outcome": {"deployed_at": None, "uat_result": None, "production_incidents": [], "decisions": []},
    }
    body = (
        f"\n## Intent\n{args.intent or '_TODO: problem, goals, acceptance criteria_'}\n\n"
        "## What Was Done\n_TODO_\n\n"
        "## Architecture Used\n_TODO: link the relevant section of the reference architecture._\n\n"
        "## Test Results\n_TODO_\n\n"
        "## Build-Process Retro\n_TODO_\n\n"
        "## Risk / Blast Radius\n_TODO_\n\n"
        "## Feedback Events\n_none yet_\n\n"
        "## Closed-Loop Outcome\n_pending deploy_\n"
    )
    _write(path, fm, body)
    print(f"ctx: created {path.relative_to(cfg.repo_root).as_posix()}")
    return 0


def cmd_set(cfg: Config, args) -> int:
    # Locked for the same reason ``decide`` is: agents routinely run `ctx set`
    # alongside `ctx decide`, and an unlocked set would clobber a concurrent
    # decision even though decide itself is now safe.
    path = cfg.record_path(args.pr)
    try:
        with _record_lock(path):
            before = path.read_bytes() if path.is_file() else b""
            fm, body = _parse(before)
            if not fm:
                print(f"ctx: no record for PR {args.pr}; run `ctx init --pr {args.pr}` first.")
                return 1
            for assignment in args.assignments:
                if "=" not in assignment:
                    print(f"ctx: bad assignment '{assignment}' (expected key=value)")
                    return 1
                key, _, value = assignment.partition("=")
                _set_dotted(fm, key.strip(), _coerce(value.strip()))
            if not _write_if_unchanged(path, fm, body, before):
                return 1
    except TimeoutError as exc:
        print(f"ctx: could not acquire the record lock -- nothing written ({exc}). Re-run the same command.")
        return 1
    print(f"ctx: updated {len(args.assignments)} field(s) on CTX-{int(args.pr):04d}")
    return 0


def cmd_decide(cfg: Config, args) -> int:
    """Append a decision, or -- only with explicit ``--update`` -- revise one.

    Every write here is append-only unless ``--update DEC-N-k`` names an existing
    id. A ``decide`` that lands on an id already in the record is a bug, not an
    edit, so it ERRORS and writes nothing rather than shadowing the entry that
    holds the id.
    """
    # Cheap arg validation before taking the lock or touching the file.
    if args.update and args.id:
        print("ctx: --update and --id are mutually exclusive (--update revises an existing id, --id mints one).")
        return 1
    path = cfg.record_path(args.pr)
    try:
        with _record_lock(path):
            return _decide_locked(path, args)
    except TimeoutError as exc:
        print(f"ctx: could not acquire the record lock -- nothing written ({exc}). Re-run the same command.")
        return 1


def _decide_locked(path: Path, args) -> int:
    """The read-modify-write span, run under :func:`_record_lock`."""
    # Snapshot and parse the SAME bytes. Reading twice would anchor the
    # compare-and-swap to a later read than the data being edited, so a write
    # landing between the two would be baked into the snapshot and then
    # overwritten with stale content -- the CAS would pass while losing data.
    before = path.read_bytes() if path.is_file() else b""
    fm, body = _parse(before)
    if not fm:
        print(f"ctx: no record for PR {args.pr}; run `ctx init --pr {args.pr}` first.")
        return 1
    # An explicit `key:` with no value parses to None. That is an EMPTY block,
    # not a corrupt one -- it is the shape a curation pass leaves behind, and
    # this tool exists because records get hand-edited. Fill it; refuse only on
    # a type that cannot mean "empty".
    if fm.get("agent_decisions") is None:
        fm["agent_decisions"] = []
    decisions = fm["agent_decisions"]
    if not isinstance(decisions, list):
        print(f"ctx: CTX-{int(args.pr):04d} has a non-list 'agent_decisions'; refusing to write.")
        return 1
    if fm.get("outcome") is None:
        fm["outcome"] = {}
    outcome = fm["outcome"]
    if not isinstance(outcome, dict):
        print(f"ctx: CTX-{int(args.pr):04d} has a non-mapping 'outcome'; refusing to write.")
        return 1
    if outcome.get("decisions") is None:
        outcome["decisions"] = []
    if not isinstance(outcome["decisions"], list):
        print(f"ctx: CTX-{int(args.pr):04d} has a non-list 'outcome.decisions'; refusing to write.")
        return 1

    if args.update:
        return _decide_update(path, fm, body, decisions, before, args)

    used = _decision_ids_in_use(fm)
    dec_id = args.id or _next_decision_id(fm, int(args.pr))
    if dec_id in used:
        print(
            f"ctx: decision id {dec_id} is already in use in CTX-{int(args.pr):04d} -- refusing to write.\n"
            f"     Appending it would leave two entries answering to one id, which readers and the\n"
            f"     Tier-2 fold silently collapse to whichever came last.\n"
            f"     To revise that decision:  ctx decide --pr {args.pr} --update {dec_id} ...\n"
            f"     To add a new one:         drop --id and let ctx allocate."
        )
        return 1

    entry = {
        "decision_id": dec_id,
        "agent": args.agent,
        "decision": args.decision,
        "rationale": args.rationale,
        "alternatives": [{"option": a} for a in (args.alternative or [])],
        "reversibility": args.reversibility or "unknown",
    }
    if args.supersedes:
        entry["supersedes"] = args.supersedes
    decisions.append(entry)
    # Mint a matching pending verdict in the closed-loop outcome. Shape
    # already validated above, so this cannot blow up on a hand-edited record.
    outcome["decisions"].append({"decision_id": dec_id, "verdict": "pending", "evidence": None})
    if not _write_if_unchanged(path, fm, body, before):
        return 1
    print(f"ctx: recorded {dec_id} (agent={args.agent}) on CTX-{int(args.pr):04d}")
    return 0


def _decide_update(path: Path, fm: dict, body: str, decisions: list, before: bytes, args) -> int:
    """``--update DEC-N-k``: replace exactly one existing decision, in place.

    Touches only the named entry. The verdict row is deliberately left alone --
    ``ctx verdict`` owns it, and resetting a closed loop to ``pending`` because
    someone fixed a typo would be its own quiet loss. ``alternatives`` is
    replaced only when ``--alternative`` is actually passed, for the same reason.
    """
    pr = int(args.pr)
    matches = [d for d in decisions if isinstance(d, dict) and str(d.get("decision_id")) == args.update]
    if not matches:
        print(f"ctx: --update {args.update}: no such decision in CTX-{pr:04d}; nothing written.")
        return 1
    if len(matches) > 1:
        print(
            f"ctx: --update {args.update}: {len(matches)} decisions in CTX-{pr:04d} share that id -- "
            f"refusing to guess which one you meant. Give them distinct ids first."
        )
        return 1
    target = matches[0]
    target["agent"] = args.agent
    target["decision"] = args.decision
    target["rationale"] = args.rationale
    if args.alternative:
        target["alternatives"] = [{"option": a} for a in args.alternative]
    if args.reversibility:
        target["reversibility"] = args.reversibility
    if args.supersedes:
        target["supersedes"] = args.supersedes
    if not _write_if_unchanged(path, fm, body, before):
        return 1
    print(f"ctx: updated {args.update} (agent={args.agent}) on CTX-{pr:04d}; {len(decisions)} decision(s) total")
    return 0


def _write_if_unchanged(path: Path, fm: dict, body: str, before: bytes) -> bool:
    """Write only if the record still holds the bytes the caller parsed.

    This is a BACKSTOP, not the concurrency fix. Two processes starting together
    both read the pre-write record and both pass this comparison, so on its own
    it does not prevent a lost update -- :func:`_record_lock` is what does.

    It is still load-bearing, for two cases the lock does not cover: a writer
    that never takes the lock (a hand-edit, an external tool, an older ctx), and
    the moment after a stale lock has been broken, where the previous holder may
    still be alive and mid-write. In that second case this comparison is the only
    thing standing between the two writers, and it degrades to a loud refusal.
    """
    current = path.read_bytes() if path.is_file() else b""
    if current != before:
        what = "was deleted" if not path.is_file() else "changed on disk"
        print(
            f"ctx: {path.name} {what} while this write was in flight -- nothing written, "
            f"so no existing decision was overwritten. Re-run the same command."
        )
        return False
    _write(path, fm, body)
    return True


def cmd_lint(cfg: Config, args) -> int:
    floor = getattr(args, "floor", DEFAULT_GATE_FLOOR)
    # `--pr N` asks the pre-merge question -- "will this PR pass Context Check?"
    # -- so it must reach the gate's verdict, historian requirement included, or
    # we have rebuilt the local-vs-CI drift with a new field. A bare sweep or an
    # explicit `path` is a historical health report over records mostly written
    # before the rule existed; defaulting it on there would print ~347 failures
    # and teach everyone to ignore the command.
    require_historian = getattr(args, "require_historian", None)
    if require_historian is None:
        require_historian = bool(args.pr)
    if args.path:
        targets = [Path(args.path)]
    elif args.pr:
        targets = [cfg.record_path(args.pr)]
    else:
        targets = sorted(cfg.records_dir().glob("CTX-*.md"))
    if not targets:
        print("ctx lint: no records found.")
        return 0
    failed = 0
    for t in targets:
        if not t.is_file():
            print(f"  [FAIL] {t}: file not found")
            failed += 1
            continue
        problems = lint_record(t, floor=normalize_floor(floor), require_historian=require_historian)
        rel = t.relative_to(cfg.repo_root).as_posix() if t.is_absolute() else str(t)
        if problems:
            failed += 1
            print(f"  [FAIL] {rel}")
            for p in problems:
                print(f"         - {p}")
        else:
            print(f"  [PASS] {rel}")
    # The floor is printed because it decides the verdict: the default matches
    # the Context Check CI gate, and a lower floor is a strictly weaker check.
    # Same reason the historian requirement is printed when it is NOT applied:
    # a sweep that says 325/351 valid must not be mistaken for a merge verdict.
    historian_note = "" if require_historian else ", historian not required"
    print(f"\nctx lint: {len(targets) - failed}/{len(targets)} valid (floor={floor}{historian_note})")
    return 1 if failed else 0


def cmd_assemble(cfg: Config, args) -> int:
    """Historian finalize -- deterministic.

    Validates the record against the merge gate's floor and, on success, writes
    the CONTEXT-OK marker the release-manager checks. No model calls -- the
    substance judgment is the context-keeper agent's separate advisory step.

    Lints at DEFAULT_GATE_FLOOR, not the record's own status: assemble is a
    pre-merge finalize, so a CONTEXT-OK marker must mean "this record will pass
    Context Check". It previously linted at the record's status and therefore
    green-lit records the CI gate went on to reject.
    """
    path = cfg.record_path(args.pr)
    rel = path.relative_to(cfg.repo_root).as_posix()
    if not path.is_file():
        print(f"ctx assemble: CONTEXT-INCOMPLETE -- no record at {rel}")
        return 1
    # require_historian=True: assemble IS the Historian's finalize step, and the
    # marker it writes is what the release-manager reads as "the record is good".
    # A CONTEXT-OK on a record the Historian never touched is the exact claim
    # this change exists to stop making.
    problems = lint_record(path, floor=DEFAULT_GATE_FLOOR, require_historian=True)
    if problems:
        print(f"ctx assemble: CONTEXT-INCOMPLETE -- {rel} failed lint (floor={DEFAULT_GATE_FLOOR}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    marker = cfg.repo_root / ".claude" / f"context-recorded-{int(args.pr)}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{path.stem} assembled\n", encoding="utf-8")
    print(f"ctx assemble: CONTEXT-OK -- {rel} valid; wrote {marker.relative_to(cfg.repo_root).as_posix()}")
    return 0


def cmd_lifecycle_sync(cfg: Config, args) -> int:
    """Advance records whose PR has merged: status open/in_review -> merged.

    Detection follows the squash-merge convention: a commit reachable from
    HEAD whose subject contains "(#N)". This runs post-merge (the weekly
    reconcile workflow), so records that fail the merged-level floors after
    flipping are reported as a curation queue rather than a hard failure —
    the *binding* layer is the PR gate (check_context_record --floor merged),
    which blocks incomplete records before they merge.
    """
    import re as _re
    import subprocess

    # One pass over history; match "(#N)" in SUBJECTS only. --grep would search
    # the whole message, so a commit merely *referencing* another PR in its
    # body ("consumes the backend (#N)") would count as proof it merged.
    try:
        log = subprocess.run(
            ["git", "log", "--format=%H%x09%s"],
            cwd=cfg.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        # A git failure (shallow clone, missing git, corrupt repo) must not
        # masquerade as a healthy "0 records advanced" no-op.
        print(f"ctx lifecycle-sync: ERROR reading git history: {exc}")
        return 1
    merged_prs: dict[int, str] = {}
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        for m in _re.finditer(r"\(#(\d+)\)", subject):
            merged_prs.setdefault(int(m.group(1)), sha)

    flipped: list[str] = []
    queue: list[tuple[str, list[str]]] = []
    for path in sorted(cfg.records_dir().glob("CTX-*.md")):
        # Locked per record, like every other mutating command. This runs in a
        # scheduled workflow where nothing else is writing, so it was tempting to
        # leave it alone -- but it is a full read-modify-write of a record, and
        # an unlocked one here would silently drop a decision if it ever DID run
        # beside an agent. A guarantee with an undocumented exception is the kind
        # of claim this PR exists to stop making.
        try:
            with _record_lock(path):
                flipped_rel = _lifecycle_sync_one(cfg, args, path, merged_prs, queue)
        except TimeoutError as exc:
            print(f"ctx lifecycle-sync: SKIPPED {path.name} -- {exc}")
            continue
        if flipped_rel:
            flipped.append(flipped_rel)

    if queue:
        print(f"\nctx lifecycle-sync: {len(queue)} record(s) now below the merged floor (curation queue):")
        for rel, problems in queue:
            print(f"  [CURATE] {rel}")
            for p in problems:
                print(f"           - {p}")
    print(f"\nctx lifecycle-sync: {len(flipped)} record(s) {'would be ' if args.dry_run else ''}advanced")
    return 0


def _lifecycle_sync_one(cfg: Config, args, path: Path, merged_prs: dict, queue: list) -> str | None:
    """Advance one record if its PR merged. Runs under the record's lock."""
    before = path.read_bytes() if path.is_file() else b""
    fm, body = _parse(before)
    if not fm:
        return None
    status = str(fm.get("status", "open"))
    if status not in ("open", "in_review"):
        return None
    try:
        pr = int(fm.get("pr_number"))
    except (TypeError, ValueError):
        return None
    merge_sha = merged_prs.get(pr)
    if not merge_sha:
        return None
    rel = path.relative_to(cfg.repo_root).as_posix()
    if args.dry_run:
        print(f"ctx lifecycle-sync: would flip {rel} ({status} -> merged, PR #{pr} at {merge_sha[:9]})")
        return rel
    fm["status"] = "merged"
    if not _write_if_unchanged(path, fm, body, before):
        return None
    print(f"ctx lifecycle-sync: {rel} {status} -> merged (PR #{pr} at {merge_sha[:9]})")
    problems = lint_record(path)
    if problems:
        queue.append((rel, problems))
    return rel


def cmd_bootstrap(cfg: Config, args) -> int:
    """Render generic agent templates into canonical paths.

    The Project Profile is config-source + rendered: edit .context/config.yml,
    re-run bootstrap. Templates carry {{tokens}} substituted from config.
    """
    from render import render_agent_templates  # local import

    written = render_agent_templates(cfg, check_only=args.check)
    verb = "in sync" if args.check else "rendered"
    for dest in written:
        print(f"  [{verb}] {dest}")
    if args.check and written:
        print(f"\nctx bootstrap --check: {len(written)} file(s) OUT OF SYNC with config. Re-run `ctx bootstrap`.")
        return 1
    print(f"\nctx bootstrap: {len(written)} file(s) {verb} from .context/config.yml")
    return 0


def cmd_verdict(cfg: Config, args) -> int:
    """Close the loop on one decision: set its outcome verdict + evidence."""
    if args.verdict not in ("good", "bad", "mixed", "superseded", "unreconciled", "pending"):
        print(f"ctx: invalid verdict '{args.verdict}'")
        return 1
    path = cfg.record_path(args.pr)
    try:
        with _record_lock(path):
            before = path.read_bytes() if path.is_file() else b""
            fm, body = _parse(before)
            if not fm:
                print(f"ctx: no record for PR {args.pr}; run `ctx init --pr {args.pr}` first.")
                return 1
            if fm.get("outcome") is None:
                fm["outcome"] = {}
            outcome = fm["outcome"]
            if isinstance(outcome, dict) and outcome.get("decisions") is None:
                outcome["decisions"] = []
            if not isinstance(outcome, dict) or not isinstance(outcome["decisions"], list):
                print(f"ctx: CTX-{int(args.pr):04d} has a malformed 'outcome.decisions'; refusing to write.")
                return 1
            for d in outcome["decisions"]:
                if isinstance(d, dict) and d.get("decision_id") == args.decision:
                    d["verdict"] = args.verdict
                    d["evidence"] = args.evidence
                    if not _write_if_unchanged(path, fm, body, before):
                        return 1
                    print(f"ctx verdict: {args.decision} -> {args.verdict} on CTX-{int(args.pr):04d}")
                    return 0
    except TimeoutError as exc:
        print(f"ctx: could not acquire the record lock -- nothing written ({exc}). Re-run the same command.")
        return 1
    print(f"ctx: decision {args.decision} not found in CTX-{int(args.pr):04d} outcome.decisions")
    return 1


def cmd_reduce(cfg: Config, args) -> int:
    import reduce as _reduce

    pr = int(args.pr) if getattr(args, "pr", None) else None
    return _reduce.run_reduce(cfg, pr)


def cmd_reconcile(cfg: Config, args) -> int:
    import reduce as _reduce

    return _reduce.run_reconcile(cfg)


def cmd_query(cfg: Config, args) -> int:
    import query as _query

    return _query.run_query(
        cfg,
        service=args.service,
        files=args.files,
        intent=args.intent,
        topics=args.topic or [],
        as_json=args.json,
    )


def cmd_index(cfg: Config, args) -> int:
    import index as _index

    return _index.run_index(cfg, backend=args.backend)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctx", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="create a new context record for a PR")
    pi.add_argument("--pr", required=True)
    pi.add_argument("--title", required=True)
    pi.add_argument("--prd")
    pi.add_argument("--service", action="append")
    pi.add_argument("--topic", action="append")
    pi.add_argument("--intent")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("set", help="set frontmatter fields (dotted keys)")
    ps.add_argument("--pr", required=True)
    ps.add_argument("assignments", nargs="+", help="key=value (e.g. risk_level=LOW)")
    ps.set_defaults(func=cmd_set)

    pd = sub.add_parser("decide", help="record an agent decision (append-only unless --update)")
    pd.add_argument("--pr", required=True)
    pd.add_argument("--agent", required=True)
    pd.add_argument("--decision", required=True)
    pd.add_argument("--rationale", required=True)
    pd.add_argument("--id", help="mint this exact id instead of the next free one; errors if already in use")
    pd.add_argument(
        "--update",
        metavar="DEC-N-k",
        help=(
            "revise the decision already filed under this id, in place. Without it, decide is "
            "append-only and an id collision is an error -- there is no silent update."
        ),
    )
    pd.add_argument("--alternative", action="append")
    pd.add_argument(
        "--reversibility",
        default=None,
        help="defaults to 'unknown' on a new decision; left as-is by --update when omitted",
    )
    pd.add_argument("--supersedes")
    pd.set_defaults(func=cmd_decide)

    pl = sub.add_parser("lint", help="validate record(s) against the schema floor")
    pl.add_argument("--pr")
    _fields, _sections = requirements_for(DEFAULT_GATE_FLOOR)
    pl.add_argument(
        "--floor",
        choices=FLOOR_CHOICES,
        default=DEFAULT_GATE_FLOOR,
        help=(
            f"lifecycle floor to validate against (default: {DEFAULT_GATE_FLOOR} -- the same floor the "
            f"Context Check CI gate imposes, requiring frontmatter {list(_fields)} and sections "
            f"{['## ' + s for s in _sections]}). '{RECORD_NATIVE_FLOOR}' imposes no floor and lints at "
            f"the record's own status, which is a strictly weaker check than CI."
        ),
    )
    pl.add_argument(
        "--require-historian",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            f"require an agent_decisions entry from '{HISTORIAN_AGENT}'. Default: on with --pr "
            f"(matching the CI gate), off for a whole-ledger sweep."
        ),
    )
    pl.add_argument("path", nargs="?")
    pl.set_defaults(func=cmd_lint)

    pa = sub.add_parser("assemble", help="Historian finalize: validate + write CONTEXT-OK marker")
    pa.add_argument("--pr", required=True)
    pa.set_defaults(func=cmd_assemble)

    pls = sub.add_parser(
        "lifecycle-sync",
        help="advance open/in_review records whose PR merged (subject contains '(#N)') to status=merged",
    )
    pls.add_argument("--dry-run", action="store_true", help="report what would flip; write nothing")
    pls.set_defaults(func=cmd_lifecycle_sync)

    pb = sub.add_parser("bootstrap", help="render agent templates' Project Profile from config")
    pb.add_argument("--check", action="store_true", help="verify rendered files match config; do not write")
    pb.set_defaults(func=cmd_bootstrap)

    pv = sub.add_parser("verdict", help="close the loop on a decision: set outcome verdict + evidence")
    pv.add_argument("--pr", required=True)
    pv.add_argument("--decision", required=True, help="decision_id, e.g. DEC-<pr>-1")
    pv.add_argument("--verdict", required=True, help="good|bad|mixed|superseded|unreconciled|pending")
    pv.add_argument("--evidence", default=None)
    pv.set_defaults(func=cmd_verdict)

    pr_ = sub.add_parser("reduce", help="fold record(s) into the Tier-2 synthesis layer")
    pr_.add_argument("--pr", help="guard: require this PR's record to exist before re-deriving")
    pr_.set_defaults(func=cmd_reduce)

    pc = sub.add_parser("reconcile", help="re-derive Tier 2 + flag unreconciled + curation queue")
    pc.set_defaults(func=cmd_reconcile)

    pq = sub.add_parser("query", help="before-dev briefing: prior decisions/patterns/open-loops")
    pq.add_argument("--service", help="target service (e.g. billing-service)")
    pq.add_argument("--files", action="append", help="changed/target file path(s)")
    pq.add_argument("--intent", help="one-line description of what you're about to do")
    pq.add_argument("--topic", action="append", help="topic filter")
    pq.add_argument("--json", action="store_true", help="structured output for an agent")
    pq.set_defaults(func=cmd_query)

    px = sub.add_parser("index", help="build the optional recall index for query")
    px.add_argument("--backend", choices=["auto", "lexical", "embeddings"], default="auto")
    px.set_defaults(func=cmd_index)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
