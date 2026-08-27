# Context Record Schema

One record per change unit (per PR), at `<ledger.records_dir>/CTX-{pr}.md`
(zero-padded to 4 digits). YAML frontmatter + prose sections. Validated by
`ctx lint` / the CI gate against a **lifecycle-aware floor** — it kills empty
shells, but does not judge rationale quality; that is the substance funnel
the context-keeper agent's separate advisory step.

## Lifecycle

`open → in_review → merged → deployed → reconciled`. Each state adds required
fields; the linter checks all fields up to and including the **floor** — the
later of the record's own `status` and the `--floor` the caller imposes.

### Floors

`ctx lint` and `check_context_record.py` share one `--floor` vocabulary and one
requirements table (`schema.FLOOR_CHOICES` / `schema.requirements_for`), and
both **default to `merged`** wherever a record is validated for merge:

| Caller | Default floor |
|--------|---------------|
| `ctx lint` | `merged` |
| `ctx assemble` | `merged` |
| `check_context_record.py` (CI) | `merged` |
| `check_context_record.py --staged` (pre-commit) | `status` |

`--floor status` imposes no floor and lints at the record's own `status`. Since
a pre-merge record sits at `open` (`lifecycle-sync` only advances it after the
merge commit lands), that is a **strictly weaker check than CI** — it skips
`services_affected`, `agent_decisions`, `test_results`, `risk_level`,
`## What Was Done`, and `## Architecture Used`. It used to be the `ctx lint`
default, which is how two separate PRs each linted clean locally and then failed
the `Context Check` gate in CI — the reason the two callers now share one
requirements table and one default.

## Frontmatter

| Field | Required at | Notes |
|-------|-------------|-------|
| `ctx_id` | open | `CTX-` + the PR number, zero-padded to 4 digits |
| `pr_number` | open | natural key |
| `title` | open | one line |
| `status` | open | a lifecycle state |
| `services_affected` | merged | code areas touched |
| `topics` | (recommended) | Tier-2 reduce keys; seed list in `<ledger.topics>` |
| `agent_decisions` | merged | list; each needs `decision_id`, `agent`, `decision`, `rationale` |
| `architecture_used` | (recommended) | `pattern_ref`, `arch_doc_ref`, `establishes_pattern`, `changes_pattern` |
| `test_results` | merged | structured pass/fail/new/coverage |
| `risk_level` | merged | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` (drives horizon + review) |
| `outcome.decisions[]` | (closed-loop) | one `{decision_id, verdict, evidence}` per decision; verdict in `pending/good/bad/mixed/superseded/unreconciled` |

## Required body sections

| Section | Required at |
|---------|-------------|
| `## Intent` | open |
| `## What Was Done` | merged |
| `## Architecture Used` | merged |

(Plus recommended `## Test Results`, `## Build-Process Retro`, `## Risk / Blast
Radius`, `## Feedback Events`, `## Closed-Loop Outcome`. `ctx init` scaffolds
all of them with `_TODO_` placeholders, so the shape of a full record is
whatever that command writes.)

## Closed-loop invariant

A `decision_id` stays OPEN until its `outcome.decisions[]` entry has a terminal
`verdict` with non-null `evidence`. Past the risk-tuned review horizon an
open decision auto-marks `unreconciled` — a first-class, scorecard-worsening,
git-visible state. `ctx decide` mints the matching `pending` verdict
automatically.
