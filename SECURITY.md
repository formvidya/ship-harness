# Security policy

## Supported versions

`main` only. There are no released versions and no backports; fixes land on
`main` and adopters re-vendor.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](../../security/advisories/new).
It is private between you and the maintainers, so please use it rather than
opening a public issue.

Useful things to include: what an attacker gains, the smallest reproduction you
have, and which file or gate is involved.

## What to expect

This project is maintained on a best-effort basis. **There is no response-time
commitment and no bug bounty.** That is a deliberate statement rather than an
omission: inviting reports without saying what happens next imports expectations
nobody agreed to. You should hear back, but the honest answer is that it depends
on what else is on fire that week.

If a report turns out to be a real vulnerability, the fix and an advisory go on
`main`, and you get credit unless you would rather not.

## Scope, and what this project can and cannot promise you

This harness is a set of CI gates and a record-keeping tool. Its own attack
surface is small: it reads your repository, writes files, calls the GitHub API
with whatever token you give it, and — if you enable the LLM reviewer — sends
diffs to a model provider you configure.

Worth reporting: a gate that can be made to pass while its check does not
actually run; a path where a token or a secret reaches a log, a report, or a
model prompt; anything that lets a pull request weaken the gates that guard it.

**A gate reporting green while doing nothing is a security bug here, not a
papercut.** Several of this project's own tests exist because exactly that
happened.

Not in scope: the gates cannot promise your repository is secure. They are
scaffolding for a review process, and every one can be bypassed by a maintainer
who wants to bypass it.
