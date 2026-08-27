# context-harness

A portable institutional-memory ledger for any project. Captures, per change,
the intent → per-agent decisions → architecture → build retro → test results →
feedback → production/UAT outcome, keeps a closed-loop good-vs-bad verdict on
every decision, makes that context **queryable before new work**, and **enforces
a record on every change**.

Design + rationale live with the code: `docs/SCHEMA.md` (the record model and
the lifecycle floors), `docs/USAGE.md` (every command, with the reasoning behind
each), `docs/INSTALL.md` (dropping it into a repo).

## What makes it portable

Nothing in this folder contains a project-specific string. Everything about a
given product lives in one fill-once file, `.context/config.yml` at the repo
root. The engine reads it and hard-codes nothing — so this folder lifts into any
repo unchanged.

Paths below (and throughout the docs) are where the harness sits **after
install**, inside the repo that uses it — `install.py` puts it at
`tools/harness/context-harness/`, and the CI gate, hook and agent templates all
address it there.

```
tools/harness/context-harness/
  ctx/
    config.py                 loads + validates .context/config.yml
    schema.py                 context-record schema + linter (the "floor")
    ctx.py                    the CLI (init/set/decide/lint/assemble/bootstrap/
                              verdict/reduce/reconcile/query/index)
    check_context_record.py   the enforcement gate (CI + pre-commit)
    reduce.py                 Tier-2 synthesis + architecture-drift detector
    query.py, index.py        the before-dev briefing and its optional index
  templates/
    config.example.yml        copy to .context/config.yml and fill in
    workflows/context-check.yml   the CI gate (render into .github/workflows/)
    agents/, hooks/           write-path + pre-dev hook templates
  install.py                  turnkey scaffolder (see docs/INSTALL.md)
  docs/  SCHEMA.md  INSTALL.md  USAGE.md
```

## Quickstart (existing repo)

`python tools/harness/context-harness/install.py` does the scaffolding below
(and the hook, PR template and `.gitignore` entries) in one idempotent pass —
see `docs/INSTALL.md`. By hand:

```bash
cp tools/harness/context-harness/templates/config.example.yml .context/config.yml
$EDITOR .context/config.yml                 # ~8 fields: name, languages, code_roots, ledger paths
cp tools/harness/context-harness/templates/workflows/context-check.yml .github/workflows/
pip install pyyaml

# create + validate a record for a PR
python tools/harness/context-harness/ctx/ctx.py init --pr 142 --title "Rate-limit OTP" \
    --service billing-service --topic rate-limiting --intent "Throttle OTP requests"
python tools/harness/context-harness/ctx/ctx.py decide --pr 142 --agent billing \
    --decision "sliding-window throttle" --rationale "multi-replica safe; no new datastore"
python tools/harness/context-harness/ctx/ctx.py lint --pr 142
```

`ctx lint` defaults to the same `--floor merged` the CI gate imposes, off the
same requirements table — green locally means green in `Context Check`.

Make **`Context Check / per-change record present & valid`** a required status
check in branch protection — it is the only layer that covers a human merging
via the web UI.

## Status

Complete and in use. The event layer and enforcement (`init / set / decide /
lint` plus the CI and pre-commit gate), the Historian write-path (`assemble`,
`bootstrap`), the Tier-2 synthesis and closed-loop passes (`verdict`, `reduce`,
`reconcile`) and the before-dev briefing (`query`, with the optional `index`)
all ship here. `docs/USAGE.md` documents each command; the only optional piece
is the embeddings backend behind `ctx index`, which is never required.
