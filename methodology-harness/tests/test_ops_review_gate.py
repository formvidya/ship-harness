"""Unit tests for the ops-review gate.

Covers the three pieces whose logic must not regress:
  * ops_marker_gate._is_mutating / _read_input — the mutating-vs-read-only
    classification and the stdin-JSON-first / argv-fallback input contract.
  * stop_review_gate._count_records / _summary — the op count and phrasing in
    the block message, which is what a human reads to size up a turn's blast
    radius. The marker holds one RECORD per command, not one line, and the two
    scripts must not drift on that format.
  * check_security_review._record_has_security_decision — the PASS signal
    (an 'agent: security' decision in the PR's context record).

The classification is the load-bearing bit: a read-only command (e.g. an
incident `kubectl get`) must NEVER be logged, and a mutation must ALWAYS be
logged — because the Stop hook keys off that marker.

Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ops_marker_gate as omg  # noqa: E402
import stop_review_gate as srg  # noqa: E402


# ── mutating vs read-only classification ────────────────────────────────────
def test_kubectl_mutations_are_flagged():
    for cmd in (
        "kubectl patch secret foo -n bar -p x",
        "kubectl apply -f deploy.yaml",
        "kubectl delete pod x",
        "kubectl set image deployment/api api=repo:tag",
        "kubectl scale deployment/search --replicas=30",
        "kubectl rollout undo deployment/billing",  # incident cmd: logged, not blocked
        "kubectl exec -it pod -- sh",
        "kubectl drain node-1",
    ):
        assert omg._is_mutating(cmd), cmd


def test_other_tool_mutations_are_flagged():
    for cmd in (
        "doctl kubernetes cluster create foo",
        "doctl compute droplet delete 123",
        "helm upgrade api ./chart",
        "helm rollback api 3",
        "gh workflow run deploy.yml",
        "gh secret set PROD_TOKEN",
        "gh secret delete PROD_TOKEN",
        "gh api repos/o/r/issues -X POST -f title=z",
        "gh api repos/o/r --method DELETE",
        "infisical secrets set FOO=bar",
        "infisical secrets delete FOO",
    ):
        assert omg._is_mutating(cmd), cmd


def test_read_only_is_not_flagged():
    for cmd in (
        "kubectl get pods",
        "kubectl logs deploy/api",
        "kubectl describe pod x",
        "kubectl top nodes",
        "kubectl version",
        "kubectl api-resources",
        "kubectl explain pod",
        "kubectl config view",
        "doctl kubernetes cluster list",
        "doctl compute droplet get 1",
        "gh pr view 5",
        "gh run list",
        "gh pr list",
        "curl https://example.com/health",
        "ls -la",
        "git status",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_incidental_mutating_keyword_is_not_flagged():
    # A read-only get piped through grep that mentions a mutating word must not
    # owe a review — pipe segments are classified independently, and the
    # MUTATING_OPS patterns are precise enough not to match bare flags.
    assert not omg._is_mutating("kubectl get pods | grep delete")
    assert not omg._is_mutating("gh api repos/o/r --jq '.default_branch'")


def test_gh_api_without_mutating_method_is_read_only():
    assert not omg._is_mutating("gh api repos/o/r")
    assert not omg._is_mutating("gh api repos/o/r -X GET")


# ── gh classification (recorded regression) ─────────────────────────────────
# The old read-only pattern `\bgh\s+(pr|run|view|list)\b` matched *every*
# `gh pr` subcommand and was consulted before MUTATING_OPS, so `gh pr merge`
# et al. never owed a review, and `gh issue` mutations matched nothing at all.
def test_gh_pr_mutations_are_flagged():
    for cmd in (
        "gh pr merge 411 --squash --delete-branch",
        "gh pr close 400",
        "gh pr edit 402 --add-label ops",
        "gh pr ready 407",
        "gh pr create --draft --title x --body y",
        "gh pr review 405 --approve",
        "gh pr reopen 399",
        "gh pr comment 401 --body done",
        "gh pr lock 398",
        "gh pr update-branch 403",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_issue_mutations_are_flagged():
    for cmd in (
        "gh issue close 380",
        "gh issue edit 381 --add-label bug",
        "gh issue create --title x --body y",
        "gh issue comment 382 --body triaged",
        "gh issue reopen 383",
        "gh issue delete 384",
        "gh issue transfer 385 o/other-repo",
        "gh issue pin 386",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_release_workflow_run_mutations_are_flagged():
    for cmd in (
        "gh release create v3.2.1 --notes x",
        "gh release edit v3.2.1 --draft=false",
        "gh release delete v3.2.0",
        "gh release delete-asset v3.2.0 app.apk",
        "gh release upload v3.2.1 app.apk",
        "gh workflow run deploy.yml",
        "gh workflow enable ci.yml",
        "gh workflow disable ci.yml",
        "gh run cancel 123456",
        "gh run rerun 123456 --failed",
        "gh run delete 123456",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_api_mutations_are_flagged():
    for cmd in (
        "gh api repos/o/r/issues -X POST -f title=z",
        "gh api repos/o/r/issues/5 --method PATCH -f state=closed",
        "gh api repos/o/r --method DELETE",
        # fields with no explicit method: gh defaults the request to POST
        "gh api repos/o/r/issues -f title=x",
        "gh api repos/o/r/labels -F name=@file",
        "gh api repos/o/r/issues/5/comments --field body=hi",
        "gh api repos/o/r/releases --input payload.json",
        "gh api graphql -f query='mutation { closeIssue(input: {issueId: \"I_1\"}) { issue { id } } }'",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_read_only_forms_are_not_flagged():
    for cmd in (
        "gh pr view 411",
        "gh pr list --state open",
        "gh pr checks 411",
        "gh pr diff 411",
        "gh pr status",
        "gh run view 123456 --log",
        "gh run list --limit 5",
        "gh run watch 123456",
        "gh run download 123456",
        "gh issue view 380",
        "gh issue list --label bug",
        "gh issue status",
        "gh release view v3.2.1",
        "gh release list",
        "gh release download v3.2.1",
        "gh api repos/o/r/pulls",
        "gh api repos/o/r -X GET -f per_page=100",
        "gh api graphql -f query='query { viewer { login } }'",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_mutating_wins_over_read_only_in_same_segment():
    # Ordering regression: even a clause that matches a read-only pattern must
    # be flagged when it also contains an explicit mutation.
    assert omg._is_mutating("gh pr merge 411 && gh pr view 411")
    assert omg._is_mutating("kubectl delete pod $(kubectl get pods -o name)")
    assert omg._is_mutating("gh issue list --label bug && gh issue close 380")


# ── security-review hardening (an adversarial pass over the gh rows) ────────
def test_gh_interspersed_flags_still_flagged():
    # cobra resolves the subcommand and verb past flags, so repo-scoped forms
    # are valid gh invocations and must not evade the patterns.
    for cmd in (
        "gh -R acme/example-repo pr merge 411 --squash",
        "gh --repo acme/example-repo issue close 380",
        "gh pr --repo acme/example-repo merge 411",
        "gh -R o/r workflow run deploy.yml",
        "gh --repo=o/r release delete v3.2.0",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_create_aliases_and_develop_are_flagged():
    # `new` is a real alias of `create`; `issue develop` creates a remote branch.
    for cmd in (
        "gh pr new --draft --title x --body y",
        "gh issue new --title x --body y",
        "gh issue develop 380 --checkout",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_api_method_spellings_are_flagged():
    # gh accepts `--method=X`, attached `-XPOST`, and lowercase values (it
    # upcases them itself) — all reach the wire as real mutations.
    for cmd in (
        "gh api repos/o/r --method=DELETE",
        "gh api repos/o/r -XDELETE",
        "gh api repos/o/r -X delete",
        "gh api repos/o/r/issues -X post -f title=x",
        "gh api repos/o/r/contents/f -X PUT -f message=x",
        "gh api repos/o/r/pages --method=put --input site.json",
        "gh api repos/o/r/issues --raw-field body=@notes.md",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_api_explicit_get_is_not_flagged():
    # The mirror image: an explicit GET must stay read-only in every spelling,
    # including with fields (they become query params).
    for cmd in (
        "gh api repos/o/r --method=GET -f per_page=100",
        "gh api repos/o/r -X GET -f per_page=100",
        "gh api repos/o/r -XGET",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_gh_api_graphql_multiline_mutation_is_flagged():
    # Real mutation documents are pretty-printed across lines; the newline
    # segment split must not scatter the `mutation` keyword away from `gh api`.
    pretty = (
        "gh api graphql -f query='\n"
        "mutation {\n"
        '  mergePullRequest(input: {pullRequestId: "PR_1"}) { clientMutationId }\n'
        "}'"
    )
    assert omg._is_mutating(pretty)
    heredoc = (
        "gh api graphql --input - <<'EOF'\n"
        '{"query":"mutation { closeIssue(input: {issueId: \\"I_1\\"}) { clientMutationId } }"}\n'
        "EOF"
    )
    assert omg._is_mutating(heredoc)


def test_gh_variable_cache_repo_mutations_are_flagged():
    for cmd in (
        "gh variable set DEPLOY_ENV --body prod",
        "gh variable delete DEPLOY_ENV",
        "gh cache delete --all",
        "gh repo edit --default-branch main",
        "gh repo delete o/r --yes",
        "gh repo create o/new --private",
        "gh repo rename newname",
        "gh repo archive o/r",
        "gh repo sync",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_lock_pin_variants_are_flagged():
    for cmd in (
        "gh pr unlock 398",
        "gh issue unpin 386",
        "gh issue lock 387 --reason spam",
        "gh issue unlock 387",
    ):
        assert omg._is_mutating(cmd), cmd


def test_gh_help_and_variable_reads_are_not_flagged():
    for cmd in (
        "gh release --help",
        "gh release -h",
        "gh variable list",
        "gh cache list",
        "gh repo view acme/example-repo",
        "gh repo list acme",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_kubectl_reads_with_mutating_words_in_names_are_not_flagged():
    # The kubectl verb is anchored to the first non-flag token, so mutating
    # words inside resource names, selectors, or flags must not flag a read.
    for cmd in (
        "kubectl logs job/migrate-create-indexes -n platform",
        "kubectl describe pod backup-delete-old-1-abcde",
        "kubectl get pods -l job-name=db-create-schema",
        "kubectl get nodes --label-columns=topology.kubernetes.io/zone",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_kubectl_flags_before_verb_still_flagged():
    assert omg._is_mutating("kubectl --context prod delete pod x")
    assert omg._is_mutating("kubectl -n platform rollout restart deploy/api")


def test_mutation_chained_after_read_is_flagged():
    # Regression: a mutation chained after a read-only command must still owe a
    # review — segments are classified independently, so the read-only leading
    # clause does not shadow a later mutation.
    assert omg._is_mutating("kubectl get pods && kubectl delete pod x -n prod")
    assert omg._is_mutating("date; doctl x; kubectl patch secret s -n n -p y")
    assert omg._is_mutating("echo manifest | kubectl apply -f -")
    # ...but a read piped into a keyword-bearing filter still must not.
    assert not omg._is_mutating("kubectl get pods | grep delete")


def test_empty_command_is_not_mutating():
    assert not omg._is_mutating("")
    assert not omg._is_mutating("   ")


# ── git / filesystem classification ─────────────────────────────────────────
# MUTATING_OPS had no git coverage at all: of ~15 mutating actions in one
# measured batch exactly one was logged, and a file deletion plus a stash of
# unattributed work were both justified only after the fact. These tests pin
# each destructive git/fs form on the flagged side and its read-only sibling on
# the unflagged side, the same both-sides shape the gh rows use.
def test_git_history_rewrites_are_flagged():
    for cmd in (
        "git rebase -i HEAD~3",
        "git rebase --onto main feature",
        "git rebase --continue",
        "git rebase --abort",
        "git pull --rebase origin main",
        "git filter-branch --tree-filter 'rm -f secrets.env' HEAD",
        "git filter-repo --path secrets.env --invert-paths",
        "git reflog expire --expire=now --all",
        "git reflog delete HEAD@{2}",
        "git gc --prune=now",
        "git update-ref -d refs/heads/old",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_working_tree_discards_are_flagged():
    for cmd in (
        "git reset --hard HEAD~3",
        "git reset --hard origin/main",
        "git reset HEAD~1 --hard",
        "git reset --merge",
        "git reset --keep HEAD~1",
        "git checkout -- services/billing-service/src/api/invoices.py",
        "git checkout HEAD -- docs/context/records/REC-0428.md",
        "git checkout .",
        "git checkout -f main",
        "git checkout --force main",
        "git checkout --ours services/x.py",
        "git checkout --theirs services/x.py",
        "git checkout -p services/x.py",
        "git checkout -B agent/reset-branch origin/main",
        "git switch --discard-changes main",
        "git switch -f main",
        "git restore services/checkout-service/src/models/order.py",
        "git restore --staged docs/specs/SPEC-101.md",
        "git restore --source=HEAD~2 --worktree services/x.py",
        "git mv services/x/api/old.py services/x/api/new.py",
        "git clean -fd",
        "git clean -xdf tools/",
        "git clean --force",
        "git rm services/x/api/legacy.py",
        "git rm -r --cached infrastructure/",
        "git worktree remove ../wt-feature-x",
        "git worktree prune",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_stash_hiding_work_is_flagged():
    # The observed failure mode: uncommitted work stashed out of sight with
    # no record. Default-flagged, so a future subcommand lands on the safe side.
    for cmd in (
        "git stash",
        "git stash -u",
        "git stash push -m wip",
        "git stash push -- apps/mobile/lib",
        "git stash save 'wip'",
        "git stash drop stash@{0}",
        "git stash clear",
        "git stash create",
        "git stash store abc123",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_ref_and_remote_deletions_are_flagged():
    for cmd in (
        "git push --force origin feature",
        "git push --force-with-lease",
        "git push --force-if-includes origin HEAD",
        "git push -f origin main",
        "git push origin --delete stale-branch",
        "git push origin -d stale-branch",
        "git push origin :stale-branch",
        # `+ref` is the refspec spelling of a force push
        "git push origin +main",
        "git push origin +HEAD:refs/heads/main",
        "git push --mirror backup",
        "git push --prune origin",
        "git branch -D agent/abandoned-work",
        "git branch -d merged-feature",
        "git branch -f main HEAD~5",  # force-moves an existing branch ref
        "git branch --delete --force old",
        "git branch -m old-name new-name",
        "git branch --move old new",
        "git tag -d v3.2.0",
        "git tag --delete v3.2.0",
        "git tag -f v3.2.1 HEAD",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_global_flags_before_subcommand_still_flagged():
    # git resolves the subcommand past global options, exactly like the gh case
    # fixed above — an anchored pattern with no flag gap would be evaded.
    for cmd in (
        "git -C /repo push --force origin main",
        "git --no-pager reset --hard HEAD~1",
        "git -c user.name=x rebase main",
        "git -C /repo stash drop",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_reads_are_not_flagged():
    for cmd in (
        "git status",
        "git status --porcelain",
        "git log --oneline -20",
        "git log -p services/billing-service",
        "git diff",
        "git diff --stat main...HEAD",
        "git diff -- docs/specs/SPEC-101.md",
        "git show HEAD --name-only",
        "git rev-parse --abbrev-ref HEAD",
        "git rev-parse --show-toplevel",
        "git rev-list --count main..HEAD",
        "git branch",
        "git branch --list 'agent/*'",
        "git branch -a",
        "git branch -r",
        "git branch -vv",
        "git branch --show-current",
        "git branch --merged main",
        "git branch --no-merged main",
        "git branch --contains HEAD",
        "git branch --format='%(refname:short)'",
        "git fetch origin",
        "git fetch --all --prune",
        "git blame services/checkout-service/src/app.py",
        "git ls-files tools/harness",
        "git ls-remote --heads origin",
        "git describe --tags",
        "git shortlog -sn",
        "git stash list",
        "git stash show -p stash@{0}",
        "git worktree list",
        "git remote -v",
        "git config --get user.email",
        "git tag -l 'v3.*'",
        "git clean -nd",
        "git clean --dry-run",
        "git rm --dry-run tools/x.py",
        "git mv --dry-run a.py b.py",
        "git count-objects -vH",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_git_normal_workflow_is_not_logged():
    # A recorded judgement call: additive, reflog-recoverable, fires-every-turn commands are
    # deliberately kept off the owed log — the change they produce is already
    # gated by `gh pr create` / `gh pr merge` (both flagged). This test is the
    # pin on that decision: flipping any of these onto the logged side must be
    # a deliberate edit here, not a silent side effect of a pattern tweak.
    for cmd in (
        "git add -A",
        "git add tools/harness/methodology-harness/scripts/ops_marker_gate.py",
        "git commit -m 'fix(harness): git coverage'",
        "git commit --amend --no-edit",
        "git push origin agent/feature-work",
        "git push -u origin HEAD",
        "git push origin main:main",  # explicit refspec, no leading + or :
        "git push https://user:token@github.com/o/r main",  # `:` inside a URL
        "git merge main",
        "git cherry-pick abc1234",
        "git revert abc1234",
        "git switch main",
        "git switch -c agent/new-work",
        "git switch --create agent/new-work",
        "git checkout -b agent/new-work",
        "git checkout main",
        "git checkout v3.2.1",
        "git stash pop",
        "git stash apply stash@{0}",
        "git init",
        "git worktree add ../wt agent/branch",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_git_mutating_wins_over_read_only_in_same_segment():
    # The gh rows' ordering contract, re-asserted for the git rows: a read-only
    # git form in the same chain must never shadow a destructive one.
    assert omg._is_mutating("git status && git reset --hard origin/main")
    assert omg._is_mutating("git stash list && git stash drop stash@{0}")
    assert omg._is_mutating("git branch --list && git branch -D agent/old")
    assert omg._is_mutating("git fetch origin && git push --force origin main")
    assert omg._is_mutating("git log --oneline -5; git clean -fdx")
    assert omg._is_mutating("git rm docs/stale.md && git status")
    # ...and the mirror: a read that merely mentions a destructive word is not.
    assert not omg._is_mutating("git log --oneline | grep 'reset --hard'")
    assert not omg._is_mutating("git branch --list | grep delete")


def test_filesystem_removal_of_source_paths_is_flagged():
    for cmd in (
        "rm services/billing-service/src/api/invoices.py",
        "rm -f docs/specs/SPEC-101.md",
        "rm -rf services/legacy-service",
        "sudo rm -rf /etc/nginx/conf.d/app.conf",
        "mv services/checkout-service/src/models/order.py services/checkout-service/src/models/cart.py",
        "mv README.md docs/",
        "rmdir infrastructure/unused",
        "shred -u infrastructure/k8s/secrets.yaml",
        "rm 'docs/context/records/REC-0428.md'",
        "rm $FILES_TO_DROP",
        # `mv` with one ephemeral operand still moves a real file out of tree
        "mv config/cloudflare.yml /tmp/cloudflare.yml.bak",
    ):
        assert omg._is_mutating(cmd), cmd


def test_filesystem_removal_of_ephemeral_paths_is_not_flagged():
    # Build output, caches, venvs and session scratch are removed constantly;
    # logging them would drown the owed marker in noise nobody reads.
    for cmd in (
        "rm -rf node_modules",
        "rm -rf apps/mobile/build",
        "rm -rf services/x/dist",
        "rm -rf __pycache__ .pytest_cache",
        "rm -rf .venv",
        "rm -f /tmp/probe.json",
        "rm -f ci.log deploy.log",
        "rm -f services/x/app.pyc",
        "rm -rf $TMPDIR/claude",
        "mv build/app.js dist/app.js",
        "rm -rf",  # no operands: destroys nothing
        "rm",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_bulk_deletion_via_find_and_xargs_is_flagged():
    # First-token anchoring cannot see the `rm` in these, so they get patterns.
    for cmd in (
        "find services -name '*.py' -delete",
        "find . -name '*.orig' -exec rm {} ;",
        "find . -type d -execdir rm -rf {} +",
        "find docs -name '*.md' | xargs rm",
        "git ls-files 'docs/*.md' | xargs rm -f",
    ):
        assert omg._is_mutating(cmd), cmd


def test_git_command_strings_quoted_inside_other_commands_are_not_flagged():
    # The git rows are anchored to the segment's command word. Searching docs
    # for a destructive command, or naming one in a commit message, must not
    # owe a security review — the Stop hook blocks turn completion on this
    # marker, so a false positive here costs a whole review ritual, not a line.
    for cmd in (
        "grep -rn 'git push --force' docs/",
        "rg 'git reset --hard' docs/context",
        "git commit -m 'stop using git reset --hard on shared branches'",
        "git commit -m 'never git push --force to main'",
        "git log --grep='git stash drop'",
        "echo 'git clean -fdx' >> docs/runbook.md",
        "cat docs/runbook.md | grep 'git rm'",
        "grep -rn 'find . -delete' scripts/",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_git_mutations_behind_shell_prefixes_are_still_flagged():
    # The anchor skips leading loop/conditional keywords, env assignments and
    # wrapper words, so the command word is still reached in those positions.
    for cmd in (
        "cd /repo && git push --force origin main",
        "for b in $(git branch --list 'claude/*'); do git branch -D $b; done",
        'if [ -n "$X" ]; then git reset --hard origin/main; fi',
        "sudo git clean -fdx",
        "GIT_DIR=/repo/.git git stash drop",
        "time git rebase main",
        "(git checkout -- services/x.py)",
        "git fetch origin\ngit reset --hard origin/main",
        # `bash -c` puts the command word one quoting level in — the regex rows
        # skip it via _SEG_HEAD, the fs tokenizer unwraps it explicitly.
        "bash -c 'git reset --hard origin/main'",
        'sh -c "rm -rf services/legacy"',
        "bash -c 'git clean -fdx'",
    ):
        assert omg._is_mutating(cmd), cmd


def test_separators_inside_quotes_do_not_manufacture_segments():
    # A `;` or `|` inside a quoted argument is data, not a statement separator.
    # A naive split turned prose ABOUT destructive commands into segments that
    # begin with a destructive command word — which is how the anchored rows
    # flagged `ctx decide --decision "...; git rm and git mv; ..."`. Since this
    # repo's commit messages, PR bodies and ledger rationales are exactly such
    # prose, this is the dominant false-positive shape, not a corner case.
    for cmd in (
        'ctx decide --decision "flag stash; git rm and git mv; clean -f"',
        "ctx decide --rationale 'first git reset --hard; then git clean -fdx'",
        'grep -rn "use git switch; never git checkout -- ." docs/',
        "echo 'step 1 | git push --force | step 3'",
    ):
        assert not omg._is_mutating(cmd), cmd
    # The mirror: a command that is itself a mutation stays flagged no matter
    # what its quoted argument says (`gh pr comment` mutates the PR).
    assert omg._is_mutating('gh pr comment 12 --body "never git push --force"')
    # ...but the same separators outside quotes still split, so a real mutation
    # chained after a quoted mention is still caught.
    assert omg._is_mutating("echo 'never git push --force'; git push --force")
    assert omg._is_mutating("echo 'a; b' && rm services/x.py")


def test_split_segments_matches_shell_semantics():
    assert omg._split_segments("a && b") == ["a ", " b"]
    assert omg._split_segments("a; b | c") == ["a", " b ", " c"]
    assert omg._split_segments("a || b") == ["a ", " b"]
    assert omg._split_segments("echo 'a; b'") == ["echo 'a; b'"]
    assert omg._split_segments('echo "a | b"') == ['echo "a | b"']
    assert omg._split_segments('echo "a \\" ; b"') == ['echo "a \\" ; b"']
    # A single `&` is backgrounding, not a separator — unchanged from before.
    assert omg._split_segments("a & b") == ["a & b"]
    # Unbalanced quote degrades to the old, naive behaviour rather than
    # swallowing the rest of the command into one unsplit segment.
    assert omg._split_segments("echo 'oops ; rm x") == ["echo 'oops ", " rm x"]


def test_non_destructive_shell_commands_are_not_flagged():
    # First-token anchoring keeps `rm`-shaped words inside other commands out.
    for cmd in (
        "grep -rn 'rm -rf' scripts/",
        "cat docs/rm-notes.md",
        "ls -la services/",
        "cp docs/a.md docs/b.md",
        "mkdir -p services/new",
        "python -m pytest tools/harness/methodology-harness/tests/ -q",
        "ruff check --no-fix tools/harness",
        "echo 'rm services/x.py'",
        "bash -c 'pytest -q'",
        "python -c \"print('rm -rf /')\"",
        "grep -rn 'git branch -f' docs/",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_fs_operand_helper_ignores_non_destructive_heads():
    # `git rm` must stay on the git patterns (which apply the --dry-run guard),
    # not fall through to the operand classifier.
    assert omg._fs_operands("git rm services/x.py") is None
    assert omg._fs_operands("kubectl delete pod x") is None
    assert omg._fs_operands("rm -rf a b") == ["a", "b"]
    assert omg._fs_operands("sudo rm -- a") == ["a"]
    assert omg._fs_operands("/bin/rm a") == ["a"]
    assert omg._fs_operands("FOO=1 rm a") == ["a"]
    assert omg._fs_operands("") is None
    # `bash -c '<cmd>'` is unwrapped once, and only once (no recursion loop).
    assert omg._fs_operands("bash -c 'rm services/x.py'") == ["services/x.py"]
    assert omg._fs_operands("sh -c 'bash -c \"rm a\"'") is None
    assert omg._fs_operands("bash -c 'pytest -q'") is None
    # An unbalanced quote degrades to a whitespace split rather than raising.
    assert omg._fs_operands("rm 'unterminated services/x.py") == ["'unterminated", "services/x.py"]


# ── kubectl execution / data-movement surface ───────────────────────────────
# A review found MUTATING_OPS anchored on a verb ENUMERATION, and the follow-up
# closed the git half of it without touching the kubectl half. Re-verified
# against merged main: 17 mutating kubectl forms classified read-only,
# including four that each drive arbitrary production database writes with zero
# audit trail. Same both-sides shape as the gh and git matrices.
def test_kubectl_execution_surface_is_flagged():
    # Named by the finding. Each is a full mutation channel that leaves no
    # mutating verb in argv at all — which is exactly why the enumeration
    # missed them: the destructive part happens after the hook has seen the
    # command, over a socket or inside a pod.
    for cmd in (
        # port-forward: a local mongosh/redis-cli speaking straight to prod
        "kubectl port-forward svc/mongodb 27017:27017 -n platform",
        "kubectl -n platform port-forward deploy/redis 6379:6379",
        "kubectl --context prod-cluster port-forward svc/mongodb 27017:27017",
        "kubectl port-forward pod/billing-service-abc 8080:8080 --address 0.0.0.0",
        # cp: plants a file (script, cron entry, seed data) inside a pod
        "kubectl cp ./seed.py platform/billing-service-abc:/tmp/seed.py",
        "kubectl cp platform/mongo-0:/data/dump ./dump",
        # run: arbitrary code in-cluster
        "kubectl run shell --rm -it --image=alpine -- sh",
        'kubectl run c --image=mongo --command -- mongosh "$URI" --eval "db.users.deleteMany({})"',
        # proxy: an unauthenticated local door onto the whole API server
        "kubectl proxy --port=8001",
        "kubectl proxy --address=0.0.0.0 --accept-hosts='.*'",
    ):
        assert omg._is_mutating(cmd), cmd


def test_kubectl_cluster_state_verbs_missed_by_the_enumeration_are_flagged():
    # Not in the finding's list, but the same defect: each sat one alternation
    # entry away from a verb that WAS logged.
    for cmd in (
        "kubectl attach -it billing-service-abc",  # == exec, already logged
        "kubectl debug -it pod/api --image=busybox --target=api",
        "kubectl taint nodes node-1 dedicated=gpu:NoSchedule",
        "kubectl uncordon node-1",  # inverse of the logged `cordon`
        "kubectl expose deployment api --port=80",
        "kubectl autoscale deployment api --min=2 --max=10",
        "kubectl certificate approve csr-abc",  # mints cluster credentials
        "kubectl auth reconcile -f rbac.yaml",  # writes RBAC objects
        "kubectl -n platform auth reconcile -f rbac.yaml",
    ):
        assert omg._is_mutating(cmd), cmd


def test_kubectl_read_only_contract_is_preserved():
    # The forms named in the read-only contract, each asserted UNFLAGGED after
    # the mutating side grew. A regression here is the expensive direction:
    # `kubectl get` during an incident owing a security review is what gets
    # OPS_REVIEW_BYPASS=1 exported, which switches the whole gate off.
    for cmd in (
        "kubectl get pods",
        "kubectl get pods -n platform -o wide",
        "kubectl describe pod billing-service-abc",
        "kubectl logs deploy/api --tail=100",
        "kubectl top nodes",
        "kubectl explain pod.spec.containers",
        "kubectl api-resources",
        "kubectl version --short",
        "kubectl config view",
        "kubectl -n platform config view --minify",
        "kubectl auth can-i create pods",
        "kubectl -n platform auth can-i delete secrets",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_kubectl_new_verbs_inside_resource_names_are_not_flagged():
    # The verb is anchored to the first non-flag token, and `(?![\w-])` closes
    # the flag-value backtrack: _FLAGS may stop after `-n` and offer the
    # NAMESPACE as the verb, so a plain `\b` would match `run` in `run-ns`.
    for cmd in (
        "kubectl -n run-ns get pods",
        "kubectl -n cp-staging get pods",
        "kubectl -n port-forward-test get svc",
        "kubectl logs job/run-migrations -n platform",
        "kubectl describe pod proxy-abc-123",
        "kubectl get svc cp-service",
        "kubectl get pods -l app=debug-tools",
        "kubectl get deploy expose-api",
    ):
        assert not omg._is_mutating(cmd), cmd


def test_kubectl_mutating_wins_over_read_only_for_the_new_verbs():
    # The same ordering rule, re-asserted on the rows added here. Each of these
    # DOES match a READ_ONLY pattern — the read-only kubectl row is an
    # anywhere-match, so `get-data.py` and `logs-api` satisfy it — and must be
    # flagged anyway, because nothing may veto a mutating match.
    for cmd in (
        "kubectl cp ./x.py platform/api-0:/tmp/get-data.py",
        "kubectl port-forward svc/logs-api 3100:3100",
        "kubectl run top-debug --image=alpine --rm -it -- sh",
    ):
        assert any(re.search(p, cmd) for p in omg.READ_ONLY), f"precondition: {cmd} must match READ_ONLY"
        assert omg._is_mutating(cmd), cmd
    # ...and the same across chained segments, in both orders.
    assert omg._is_mutating("kubectl get pods && kubectl port-forward svc/mongodb 27017:27017")
    assert omg._is_mutating("kubectl cp ./x platform/api-0:/tmp/x && kubectl get pods")
    assert omg._is_mutating("kubectl describe pod x; kubectl proxy --port=8001")
    # ...but a read piped into a filter that merely names a new verb must not be.
    assert not omg._is_mutating("kubectl get pods | grep port-forward")
    assert not omg._is_mutating("kubectl get pods -o name | grep proxy")


def test_kubectl_deliberate_boundary_is_pinned():
    # Two recorded judgement calls, pinned so moving either is a
    # deliberate edit rather than a silent drift.
    #
    # (a) NO --dry-run carve-out. `kubectl run --dry-run=client` creates
    # nothing, but `apply --dry-run` has been flagged since the gate's first
    # version and `--dry-run=server` still reaches the prod API server. Consistency plus
    # erring toward flagging beats a spelling-sensitive exemption.
    assert omg._is_mutating("kubectl run nginx --image=nginx --dry-run=client -o yaml")
    assert omg._is_mutating("kubectl apply -f x.yaml --dry-run=server")
    # (b) `kubectl config` set/use-context is NOT flagged: it retargets the
    # local kubeconfig, not cluster state, and the mutation it precedes is
    # itself logged. Flagging it would owe a review for switching contexts.
    assert not omg._is_mutating("kubectl config use-context prod-cluster")
    assert not omg._is_mutating("kubectl config set-context --current --namespace=platform")
    # Genuinely read-only verbs outside the named contract stay unflagged too.
    for cmd in (
        "kubectl diff -f deploy.yaml",
        "kubectl wait --for=condition=ready pod/api --timeout=60s",
        "kubectl cluster-info",
        "kubectl events -n platform",
        "kubectl api-versions",
        "kubectl kustomize infrastructure/k8s/overlays/production",
    ):
        assert not omg._is_mutating(cmd), cmd


# ── marker write: loud on failure, never silent ─────────────────────────────
# The swallowed OSError made the gate fail OPEN *and* silently: no marker, so
# the Stop hook demands no review, and nothing anywhere says the record was
# lost. These tests pin the replacement — retry the transient class, then be
# unmissable — and pin that being loud never becomes blocking.
def _fail_open_n_times(monkeypatch, marker, n, exc=None):
    """Make append-mode opens of `marker` raise `n` times, then work normally."""
    real_open = Path.open
    state = {"n": 0}

    def flaky(self, mode="r", *args, **kwargs):
        if self == marker and "a" in mode:
            state["n"] += 1
            if state["n"] <= n:
                raise exc or PermissionError(13, "used by another process")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)
    monkeypatch.setattr(omg.time, "sleep", lambda _s: None)  # keep the suite fast
    return state


def test_append_marker_succeeds_first_try_without_noise(tmp_path):
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    assert omg._append_marker(marker, "line\n") == []
    assert marker.read_text(encoding="utf-8") == "line\n"


def test_append_marker_retries_transient_contention(tmp_path, monkeypatch):
    # The measured scenario: concurrent appenders on Windows. The record must
    # survive, and the retries must be reported rather than hidden.
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    _fail_open_n_times(monkeypatch, marker, omg._MARKER_WRITE_ATTEMPTS - 1)
    survived = omg._append_marker(marker, "line\n")
    assert len(survived) == omg._MARKER_WRITE_ATTEMPTS - 1
    assert marker.read_text(encoding="utf-8") == "line\n"


def test_append_marker_raises_when_every_attempt_fails(tmp_path, monkeypatch):
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    _fail_open_n_times(monkeypatch, marker, omg._MARKER_WRITE_ATTEMPTS + 5)
    with pytest.raises(omg.MarkerWriteError) as excinfo:
        omg._append_marker(marker, "line\n")
    # Subclasses OSError so the older `except OSError` still catches it...
    assert isinstance(excinfo.value, OSError)
    # ...and carries every attempt, not just the last: four sharing violations
    # is a different diagnosis from one permission error plus three.
    assert len(excinfo.value.attempts) == omg._MARKER_WRITE_ATTEMPTS


def test_append_marker_raises_on_a_real_unwritable_target(tmp_path, monkeypatch):
    # No mocking: a directory sitting where the marker file must go produces a
    # genuine OSError from the real filesystem on both Windows and POSIX.
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    marker.mkdir(parents=True)
    monkeypatch.setattr(omg.time, "sleep", lambda _s: None)
    with pytest.raises(omg.MarkerWriteError):
        omg._append_marker(marker, "line\n")


def test_concurrent_appenders_lose_nothing(tmp_path):
    # THE measured defect, not a hypothetical: `open(..., "a")` is only atomic
    # where the OS implements O_APPEND in the kernel, and the Windows CRT
    # emulates it as seek-to-end-then-write. Without _marker_lock this lands
    # roughly 217 of 480 lines, with torn rows, and raises NOTHING — silent
    # stop-recording by a route no `except` can see. Parallel support-squad
    # agents in one repo are exactly the shape that produces it.
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    marker.parent.mkdir(parents=True)
    threads, writes = 8, 15
    start = threading.Barrier(threads)
    failures: list[str] = []

    def worker(i):
        start.wait()  # release together, to actually contend
        for j in range(writes):
            try:
                omg._append_marker(marker, f"t{i}-w{j}\n")
            except OSError as exc:  # pragma: no cover — a failure IS the finding
                failures.append(f"t{i}-w{j}: {exc}")

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert failures == []
    lines = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]
    expected = {f"t{i}-w{j}" for i in range(threads) for j in range(writes)}
    assert set(lines) == expected  # nothing lost
    assert len(lines) == len(expected)  # and nothing duplicated or torn


def test_marker_lock_is_released_and_leaves_no_file(tmp_path):
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    omg._append_marker(marker, "one\n")
    omg._append_marker(marker, "two\n")
    assert marker.read_text(encoding="utf-8") == "one\ntwo\n"
    # A leaked lock would wedge every later mutating command on the branch.
    assert not marker.with_name(marker.name + ".lock").exists()


def test_marker_lock_timeout_is_loud_not_silent(tmp_path, monkeypatch):
    # A lock held by a live process must not turn into a dropped record. The
    # write fails, and failing is the point — it routes to the loud banner.
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    marker.parent.mkdir(parents=True)
    marker.with_name(marker.name + ".lock").write_text("99999\n", encoding="utf-8")
    monkeypatch.setattr(omg, "_LOCK_TIMEOUT_S", 0.02)
    monkeypatch.setattr(omg.time, "sleep", lambda _s: None)
    with pytest.raises(omg.MarkerWriteError) as excinfo:
        omg._append_marker(marker, "line\n")
    assert "TimeoutError" in " ".join(excinfo.value.attempts)


def test_stale_marker_lock_is_broken_not_wedged(tmp_path, monkeypatch):
    # A hook killed mid-write leaves its lock behind. If that wedged the marker,
    # every later mutation on the branch would go unrecorded — the original
    # failure mode restored by the fix for it.
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    marker.parent.mkdir(parents=True)
    lock = marker.with_name(marker.name + ".lock")
    lock.write_text("99999\n", encoding="utf-8")
    monkeypatch.setattr(omg, "_LOCK_STALE_S", 0.0)  # everything is stale
    assert omg._append_marker(marker, "line\n") == []
    assert marker.read_text(encoding="utf-8") == "line\n"
    assert not lock.exists()


def test_future_dated_lock_mtime_does_not_wedge_the_marker(tmp_path, monkeypatch):
    # Clock skew / VM snapshot restore: a lock stamped in the future must read
    # as stale (abs(age)), not as "0 seconds old, forever".
    marker = tmp_path / ".claude" / "ops-review-owed-b"
    marker.parent.mkdir(parents=True)
    lock = marker.with_name(marker.name + ".lock")
    lock.write_text("99999\n", encoding="utf-8")
    future = time.time() + 86_400
    os.utime(lock, (future, future))
    monkeypatch.setattr(omg, "_LOCK_TIMEOUT_S", 0.05)
    assert omg._append_marker(marker, "line\n") == []
    assert marker.read_text(encoding="utf-8") == "line\n"


def test_main_logs_a_mutation_and_exits_zero(tmp_path, monkeypatch):
    rc = _run_main(monkeypatch, tmp_path, "kubectl port-forward svc/mongodb 27017:27017")
    assert rc == omg.EXIT_OK
    line = (tmp_path / ".claude" / "ops-review-owed-test-branch").read_text(encoding="utf-8")
    stamp, _, cmd = line.rstrip("\n").partition("\t")
    assert cmd == "kubectl port-forward svc/mongodb 27017:27017"
    assert stamp.endswith("Z")


def test_main_marker_write_failure_is_loud(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".claude" / "ops-review-owed-test-branch"
    _fail_open_n_times(monkeypatch, marker, omg._MARKER_WRITE_ATTEMPTS + 5)
    rc = _run_main(monkeypatch, tmp_path, "kubectl delete pod api-0 -n platform")

    # Loud: a distinct non-zero exit code, not the earlier silent 0.
    assert rc == omg.EXIT_MARKER_WRITE_FAILED
    assert rc != omg.EXIT_OK
    # ...and never the one code that would block the command.
    assert rc != omg._EXIT_BLOCK

    err = capsys.readouterr().err
    assert "OPS MARKER WRITE FAILED" in err
    assert "UNAUDITED" in err
    assert "kubectl delete pod api-0 -n platform" in err  # which op was lost
    assert str(marker) in err  # where it should have gone
    assert "PermissionError" in err  # why it failed
    # Nothing was written, so nothing may claim it was.
    assert not marker.is_file()


def test_main_marker_write_failure_survives_a_dead_stderr(tmp_path, monkeypatch):
    # The exit code — not the banner — is the load-bearing channel. With fd 2
    # gone the message is lost, and the failure must STILL be detectable.
    marker = tmp_path / ".claude" / "ops-review-owed-test-branch"
    _fail_open_n_times(monkeypatch, marker, omg._MARKER_WRITE_ATTEMPTS + 5)

    def dead(_text):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(omg.sys.stderr, "write", dead)
    rc = _run_main(monkeypatch, tmp_path, "helm upgrade api ./chart")
    assert rc == omg.EXIT_MARKER_WRITE_FAILED


def test_main_reports_a_recovered_retry_rather_than_hiding_it(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".claude" / "ops-review-owed-test-branch"
    _fail_open_n_times(monkeypatch, marker, 2)
    rc = _run_main(monkeypatch, tmp_path, "kubectl cp ./x.py platform/api-0:/tmp/x.py")
    assert rc == omg.EXIT_OK
    assert marker.read_text(encoding="utf-8").endswith("kubectl cp ./x.py platform/api-0:/tmp/x.py\n")
    assert "recovered after 2 failed attempt" in capsys.readouterr().err


def test_main_never_returns_the_blocking_exit_code(tmp_path, monkeypatch):
    # "Gate the exit, not the emergency" as an executable invariant: whatever
    # happens, this hook must not be the thing that stops a command running.
    marker = tmp_path / ".claude" / "ops-review-owed-test-branch"
    for command, tool, break_write in (
        ("kubectl get pods", "Bash", False),  # read-only
        ("kubectl proxy --port=8001", "Bash", False),  # mutating, logged
        ("kubectl delete pod x", "Bash", True),  # mutating, marker lost
        ("kubectl delete pod x", "Edit", False),  # not a Bash call
        ("", "Bash", False),  # empty command
    ):
        with monkeypatch.context() as mp:
            if break_write:
                _fail_open_n_times(mp, marker, omg._MARKER_WRITE_ATTEMPTS + 5)
            rc = _run_main(mp, tmp_path, command, tool_name=tool)
        assert rc != omg._EXIT_BLOCK, (command, tool, break_write)
        assert rc in (omg.EXIT_OK, omg.EXIT_MARKER_WRITE_FAILED), (command, tool, break_write)


def test_main_writes_no_marker_for_reads_or_non_bash_tools(tmp_path, monkeypatch):
    marker = tmp_path / ".claude" / "ops-review-owed-test-branch"
    assert _run_main(monkeypatch, tmp_path, "kubectl get pods") == omg.EXIT_OK
    assert not marker.exists()
    assert _run_main(monkeypatch, tmp_path, "kubectl delete pod x", tool_name="Edit") == omg.EXIT_OK
    assert not marker.exists()


# ── input contract: stdin JSON first, argv fallback ─────────────────────────
def test_read_input_parses_stdin_json(monkeypatch):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "kubectl apply -f x"}})
    _feed_stdin(monkeypatch, payload)
    tool, cmd = omg._read_input([])
    assert tool == "Bash"
    assert cmd == "kubectl apply -f x"


def test_read_input_argv_fallback_when_stdin_empty(monkeypatch):
    _feed_stdin(monkeypatch, "")
    tool, cmd = omg._read_input(["kubectl", "delete", "pod", "x"])
    assert tool == ""  # no tool_name available via argv
    assert cmd == "kubectl delete pod x"


def test_read_input_handles_malformed_stdin(monkeypatch):
    _feed_stdin(monkeypatch, "not-json{{{")
    # Malformed JSON must not raise; falls back to argv.
    tool, cmd = omg._read_input(["helm", "upgrade", "x"])
    assert cmd == "helm upgrade x"


def test_read_input_string_tool_input(monkeypatch):
    payload = json.dumps({"tool_name": "Bash", "tool_input": "kubectl scale x --replicas=3"})
    _feed_stdin(monkeypatch, payload)
    _, cmd = omg._read_input([])
    assert cmd == "kubectl scale x --replicas=3"


# ── stop gate: the block message counts RECORDS, not lines ──────────────────
# Observed in the field: the gate announced
# "21 mutating ops ran this session" when exactly TWO commands had run — a
# `gh pr comment` whose markdown body was 19 lines, and the `gh api -X PATCH`
# that repaired it. Every body line counted as an op. Earlier in the same
# session it announced "0 mutating ops" and blocked anyway. Both numbers wrong,
# in opposite directions, in the one field a human reads to decide how hard to
# review. The blocking mtime comparison was and stays correct; these tests pin
# the number, and pin that the number can never move the verdict.
_STOP_BRANCH = "test-branch"  # matches the branch _run_main pins for the writer

# A body shaped like the one that produced the 21: markdown, blank lines, a
# table, a checklist. Nothing here is a command; it is all one --body argument.
_PR_COMMENT_BODY = """## Ops review — account isolation

Findings from the multi-account isolation pass:

1. social sign-in credential steal (HIGH)
2. magic-link self-link (HIGH)
3. DELETE /me missing step-up (HIGH)

Providers with no auth listener: 16 (none invalidate on identity change).

| area     | state         |
| -------- | ------------- |
| accounts | 3 HIGH        |
| mobile   | 16 providers  |
| api      | 1 MED         |
| portal   | 0             |

Repro on a clean checkout:

    ctx lint --pr 12

Next up:

- [ ] fix social sign-in
- [ ] fix magic-link
- [ ] fix DELETE /me
- [ ] re-run the isolation sweep
"""
_MULTILINE_COMMAND = f'gh pr comment 12 --body "{_PR_COMMENT_BODY}"'
_SINGLE_LINE_COMMAND = "gh api -X PATCH /repos/o/r/issues/comments/1 -f body=fixed"


def _naive_line_count(text):
    """The pre-fix count: non-empty lines. Kept as the regression's yardstick."""
    return sum(1 for line in text.splitlines() if line.strip())


def _write_owed(tmp_path, commands, branch=_STOP_BRANCH):
    """Build an owed marker through the WRITER's own serializer, so a change to
    the record format breaks this test rather than silently invalidating it."""
    marker = tmp_path / ".claude" / f"ops-review-owed-{branch}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "".join(omg.format_record(f"2026-01-15T12:{i:02d}:00Z", c) for i, c in enumerate(commands)),
        encoding="utf-8",
    )
    return marker


def _run_stop(monkeypatch, tmp_path, capsys):
    """Drive stop_review_gate.main() with the repo root redirected into tmp_path.
    Returns (exit_code, decision_dict_or_None)."""
    monkeypatch.delenv("OPS_REVIEW_BYPASS", raising=False)
    monkeypatch.setattr(srg, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(srg, "_branch", lambda _root: _STOP_BRANCH)
    _feed_stdin(monkeypatch, "{}")
    rc = srg.main()
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out) if out else None)


def test_multi_line_command_body_is_one_op_not_many():
    # THE regression. Two commands, one of them carrying a markdown body.
    text = omg.format_record("2026-01-15T12:00:00Z", _MULTILINE_COMMAND) + omg.format_record(
        "2026-01-15T12:01:00Z", _SINGLE_LINE_COMMAND
    )
    assert omg.count_records(text) == 2
    # And the count the gate used to print, for the size of the lie: the body
    # inflates two ops into twenty-one.
    assert _naive_line_count(text) == 21


def test_block_message_reports_the_command_count(tmp_path, monkeypatch, capsys):
    _write_owed(tmp_path, [_MULTILINE_COMMAND, _SINGLE_LINE_COMMAND])
    rc, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert rc == 0
    assert decision["decision"] == "block"
    assert re.search(r"\b2 mutating ops ran this session\b", decision["reason"]), decision["reason"]
    assert "21 mutating" not in decision["reason"]


def test_block_message_is_singular_for_one_op(tmp_path, monkeypatch, capsys):
    _write_owed(tmp_path, [_MULTILINE_COMMAND])
    _, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert "1 mutating op ran this session" in decision["reason"]
    assert "1 mutating ops" not in decision["reason"]


def test_writer_and_reader_agree_on_the_record_format(tmp_path, monkeypatch, capsys):
    # Anti-drift: no hand-built marker anywhere in this test. The real
    # PreToolUse hook writes the record, the real Stop hook counts it. If the
    # two ever restate the format independently again, this is what fails.
    assert _run_main(monkeypatch, tmp_path, _MULTILINE_COMMAND) == omg.EXIT_OK
    assert _run_main(monkeypatch, tmp_path, _SINGLE_LINE_COMMAND) == omg.EXIT_OK
    marker = tmp_path / ".claude" / f"ops-review-owed-{_STOP_BRANCH}"
    assert _naive_line_count(marker.read_text(encoding="utf-8")) > 2  # body really is multi-line
    _, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert "2 mutating ops ran this session" in decision["reason"]


def test_marker_with_no_complete_records_does_not_claim_zero_ops(tmp_path, monkeypatch, capsys):
    # The second observed case: the marker exists (so the block is correct) but
    # holds nothing countable — a torn write, or a hand-edited file. Announcing
    # "0 mutating ops ran this session" while blocking reads as "nothing
    # happened", which is the opposite of what the gate is asserting.
    marker = tmp_path / ".claude" / f"ops-review-owed-{_STOP_BRANCH}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("gh pr merge 1 --squash\n", encoding="utf-8")  # no timestamp prefix
    _, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert decision["decision"] == "block"  # the block is unchanged
    assert "0 mutating ops" not in decision["reason"]
    assert "Unreviewed ops are recorded in" in decision["reason"]


def test_empty_marker_still_blocks_without_claiming_zero(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".claude" / f"ops-review-owed-{_STOP_BRANCH}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    _, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert decision["decision"] == "block"
    assert "0 mutating ops" not in decision["reason"]


def test_unreadable_marker_degrades_the_count_but_not_the_block(tmp_path, monkeypatch, capsys):
    _write_owed(tmp_path, [_SINGLE_LINE_COMMAND])
    real_read = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name.startswith("ops-review-owed-"):
            raise PermissionError(13, "used by another process")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    _, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert decision["decision"] == "block"
    assert "count unavailable" in decision["reason"]


def test_count_never_changes_the_verdict(tmp_path, monkeypatch, capsys):
    # The mtime comparison is the enforcement; the count is a message. A marker
    # full of records is still cleared by a newer review marker...
    owed = _write_owed(tmp_path, [_MULTILINE_COMMAND, _SINGLE_LINE_COMMAND])
    reviewed = tmp_path / ".claude" / f"ops-reviewed-{_STOP_BRANCH}"
    reviewed.write_text("", encoding="utf-8")
    os.utime(reviewed, (owed.stat().st_atime + 10, owed.stat().st_mtime + 10))
    rc, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert rc == 0 and decision is None  # allowed: nothing printed
    # ...and an absent marker never blocks regardless of any count logic.
    owed.unlink()
    reviewed.unlink()
    rc, decision = _run_stop(monkeypatch, tmp_path, capsys)
    assert rc == 0 and decision is None


def test_count_records_ignores_timestamp_lookalikes():
    # The prefix is the writer's exact stamp + TAB. A body line that merely
    # starts with a date, or with the stamp followed by a space, is body text.
    text = omg.format_record(
        "2026-01-15T12:00:00Z",
        'gh pr comment 1 --body "2026-01-15 release notes\n2026-01-15T12:00:00Z not a record\nline three"',
    )
    assert omg.count_records(text) == 1


def test_marker_record_re_matches_what_the_writer_stamps():
    # Pins the timestamp FORMAT against the pattern that parses it, so changing
    # one without the other fails here rather than in a human-facing number.
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime(omg.MARKER_TIMESTAMP_FORMAT)
    assert omg.MARKER_RECORD_RE.match(omg.format_record(stamp, "kubectl delete pod x"))


# ── security-review record check ────────────────────────────────────────────
def _write_record(tmp_path, decisions):
    import yaml

    fm = {
        "ctx_id": "REC-9999",
        "pr_number": 9999,
        "title": "t",
        "status": "open",
        "agent_decisions": decisions,
    }
    body = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n## Intent\nx\n"
    p = tmp_path / "REC-9999.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_record_with_security_decision_passes(tmp_path):
    import check_security_review as csr

    rec = _write_record(
        tmp_path,
        [{"decision_id": "D1", "agent": "security", "decision": "d", "rationale": "r"}],
    )
    assert csr._record_has_security_decision(rec) is True


def test_record_without_security_decision_fails(tmp_path):
    import check_security_review as csr

    rec = _write_record(
        tmp_path,
        [{"decision_id": "D1", "agent": "sre-devops", "decision": "d", "rationale": "r"}],
    )
    assert csr._record_has_security_decision(rec) is False


def test_missing_record_fails(tmp_path):
    import check_security_review as csr

    assert csr._record_has_security_decision(tmp_path / "nope.md") is False


def test_sensitive_paths_are_flagged():
    import check_security_review as csr

    for f in [
        "infrastructure/k8s/monitoring/alloy-config.yaml",
        ".github/workflows/ci.yml",
        "services/billing-service/src/core/auth_service.py",
        "services/x/api/device_auth.py",
        "shared/secrets_manager/aws.py",
        "infrastructure/terraform/do/main.tf",
        "services/x/nginx.conf",
        "k8s/cert-manager-issuer.yaml",
        "config/cloudflare.yml",
        r"infrastructure\k8s\overlays\production\secrets-template.yaml",  # backslash path
    ]:
        assert csr._is_sensitive_path(f), f


def test_non_sensitive_paths_pass():
    import check_security_review as csr

    for f in [
        "docs/context/records/REC-0185.md",
        "README.md",
        "apps/mobile/lib/features/home/home_screen.dart",
        "services/checkout-service/src/models/order.py",
        ".gitattributes",
        "docs/specs/SPEC-101.md",
    ]:
        assert not csr._is_sensitive_path(f), f


# ── helpers ─────────────────────────────────────────────────────────────────
def _feed_stdin(monkeypatch, text):
    import io

    fake = io.StringIO(text)
    fake.isatty = lambda: False  # type: ignore[assignment]
    monkeypatch.setattr(sys, "stdin", fake)


def _run_main(monkeypatch, tmp_path, command, tool_name="Bash"):
    """Drive ops_marker_gate.main() end-to-end over the real stdin-JSON path,
    with the repo root redirected into tmp_path so the marker lands there."""
    _feed_stdin(monkeypatch, json.dumps({"tool_name": tool_name, "tool_input": {"command": command}}))
    monkeypatch.setattr(omg, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(omg, "_branch", lambda _root: "test-branch")
    monkeypatch.setattr(omg.time, "sleep", lambda _s: None)
    return omg.main([])
