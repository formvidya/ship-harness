# methodology-harness

A portable, generic **development-methodology harness**: a full agent fleet +
CI gate suite + the context ledger, made config-driven and drop-in for any
project. The bigger sibling of `tools/harness/context-harness/` (which remains
usable standalone — this harness vendors it).

It was extracted from a real multi-service codebase, and every gate here exists
because something got through without it.

## The principle that makes it not theater

The review that produced this harness found the original repo's gates were
*advisory theater* — they ran and changed nothing because branch protection
didn't actually require them. So this harness ships gates **required by
default** AND `scripts/enforcement_check.py`, which derives the
must-be-required set from config and **fails if branch
protection doesn't actually require them**. "Required" becomes a verified fact,
not a sentence in a README.

```bash
# fail if an expected gate isn't actually required on main (ship as a CI job)
python tools/harness/methodology-harness/scripts/enforcement_check.py --verify
# write the expected required checks into branch protection
python tools/harness/methodology-harness/scripts/enforcement_check.py --apply
# just print what config says must be required
python tools/harness/methodology-harness/scripts/enforcement_check.py --list
```

## Layout

```
tools/harness/methodology-harness/
  config.example.yml     the EXTENDED .context/config.yml (superset of the ledger's)
  defaults/              per-language presets (python, dart, node) to merge into languages[]
  scripts/
    enforcement_check.py  verify/apply branch-protection required checks
    classify.py           change-risk classifier behind the change-review gate
    check_version_bumps.py, check_code_quality.py, build_ci_matrix.py
    run_sca_scan.py, run_security_scan.py, run_llm_review.py + their gates
    render_agents.py      renders the agent templates into .claude/agents/
  templates/
    agents/    support + engineer agent templates
    workflows/ CI gate templates (ci, code-quality, version-check, change-review,
               security-scan, security-sca, security-llm-review)
  install.py   renders templates + writes/verifies branch protection
  tests/       unit tests (enforcement logic and every gate script)
```

## Config

One extended `.context/config.yml` drives everything: a
single fill-once file, so a gate can never disagree with the config that is
supposed to describe it. Beyond the context-ledger keys it adds: `languages[]`
(the master switch for multi-language gates), `services[]`, `version_check`,
`gates` (enabled/required/check_name per gate), `deploy`, `agent_profiles`,
`observability`. See `config.example.yml`.

## Status

Shipped: the scaffold + extended config + `enforcement_check.py`; the
enforceable gate templates; the agent templates and their renderer. Deploy
safety is the remaining workstream.

The gates and agents were improved *as they were extracted* — the review's
fixes became the generic defaults (pinned deps, identity-bound approval, 3-dot
diffs, rollback) — so the harness ships known-good rather than reproducing the
footguns of the codebase it came from.
