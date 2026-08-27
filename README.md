# ship-harness

A portable, config-driven **development-methodology harness** for teams working
with coding agents: an institutional-memory ledger, a suite of CI gates that are
*actually required* rather than advisory, and a renderable agent fleet.

Nothing in the engine hard-codes a project string. Everything project-specific
lives in one fill-once file, `.context/config.yml`.

## Why this exists

A review of one team's CI found that its quality gates were **advisory theater**:
they ran, they reported, and they changed nothing, because branch protection did
not actually require any of them. A gate nobody is forced to pass is a slower
way of not having a gate.

So this harness ships its gates required by default, and includes
`enforcement_check.py`, which derives the must-be-required set from your config
and fails when branch protection does not match. "Required" becomes a verified
fact rather than a sentence in a README.

The same instinct runs through the rest of it. Agents write down *why* they
chose something, not just what they changed. Records are linted against a
substance floor before merge, so a record full of `TODO` fails the way a broken
build fails. Decisions get a closed-loop verdict once reality arrives.

## The two halves

**`context-harness/` — the ledger.** Captures, per change: intent, per-agent
decisions with rationale, architecture impact, test results, and the eventual
production outcome. Makes that history queryable *before* new work starts, and
enforces a record on every change through a CI gate.

Usable on its own. If all you want is institutional memory, install this half.

**`methodology-harness/` — the gates.** Code quality, semgrep, dependency CVEs,
a predictive LLM security reviewer, version-bump enforcement, change review with
identity-bound approval, and a selective CI matrix that runs only the jobs a
given diff can affect. Vendors the ledger.

## Install

Vendor this repository into your project at `tools/harness/`, then from your
repository root:

```bash
python tools/harness/install.py
```

The path matters: `tools/harness/` is currently assumed by the templates and
installers rather than configurable. The installer is idempotent — re-run it any
time; it will not overwrite a config you have filled in.

Then the manual steps it prints:

1. `pip install pyyaml`
2. Fill in `.context/config.yml` for your project.
3. `python tools/harness/methodology-harness/scripts/enforcement_check.py --apply`
   — make the gates genuinely required.
4. `python tools/harness/methodology-harness/scripts/render_agents.py --seed`
   — seed the generic support squad, then add your own engineer agents.
5. Optional: set a provider API key and flip `gates.security_llm` to blocking.

Step 3 needs a token with admin rights on the repository. Reading branch
protection is not available to the default CI token, so run it locally or from a
job with an elevated token — see [Known limitations](#known-limitations).

## Quickstart: the ledger

```bash
CTX=tools/harness/context-harness/ctx/ctx.py

python $CTX init --pr 142 --title "Rate-limit one-time passcodes" \
    --service billing-service --topic rate-limiting \
    --intent "Throttle OTP requests per account"

python $CTX decide --pr 142 --agent backend \
    --decision "sliding-window throttle in the shared cache" \
    --rationale "multi-replica safe; adds no new datastore"

python $CTX lint --pr 142
```

`ctx lint` defaults to the same floor the CI gate imposes, off the same
requirements table — green locally means green in CI.

Make the Context Check a required status check in branch protection. It is the
only layer that covers a human merging through the web UI.

## Requirements

Python 3.11 or newer, and `pyyaml`. The LLM security reviewer additionally needs
`anthropic` and/or `openai` plus a provider API key; without one it degrades to
no-review rather than failing your build.

## Known limitations

Worth knowing before you adopt it, because each of these has bitten someone:

- **The install path is a contract.** `tools/harness/` is hard-coded in the
  templates and installers. Vendoring elsewhere will not work yet.
- **`enforcement_check.py --verify` needs privilege.** Reading branch protection
  requires admin scope that the default GitHub Actions token does not have, so
  it cannot run as an ordinary CI job on a default setup.
- **CI fan-in is not the merge signal.** An aggregate "CI Success" job can only
  wait on jobs in its own workflow file. Gates living in other workflow files
  are invisible to it, and a green aggregate can sit next to a red required
  check. Keep the authoritative list in branch protection.
- **A skipped required check counts as passing.** If you gate an expensive job
  on `draft == false`, add `ready_for_review` to its trigger types, or the check
  never re-runs when the PR is marked ready and the gate silently passes.
- **The templates lag.** They are a known-good starting point, not a mirror of
  every hardening the origin repo has since applied.

## Getting in touch

**Open an issue** — that is the front door, and the fastest way to get an
answer. Bug reports, adoption questions, and "this limitation bit me" reports
are all welcome; the limitations above exist because someone hit them.

Pull requests are welcome too. Two things make them easy to accept: keep the
tests green (`pytest methodology-harness/tests/ context-harness/tests/`), and
say *why* in the commit message — this project's own thesis is that the
reasoning behind a change is worth more than the change.

Maintained by [@middle50](https://github.com/middle50) under the
[FormVidya](https://github.com/formvidya) organization. For anything you would
rather not put in a public issue — a suspected vulnerability especially —
message the maintainer through GitHub.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
