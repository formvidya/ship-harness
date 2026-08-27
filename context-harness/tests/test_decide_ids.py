"""Regression tests for ``ctx decide`` id allocation (data-loss fix).

Reproduced in the field: two successive ``decide`` calls for the same
(pr, agent) minted the SAME id, and the record ended up with one id answering
to two decisions -- the earlier text unreachable. The allocator was
``len(agent_decisions) + 1``, a POSITION rather than an identity, which only
holds while the list is dense and numbered from 1. Records are routinely curated
after their PR merged (the carry-forward pattern), so any prune, any explicit
``--id``, or any entry surviving only as an ``outcome.decisions`` verdict row
drops the count below the high-water mark and the next decide re-mints a live
id. A record already on the main branch was in exactly that state: ids -1, -2
and -4, with -3 pruned.

Covers:
  * two successive decides on the same (pr, agent) -> two distinct ids, both texts intact
  * a pruned entry whose verdict row survives does not get its id recycled
  * a forced collision (``--id`` on a live id) ERRORS and writes nothing
  * ``--update`` replaces only the targeted id, and only with the flag
  * genuinely concurrent `decide` PROCESSES all keep their decisions (the lock)
  * hand-corrupted records refuse cleanly instead of tracebacking

Every test meant to catch the ORIGINAL defect builds a non-dense record via
``_seed_nondense``. A record built only from ``ctx init`` + ``decide`` is always
dense, and on a dense record the old count-based allocator returns the correct
id -- so such a test passes against the buggy code and proves nothing.

Direction is checked by measurement, not assertion. Against pre-fix code (a
checkout of the commit before the fix landed): **22 fail, 5 pass**, and those 5
are the invariant locks and negative controls that are meant to pass either way
(``test_successive_decides_on_a_dense_record_append``,
``test_update_is_never_the_default``, ``test_lock_and_temp_files_are_cleaned_up``,
``test_all_non_numeric_ids_still_allocate``,
``test_written_record_still_passes_lint``). Keep that ratio honest when adding
tests here: a regression test that cannot fail against the code it indicts does
not merely miss the bug, it certifies it -- which is how the preceding change
shipped a broken concurrency guard with a passing test beside it.

Run: python -m pytest context-harness/tests/test_decide_ids.py -q
"""

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import yaml

CTX_PY = Path(__file__).resolve().parents[1] / "ctx" / "ctx.py"

# The synthetic PR number every fixture record is built under. Nothing in the
# allocator depends on its value; it is only the ``CTX-`` / ``DEC-`` prefix the
# assertions below are written against.
PR = 12345


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".context").mkdir(parents=True)
    (repo / ".context" / "config.yml").write_text(
        textwrap.dedent(
            """\
            project:
              name: TestProj
              languages: [python]
            code_roots:
              - "src/**"
            ledger:
              records_dir: docs/context/records
            """
        ),
        encoding="utf-8",
    )
    (repo / "docs" / "context" / "records").mkdir(parents=True)
    return repo


def _run(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CTX_PY), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


# Tests that must observe the acquire timeout use a short one; burning the real
# 15s three times over is 45s of CI doing nothing.
_FAST_TIMEOUT = {"CTX_LOCK_TIMEOUT_S": "0.5"}


def _decide(repo: Path, pr: int, agent: str, decision: str, *extra: str) -> subprocess.CompletedProcess:
    return _run(
        repo,
        "decide",
        "--pr",
        str(pr),
        "--agent",
        agent,
        "--decision",
        decision,
        "--rationale",
        f"rationale for {decision}",
        *extra,
    )


def _record_path(repo: Path, pr: int) -> Path:
    return repo / "docs" / "context" / "records" / f"CTX-{pr:04d}.md"


def _lock_path(repo: Path, pr: int) -> Path:
    return _record_path(repo, pr).with_name(_record_path(repo, pr).name + ".lock")


def _frontmatter(repo: Path, pr: int) -> dict:
    text = _record_path(repo, pr).read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert m, "record lost its frontmatter"
    return yaml.safe_load(m.group(1))


def _decisions(repo: Path, pr: int) -> list[dict]:
    return _frontmatter(repo, pr)["agent_decisions"]


def _ids(repo: Path, pr: int) -> list[str]:
    return [d["decision_id"] for d in _decisions(repo, pr)]


def _outcome_ids(repo: Path, pr: int) -> list[str]:
    return [d["decision_id"] for d in _frontmatter(repo, pr)["outcome"]["decisions"]]


def _init(tmp_path: Path, pr: int = PR) -> Path:
    repo = _make_repo(tmp_path)
    res = _run(repo, "init", "--pr", str(pr), "--title", "test record", "--intent", "Ship it.")
    assert res.returncode == 0, res.stdout + res.stderr
    return repo


def _rewrite_frontmatter(repo: Path, pr: int, mutate) -> None:
    """Hand-edit a record the way a curation pass would, then write it back."""
    path = _record_path(repo, pr)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fm, body = yaml.safe_load(m.group(1)), text[m.end() :]
    mutate(fm)
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )


def _seed_nondense(tmp_path: Path, pr: int = PR) -> Path:
    """A record in the curated shape: ids 1/2/4, so the COUNT (3) trails the mark (4).

    This is the state the bug needs, and it is the state a real curated record
    was found in. A record built only by ``ctx init`` + ``decide`` is always
    dense, and on a dense record the old count-based allocator returns the right
    answer -- so a test built on one passes against the buggy code and proves
    nothing. Every test below that is meant to catch the original defect starts
    here.
    """
    repo = _init(tmp_path, pr)
    for n in (1, 2, 3):
        assert _decide(repo, pr, "backend", f"seed {n}").returncode == 0

    # Renumber the third to -4 in both blocks, exactly as a supersede/curation
    # pass would, leaving a gap at -3.
    def mutate(fm):
        fm["agent_decisions"][2]["decision_id"] = f"DEC-{pr}-4"
        fm["outcome"]["decisions"][2]["decision_id"] = f"DEC-{pr}-4"

    _rewrite_frontmatter(repo, pr, mutate)
    assert _ids(repo, pr) == [f"DEC-{pr}-1", f"DEC-{pr}-2", f"DEC-{pr}-4"]
    return repo


# ── the reported bug ────────────────────────────────────────────────────────
def test_two_successive_decides_same_agent_get_distinct_ids(tmp_path):
    """The reported incident: two decides, same (pr, agent), one id between them.

    Runs on the non-dense shape ON PURPOSE. Against the old allocator the first
    call re-mints the live ``...-4`` id and this fails; on a fresh record it
    would pass either way.
    """
    repo = _seed_nondense(tmp_path)
    first = _decide(repo, PR, "security", "FIRST finding")
    second = _decide(repo, PR, "security", "SECOND finding")
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr

    ids = _ids(repo, PR)
    assert len(ids) == len(set(ids)), ids
    assert ids[-2:] == [f"DEC-{PR}-5", f"DEC-{PR}-6"], ids  # never the live -4
    texts = [d["decision"] for d in _decisions(repo, PR)]
    assert texts[-2:] == ["FIRST finding", "SECOND finding"]
    # Both ids reported to the caller must be the ids actually written.
    assert ids[-2] in first.stdout and ids[-1] in second.stdout
    assert _outcome_ids(repo, PR) == ids


def test_successive_decides_on_a_dense_record_append(tmp_path):
    """Invariant lock, not a regression test -- this passes against the old code too."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "security", "FIRST finding").returncode == 0
    assert _decide(repo, PR, "security", "SECOND finding").returncode == 0
    assert _ids(repo, PR) == [f"DEC-{PR}-1", f"DEC-{PR}-2"]
    assert [d["decision"] for d in _decisions(repo, PR)] == ["FIRST finding", "SECOND finding"]


def test_live_verdict_is_not_shadowed_by_a_recycled_id(tmp_path):
    """The corruption the incident actually caused: a shipped verdict shadowed.

    On the non-dense shape the old allocator re-mints the ``...-4`` id, giving
    it two verdict rows -- the 'good' one it earned and a fresh 'pending'.
    """
    repo = _seed_nondense(tmp_path)
    assert (
        _run(
            repo,
            "verdict",
            "--pr",
            str(PR),
            "--decision",
            f"DEC-{PR}-4",
            "--verdict",
            "good",
            "--evidence",
            "shipped",
        ).returncode
        == 0
    )
    assert _decide(repo, PR, "security", "post-curation finding").returncode == 0

    rows = _frontmatter(repo, PR)["outcome"]["decisions"]
    ids = [r["decision_id"] for r in rows]
    assert len(ids) == len(set(ids)), ids
    good = [r for r in rows if r["decision_id"] == f"DEC-{PR}-4"]
    assert len(good) == 1 and good[0]["verdict"] == "good" and good[0]["evidence"] == "shipped"


def test_pruned_decision_does_not_get_its_id_recycled(tmp_path):
    """The exact carry-forward shape: entry pruned, verdict row survives.

    Under the old count-based allocator this re-minted the pruned id, so the new
    decision inherited the pruned one's verdict row and history.
    """
    repo = _init(tmp_path)
    for n in range(3):
        assert _decide(repo, PR, "backend", f"decision {n}").returncode == 0
    assert _ids(repo, PR) == [f"DEC-{PR}-1", f"DEC-{PR}-2", f"DEC-{PR}-3"]

    # Curate: drop the -2 entry from agent_decisions, leave outcome.decisions alone.
    path = _record_path(repo, PR)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fm, body = yaml.safe_load(m.group(1)), text[m.end() :]
    fm["agent_decisions"] = [d for d in fm["agent_decisions"] if d["decision_id"] != f"DEC-{PR}-2"]
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )
    assert len(fm["agent_decisions"]) == 2  # count (2) now trails the high-water mark (3)

    res = _decide(repo, PR, "security", "post-curation finding")
    assert res.returncode == 0, res.stdout + res.stderr
    ids = _ids(repo, PR)
    assert f"DEC-{PR}-4" in ids, ids  # not the -3, and not the recycled -2
    assert len(ids) == len(set(ids)), ids
    assert len(_outcome_ids(repo, PR)) == len(set(_outcome_ids(repo, PR)))


def test_id_referenced_only_in_outcome_block_is_not_reused(tmp_path):
    """Requirement: ids referenced *only* in a verdict/outcome block are taken."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "only decision").returncode == 0

    path = _record_path(repo, PR)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fm, body = yaml.safe_load(m.group(1)), text[m.end() :]
    # The -2 id exists ONLY as a verdict row -- its decision was never written
    # back after a curation pass. It is still spoken for.
    fm["outcome"]["decisions"].append({"decision_id": f"DEC-{PR}-2", "verdict": "good", "evidence": "shipped"})
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )

    assert _decide(repo, PR, "security", "next one").returncode == 0
    assert _ids(repo, PR) == [f"DEC-{PR}-1", f"DEC-{PR}-3"]
    # The pre-existing 'good' verdict must not have been reset by a new pending row.
    verdicts = {d["decision_id"]: d["verdict"] for d in _frontmatter(repo, PR)["outcome"]["decisions"]}
    assert verdicts[f"DEC-{PR}-2"] == "good"


# ── forced collision errors ──────────────────────────────────────────────────
def test_forced_id_collision_errors_and_writes_nothing(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "security", "ORIGINAL text").returncode == 0
    before = _record_path(repo, PR).read_bytes()

    res = _decide(repo, PR, "security", "CLOBBERING text", "--id", f"DEC-{PR}-1")
    assert res.returncode == 1, res.stdout
    assert "already in use" in res.stdout
    assert _record_path(repo, PR).read_bytes() == before
    assert [d["decision"] for d in _decisions(repo, PR)] == ["ORIGINAL text"]


def test_forced_collision_against_outcome_only_id_errors(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    path = _record_path(repo, PR)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fm, body = yaml.safe_load(m.group(1)), text[m.end() :]
    fm["outcome"]["decisions"].append({"decision_id": f"DEC-{PR}-9", "verdict": "pending", "evidence": None})
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )
    before = path.read_bytes()

    res = _decide(repo, PR, "security", "collides", "--id", f"DEC-{PR}-9")
    assert res.returncode == 1, res.stdout
    assert path.read_bytes() == before


def test_update_and_id_are_mutually_exclusive(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "security", "first").returncode == 0
    res = _decide(repo, PR, "security", "x", "--id", f"DEC-{PR}-5", "--update", f"DEC-{PR}-1")
    assert res.returncode == 1
    assert "mutually exclusive" in res.stdout


# ── the explicit --update path ───────────────────────────────────────────────
def test_update_replaces_only_the_targeted_id(tmp_path):
    repo = _init(tmp_path)
    for n in (1, 2, 3):
        assert _decide(repo, PR, "backend", f"decision {n}").returncode == 0

    res = _decide(repo, PR, "security", "REVISED decision 2", "--update", f"DEC-{PR}-2")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"updated DEC-{PR}-2" in res.stdout

    decisions = _decisions(repo, PR)
    assert _ids(repo, PR) == [f"DEC-{PR}-1", f"DEC-{PR}-2", f"DEC-{PR}-3"]  # no new id minted
    by_id = {d["decision_id"]: d for d in decisions}
    assert by_id[f"DEC-{PR}-2"]["decision"] == "REVISED decision 2"
    assert by_id[f"DEC-{PR}-2"]["rationale"] == "rationale for REVISED decision 2"
    assert by_id[f"DEC-{PR}-2"]["agent"] == "security"
    # Neighbours untouched.
    assert by_id[f"DEC-{PR}-1"]["decision"] == "decision 1"
    assert by_id[f"DEC-{PR}-3"]["decision"] == "decision 3"
    assert by_id[f"DEC-{PR}-1"]["agent"] == "backend"
    # No duplicate verdict row minted for an id that already had one.
    assert _outcome_ids(repo, PR) == [f"DEC-{PR}-1", f"DEC-{PR}-2", f"DEC-{PR}-3"]


def test_update_preserves_an_existing_verdict(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "shipped it").returncode == 0
    assert (
        _run(
            repo,
            "verdict",
            "--pr",
            str(PR),
            "--decision",
            f"DEC-{PR}-1",
            "--verdict",
            "good",
            "--evidence",
            "no incidents",
        ).returncode
        == 0
    )

    assert _decide(repo, PR, "backend", "shipped it (typo fixed)", "--update", f"DEC-{PR}-1").returncode == 0
    row = _frontmatter(repo, PR)["outcome"]["decisions"][0]
    assert row["verdict"] == "good" and row["evidence"] == "no incidents"


def test_update_of_unknown_id_errors_and_writes_nothing(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    before = _record_path(repo, PR).read_bytes()

    res = _decide(repo, PR, "security", "revision", "--update", f"DEC-{PR}-99")
    assert res.returncode == 1, res.stdout
    assert "no such decision" in res.stdout
    assert _record_path(repo, PR).read_bytes() == before


def test_update_is_never_the_default(tmp_path):
    """Without --update, a second decide by the same agent must APPEND."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "security", "first").returncode == 0
    assert _decide(repo, PR, "security", "second").returncode == 0
    assert len(_decisions(repo, PR)) == 2


def test_update_leaves_alternatives_when_flag_omitted(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first", "--alternative", "the road not taken").returncode == 0
    assert _decide(repo, PR, "backend", "first, reworded", "--update", f"DEC-{PR}-1").returncode == 0
    assert _decisions(repo, PR)[0]["alternatives"] == [{"option": "the road not taken"}]


# ── concurrency ──────────────────────────────────────────────────────────────
# Racers import ctx and then BLOCK on a barrier file, so interpreter startup and
# module import happen before the race rather than inside it. Without the
# barrier ~200ms of Python startup staggers the processes out of contention and
# detection becomes unreliable -- two independent measurements of unsynchronised
# variants disagreed (2/8 and 6/8), which is itself the point: the rate is not a
# stable property. With the barrier it is: 6/6 here and 10/10 in an independent
# review against the flawed code, and 6/6 green against this one.
_RACER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ctx                      # warm up BEFORE the barrier
go = Path(sys.argv[2])
while not go.exists():
    time.sleep(0.001)
sys.exit(ctx.main(sys.argv[3:]))
"""


def test_genuinely_concurrent_decides_all_survive(tmp_path):
    """N `ctx decide` PROCESSES racing on one record; every decision must survive.

    This is the case a compare-and-swap alone cannot handle and a unit test of
    the write helper cannot see: processes that start together all read the
    pre-write record, all pass a byte comparison because none has written yet,
    and the last write wins. `ctx decide` is invoked exactly this way by design
    -- the methodology launches the support agents in parallel -- so it is the
    workflow, not an edge case.
    """
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "seed").returncode == 0
    racer = tmp_path / "racer.py"
    racer.write_text(_RACER, encoding="utf-8")
    go = tmp_path / "go"

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(racer),
                str(CTX_PY.parent),
                str(go),
                "decide",
                "--pr",
                str(PR),
                "--agent",
                f"agent{i}",
                "--decision",
                f"finding-{i}",
                "--rationale",
                "r",
            ],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(6)
    ]
    time.sleep(1.5)  # let every racer reach the barrier
    go.write_text("go", encoding="utf-8")
    results = [(p.wait(timeout=90), *p.communicate()) for p in procs]

    text = _record_path(repo, PR).read_text(encoding="utf-8")
    lost = [i for i, (rc, _o, _e) in enumerate(results) if rc == 0 and f"finding-{i}" not in text]
    assert not lost, f"agents {lost} exited 0 but their decisions are GONE -- silent data loss"

    # Integrity is only half the contract. Asserting "nobody exited 0 with a
    # missing decision" alone is satisfied by a lock that denies service to
    # everyone: a mutant with _LOCK_TIMEOUT_S = 0 recorded 1 of 6 decisions and
    # still passed. Availability has to be asserted too.
    refused = [(i, out, err) for i, (rc, out, err) in enumerate(results) if rc != 0]
    assert not refused, f"racers refused under normal contention (lock starvation): {refused}"

    ids = _ids(repo, PR)
    assert len(ids) == len(set(ids)), f"duplicate ids under concurrency: {ids}"
    assert len(ids) == 7, f"expected seed + 6 racers, got {ids}"
    assert len(_outcome_ids(repo, PR)) == len(set(_outcome_ids(repo, PR)))


def test_concurrent_set_and_verdict_do_not_drop_writes(tmp_path):
    """`set` and `verdict` are newly locked; agents run them beside `decide`."""
    repo = _init(tmp_path)
    for i in range(3):
        assert _decide(repo, PR, "backend", f"seed {i}").returncode == 0
    racer = tmp_path / "racer.py"
    racer.write_text(_RACER, encoding="utf-8")
    go = tmp_path / "go"

    cmds = [
        ["set", "--pr", str(PR), "risk_level=HIGH"],
        ["set", "--pr", str(PR), "topics=[a, b]"],
        ["verdict", "--pr", str(PR), "--decision", f"DEC-{PR}-1", "--verdict", "good", "--evidence", "shipped"],
        ["verdict", "--pr", str(PR), "--decision", f"DEC-{PR}-2", "--verdict", "bad", "--evidence", "reverted"],
        ["decide", "--pr", str(PR), "--agent", "security", "--decision", "late finding", "--rationale", "r"],
    ]
    procs = [
        subprocess.Popen(
            [sys.executable, str(racer), str(CTX_PY.parent), str(go), *c],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for c in cmds
    ]
    time.sleep(1.5)
    go.write_text("go", encoding="utf-8")
    results = [(p.wait(timeout=90), *p.communicate()) for p in procs]
    assert all(rc == 0 for rc, _o, _e in results), results

    fm = _frontmatter(repo, PR)
    assert fm["risk_level"] == "HIGH"
    assert fm["topics"] == ["a", "b"]
    verdicts = {d["decision_id"]: d["verdict"] for d in fm["outcome"]["decisions"]}
    assert verdicts[f"DEC-{PR}-1"] == "good", "a concurrent writer dropped the -1 verdict"
    assert verdicts[f"DEC-{PR}-2"] == "bad", "a concurrent writer dropped the -2 verdict"
    assert any(d["decision"] == "late finding" for d in fm["agent_decisions"])


def test_lifecycle_sync_respects_the_record_lock(tmp_path):
    """lifecycle-sync mutates records too -- unlocked it wrote straight through."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "a decision").returncode == 0
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat: shipped (#{PR})"], cwd=repo, check=True, capture_output=True)

    lock = _lock_path(repo, PR)
    lock.write_text("1\n", encoding="utf-8")  # fresh -> a live holder
    before = _record_path(repo, PR).read_bytes()
    res = _run(repo, "lifecycle-sync", env=_FAST_TIMEOUT)
    assert "SKIPPED" in res.stdout, res.stdout
    assert _record_path(repo, PR).read_bytes() == before, "lifecycle-sync wrote through a held lock"
    lock.unlink()
    assert "open -> merged" in _run(repo, "lifecycle-sync").stdout


def test_init_force_respects_the_record_lock(tmp_path):
    """`init --force` overwrites wholesale; unlocked it could erase a decision."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "a decision").returncode == 0
    lock = _lock_path(repo, PR)
    lock.write_text("1\n", encoding="utf-8")
    before = _record_path(repo, PR).read_bytes()

    res = _run(repo, "init", "--pr", str(PR), "--title", "clobber", "--force", env=_FAST_TIMEOUT)
    assert res.returncode == 1, res.stdout
    assert "could not acquire the record lock" in res.stdout
    assert _record_path(repo, PR).read_bytes() == before
    lock.unlink()


def test_crlf_record_survives_a_round_trip_without_cr_doubling(tmp_path):
    """A CRLF working-tree checkout must not accumulate \\r on every write.

    `write_text` translates \\n -> \\r\\n on Windows, so a body already holding
    \\r\\n came back as \\r\\r\\n, then \\r\\r\\r\\n. Observed at 129 occurrences
    in a real record after one `ctx verdict`, which also turned a one-field edit
    into a whole-file diff.
    """
    repo = _init(tmp_path)
    path = _record_path(repo, PR)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))  # simulate a CRLF checkout

    assert _decide(repo, PR, "backend", "a decision").returncode == 0
    raw = path.read_bytes()
    assert b"\r\r\n" not in raw, "CR doubling on round-trip"
    assert b"\r" not in raw, "record should normalise to LF"
    # Idempotent: a second pass must not reintroduce it either.
    assert _decide(repo, PR, "security", "another").returncode == 0
    assert b"\r" not in path.read_bytes()
    assert len(_ids(repo, PR)) == 2


def test_lock_and_temp_files_are_cleaned_up(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "a decision").returncode == 0
    keep = _record_path(repo, PR).name
    leftovers = [p.name for p in (repo / "docs" / "context" / "records").iterdir() if p.name != keep]
    assert leftovers == [], leftovers


def test_stale_lock_is_broken_rather_than_wedging_the_ledger(tmp_path, monkeypatch):
    """A killed agent must not leave the record permanently unwritable."""
    repo = _init(tmp_path)
    lock = _lock_path(repo, PR)
    lock.write_text("99999\n", encoding="utf-8")
    os.utime(lock, (time.time() - 3600, time.time() - 3600))  # an hour old

    res = _decide(repo, PR, "backend", "after the stale lock")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "breaking stale lock" in res.stdout
    assert not lock.exists()
    assert [d["decision"] for d in _decisions(repo, PR)] == ["after the stale lock"]


def test_live_lock_blocks_and_writes_nothing(tmp_path):
    """A lock held by a live holder must time out cleanly, not corrupt."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    lock = _lock_path(repo, PR)
    lock.write_text("1\n", encoding="utf-8")  # fresh mtime -> not stale
    before = _record_path(repo, PR).read_bytes()

    res = subprocess.run(
        [
            sys.executable,
            str(CTX_PY),
            "decide",
            "--pr",
            str(PR),
            "--agent",
            "security",
            "--decision",
            "blocked",
            "--rationale",
            "r",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(CTX_PY.parent), **_FAST_TIMEOUT},
        timeout=60,
    )
    assert res.returncode == 1, res.stdout
    assert "could not acquire the record lock" in res.stdout
    assert _record_path(repo, PR).read_bytes() == before
    lock.unlink()


def test_write_if_unchanged_refuses_a_deleted_record(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "a decision").returncode == 0
    sys.path.insert(0, str(CTX_PY.parent))
    import ctx as ctx_mod  # noqa: PLC0415 - the module under test

    path = _record_path(repo, PR)
    stale = path.read_bytes()
    path.unlink()
    # Must refuse rather than resurrect the record from stale in-memory state.
    assert ctx_mod._write_if_unchanged(path, {"ctx_id": f"CTX-{PR:04d}"}, "\n## Intent\nx\n", stale) is False
    assert not path.exists()


# ── hand-corrupted records refuse cleanly instead of tracebacking ────────────
def test_non_mapping_outcome_refuses_cleanly(tmp_path):
    repo = _init(tmp_path)
    for bad in ("a string", ["a", "list"], None):
        _rewrite_frontmatter(repo, PR, lambda fm, b=bad: fm.__setitem__("outcome", b))
        before = _record_path(repo, PR).read_bytes()
        res = _decide(repo, PR, "backend", "x")
        if bad is None:
            # `outcome: null` is a legitimate empty record shape -- it must WORK.
            assert res.returncode == 0, res.stdout + res.stderr
        else:
            assert res.returncode == 1, res.stdout
            assert "Traceback" not in res.stderr, res.stderr
            assert "refusing to write" in res.stdout
            assert _record_path(repo, PR).read_bytes() == before


def test_absurd_counter_in_an_id_does_not_crash(tmp_path):
    """A hand-edited id with a 5000-digit counter must be ignored, not fatal."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    _rewrite_frontmatter(
        repo,
        PR,
        lambda fm: fm["agent_decisions"].append({"decision_id": f"DEC-{PR}-" + "9" * 5000, "agent": "x"}),
    )
    res = _decide(repo, PR, "security", "second")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    assert f"DEC-{PR}-2" in _ids(repo, PR)


def test_all_non_numeric_ids_still_allocate(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    _rewrite_frontmatter(repo, PR, lambda fm: fm["agent_decisions"][0].__setitem__("decision_id", f"DEC-{PR}-alpha"))
    res = _decide(repo, PR, "security", "second")
    assert res.returncode == 0, res.stdout + res.stderr
    ids = _ids(repo, PR)
    assert len(ids) == len(set(ids)), ids


def test_suffixed_id_raises_the_mark(tmp_path):
    """Hand-suffixed ids like ``DEC-<pr>-4b`` occur in real records -- their
    counter must still count."""
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "first").returncode == 0
    _rewrite_frontmatter(repo, PR, lambda fm: fm["agent_decisions"][0].__setitem__("decision_id", f"DEC-{PR}-4b"))
    assert _decide(repo, PR, "security", "second").returncode == 0
    assert f"DEC-{PR}-5" in _ids(repo, PR)


# ── the record's own floor ───────────────────────────────────────────────────
def test_written_record_still_passes_lint(tmp_path):
    repo = _init(tmp_path)
    assert _decide(repo, PR, "backend", "a real decision").returncode == 0
    res = _run(repo, "lint", "--pr", str(PR), "--floor", "open")
    assert res.returncode == 0, res.stdout
