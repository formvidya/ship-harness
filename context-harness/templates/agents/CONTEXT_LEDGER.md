# Context Ledger — Section-Ownership Contract

**Generated from `.context/config.yml` — edit the config, re-run `ctx bootstrap`.**

Every change to **{{project.name}}** that touches a code root ({{code_roots}})
gets one context record at `{{ledger.records_dir}}/CTX-{pr}.md`. You do not write
that file by hand — you emit your section through the `ctx` CLI, and the
Historian ({{historian_agent}}) assembles and validates it.

This is the contract: **who writes which section, and when.** It is enforced by
the `Context Check` CI gate (a code change with no valid record is blocked) and,
for agents, by the query-before-dev hook.

## Roles → your project's agents

| Generic role | Bound agent(s) |
|--------------|----------------|
{{role_table}}

## Who writes what

| Section / field | Role | When | CLI |
|-----------------|------|------|-----|
| `## Intent`, `acceptance_criteria` | intent | at spec/PRD approval | `ctx init --pr N --title "..." --intent "..."` |
| `## What Was Done`, `## Architecture Used` | engineer | after implementing | `ctx set --pr N ...` |
| `agent_decisions[]` (one per real decision) | **each deciding agent** | as it decides | `ctx decide --pr N --agent <you> --decision "..." --rationale "..."` |
| `test_results`, `security_findings` | testing / security | support stage | `ctx set --pr N test_results.passed=...` |
| `risk_level`, `blast_radius.*` | risk | change-review stage | `ctx set --pr N risk_level=LOW` |
| `build_retro.*` | retro | final stage | `ctx set --pr N build_retro.gate_verdict=PASS` |
| assemble + substance check + `## Closed-Loop Outcome` | historian | final stage | `ctx assemble --pr N` |

## Two rules for every agent

1. **Before writing code** (engineer roles), run — as your FIRST step —
   `python tools/harness/context-harness/ctx/ctx.py query --service <svc> --files <path> --intent "<what you're doing>"`
   and read every `[BAD]` decision (pinned first, "DO NOT REPEAT") and open
   carry-forward it returns. Do not repeat a flagged bad decision without
   recording in `agent_decisions` why this time is different. The pre-dev hook
   blocks the first code-root edit until you have run this on the branch.
2. **Record your decisions as you make them.** A decision with no `rationale`,
   or a non-trivial choice with no `alternatives`, will be flagged by the
   Historian's substance check and — for HIGH/CRITICAL changes — block
   `release-approved` until a human reviews it.

## Override

If a change genuinely needs no record (rare), include `{{skip_marker}}` in a
commit message. It is logged for audit. Use sparingly.
