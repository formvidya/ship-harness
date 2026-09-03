# ctx CLI usage

All commands read `.context/config.yml` and operate on records under
`<ledger.records_dir>`.

## `ctx init` — create a record

```bash
python tools/harness/context-harness/ctx/ctx.py init --pr 142 \
  --title "Per-IP rate limit on OTP request" \
  --service billing-service --topic rate-limiting \
  --prd SPEC-001 --intent "Throttle OTP requests to prevent abuse"
```

Creates that PR's record — `CTX-` plus the PR number zero-padded to four digits,
`.md` — at `status: open`, with the section skeleton already scaffolded.

## `ctx decide` — record an agent decision (closed-loop)

```bash
python tools/harness/context-harness/ctx/ctx.py decide --pr 142 --agent billing \
  --decision "sliding-window Redis throttle, ip+route, 60s TTL" \
  --rationale "multi-replica safe; no new datastore (ADR-008)" \
  --alternative "fixed-window" --alternative "in-app token bucket" \
  --reversibility easy [--supersedes DEC-<pr>-<n>]
```

Appends to `agent_decisions[]` and mints a matching `pending` verdict in
`outcome.decisions[]`. IDs auto-number as `DEC-<pr>-<n>` — the PR the decision
was made on, then its ordinal within that record — unless `--id` is given.

## `ctx set` — set frontmatter fields

```bash
python tools/harness/context-harness/ctx/ctx.py set --pr 142 \
  risk_level=LOW status=merged test_results.passed=191 test_results.failed=0
```

Dotted keys create nested maps. Values are coerced (int / float / bool / null /
string).

## `ctx lint` — validate against the schema floor

```bash
python tools/harness/context-harness/ctx/ctx.py lint --pr 142   # one record
python tools/harness/context-harness/ctx/ctx.py lint            # all records
```

Exit 0 = valid, 1 = problems (listed per record). Required fields escalate with
`status` (see `SCHEMA.md`).

**`ctx lint --pr N` is the local equivalent of the `Context Check` CI gate.** It
defaults to `--floor merged` — the same floor, from the same requirements table,
that `check_context_record.py` imposes on a PR — and it likewise requires a
`context-keeper` decision, so a green `ctx lint --pr N` means a green gate. Pass
`--floor status` to lint at the record's own `status` instead; that is a weaker
check and will let a record through that CI rejects.

The **bare sweep** (`ctx lint`, no `--pr`) deliberately does *not* require the
Historian, and says `historian not required` in its summary when it did not. The
requirement is a rule about how records are written from now on, and applying it
to a whole back catalogue would turn every record written before the rule into a
failure — a report with hundreds of entries nobody can act on, which is how a
gate becomes wallpaper. Use the sweep to find records that are malformed; use
`--pr N` to answer "can this merge?".

## `ctx assemble` — Historian finalize

```bash
python tools/harness/context-harness/ctx/ctx.py assemble --pr 142
```

Deterministic (no model calls): validates the record is schema-complete for
its lifecycle. On `CONTEXT-OK` it writes `.claude/context-recorded-142` (the
marker the release-manager checks). On `CONTEXT-INCOMPLETE` it lists the missing
sections and exits 1.

`assemble` also requires the record to carry a decision attributed to
`context-keeper`, because this command *is* the Historian's finalize step and
the marker it writes is what the release-manager trusts. Every other decision in
a record is filed by the agent that made the change it describes, so without
this a record can reach merge having been read by nobody but its author.

What that requirement does and does not prove is worth being precise about: it
establishes that an author-independent reader was invited and left a signature.
It cannot establish that the reading was any good. Judging the *substance* of
the record — whether a claim is one the diff actually supports — remains the
context-keeper agent's own job, and no deterministic gate can do it, because a
well-written false claim and a well-written true one are the same shape.

## `ctx bootstrap` — render agent profiles from config

```bash
python tools/harness/context-harness/ctx/ctx.py bootstrap          # render templates -> canonical paths
python tools/harness/context-harness/ctx/ctx.py bootstrap --check  # verify in sync (exit 1 if drifted)
```

Substitutes `{{tokens}}` in `templates/agents/*.md` from `.context/config.yml`
(the config is the source, the agent files are rendered output). Re-run after
editing the config. `--check` is suitable as a CI guard that the committed agent
files match the config.

## `ctx verdict` — close the loop on a decision

```bash
python tools/harness/context-harness/ctx/ctx.py verdict --pr 142 \
  --decision DEC-<pr>-<n> --verdict good --evidence "zero regressions in prod"
```

Sets the terminal verdict + evidence on one `outcome.decisions[]` entry
(`good|bad|mixed|superseded|unreconciled|pending`). This is the deterministic
primitive the deploy write-back and the weekly reconcile use to record whether a
decision proved out.

## `ctx reduce` — fold into the Tier-2 synthesis layer

```bash
python tools/harness/context-harness/ctx/ctx.py reduce              # re-derive from all records
python tools/harness/context-harness/ctx/ctx.py reduce --pr 142     # guard: require PR 142's record first
```

Deterministic: derives the **bounded** Tier-2 layer from the Tier-1
records — per-service digests (`docs/context/digests/<service>.md`), the
topic-keyed `DECISION_REGISTRY.md`, the `OPEN_LOOPS.md` board, and `TOPICS.md`.
Superseded decisions and closed carry-forwards drop out (bounded by
subtraction). Runs the architecture-drift detector: a change that
establishes/alters a pattern not reflected in the reference architecture becomes
an open loop owned by docs. Same inputs -> byte-identical output.

## `ctx reconcile` — weekly closed-loop pass

```bash
python tools/harness/context-harness/ctx/ctx.py reconcile
```

Re-derives Tier 2 (same as `reduce`) and adds the time-dependent pass: flags
decisions past their risk-tuned horizon as `unreconciled`, and surfaces
off-list topics for curation. Wired as a weekly cron in
`.github/workflows/context-reconcile.yml`, which opens a triage issue. The
advisory-LLM "propose, human disposes" step is the context-keeper agent's job,
not this script's.

## `ctx query` — the before-dev briefing

```bash
python tools/harness/context-harness/ctx/ctx.py query \
  --service billing-service \
  --files services/billing-service/src/api/invoices.py \
  --intent "add rate limiting to OTP endpoint"
python tools/harness/context-harness/ctx/ctx.py query --service billing-service --json  # for an agent
```

The payoff: before you touch a service, this returns a bounded (<=400-word)
briefing of the context that should shape the change — the current architecture
pattern, the decisions that touched this area with their good/bad verdicts
(**`[BAD]` pinned first with "DO NOT REPEAT"**, kept even when superseded), and
the open loops. It reads the same fold the Tier-2 digests do, so they never
disagree. Running it writes `.claude/context-consulted-<branch>` as a side
effect — the pre-dev hook uses that to tell the queried-first path from the one
that skipped it.

**Enforcement (the "before writing code" rule):**

- The `pre-dev-context.sh` Claude Code hook (wired in `.claude/settings.json`,
  matcher `Edit|Write`) blocks the first code-root edit on a branch until
  `ctx query` has run. Override: `CTX_SKIP_PREDEV=1`.
- Every engineer agent's contract (`docs/agents/CONTEXT_LEDGER.md`) makes
  `ctx query` the mandatory first step.
- The required `Context Check` CI gate still enforces the record at merge —
  the hook is a local nudge, the CI gate is the binding enforcement.

## `ctx index` — recall index for query (optional)

```bash
python tools/harness/context-harness/ctx/ctx.py index                 # auto: lexical (+ embeddings if installed)
python tools/harness/context-harness/ctx/ctx.py index --backend lexical
```

Builds a **gitignored, regenerable** index under `ledger.index_dir` that gives
`ctx query` better recall. Two backends:

- **lexical (default, zero-dependency):** a BM25 inverted index over each
  decision's text/rationale/topics/service, plus query-time **alias expansion**
  from `topic_aliases` in the config (e.g. `throttling -> rate-limiting`). This
  is what ships and what CI exercises — deterministic (build twice = identical).
- **embeddings (optional):** if `sentence-transformers` is installed it also
  stores MiniLM vectors for true semantic similarity. Never required — absent
  the library the index is lexical-only; absent the index entirely, `query`
  degrades to plain substring matching. The portable harness gains no heavy
  dependency.

The index is a cache, never a source of truth — delete `ledger.index_dir/` and
recall falls back with no data loss. Rebuild after records change (or wire
`ctx index` into the weekly `context-reconcile` cron).
