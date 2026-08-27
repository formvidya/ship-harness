<!-- Keep the summary tight; the context checklist keeps the ledger honest. -->

## Summary

<!-- What changed and why. -->

## Context ledger

<!-- Required when this PR changes a code root (see .context/config.yml `code_roots`).
     Docs / CI / tooling-only PRs are exempt. -->

- [ ] I consulted the ledger before coding (`python tools/harness/context-harness/ctx/ctx.py query --service <svc> --intent "..."`) and read the `[BAD]` decisions it returned.
- [ ] This PR adds/updates its context record — or is exempt (docs/tests/CI only), or carries a logged `[skip-context]`.

## Checklist

- [ ] Tests added/updated and passing
- [ ] Docs updated if behavior changed
