#!/usr/bin/env python3
"""Ops-mutation marker gate — Claude Code ``PreToolUse`` hook for ``Bash``.

DESIGN PRINCIPLE — "gate the exit, not the emergency". This hook NEVER blocks a
mutating command. During an incident an agent must be free to run
``kubectl rollout undo`` the instant it's needed; obstructing that would be
actively harmful. Instead, a mutating op drops a *"review owed"* marker on the
branch, and the companion Stop hook (:mod:`stop_review_gate`) blocks the *turn*
from finishing until a security/testing review has been recorded. That precisely
targets the failure mode we care about: declaring ops work "done" without an
independent review — not the running of the command itself.

Behaviour
---------
- Read the tool call from **stdin JSON** first (current Claude Code contract:
  ``{"tool_name":"Bash","tool_input":{"command":"..."}}``), falling back to a
  join of ``argv`` for back-compat with the older argv-based hook wiring.
- Only ``Bash`` tool calls are considered; anything else exits 0 silently.
- If any chained segment of the command matches :data:`MUTATING_OPS` — or is a
  destructive filesystem command per :func:`_is_destructive_fs` — append
  ``<timestamp>\t<command>`` to ``.claude/ops-review-owed-<branch>`` and print
  a one-line notice to stderr. Mutating wins over :data:`READ_ONLY` when both
  match a segment.
- Scope covers live infra/CI/secret/remote state (kubectl, doctl, helm, gh,
  infisical) **and** local repo/worktree state: git history rewrites, working-
  tree discards, ref/remote deletions, and ``rm``/``mv`` of non-ephemeral paths.
- **Never blocks.** A parse error, a non-Bash tool, a read-only command, or a
  successfully-logged mutation all exit 0, and exit code 2 — the only code the
  ``PreToolUse`` contract treats as "block this tool call" — is never returned
  on any path.

Exit codes
----------
``0`` on every path where the audit trail is intact. ``1`` (see
:data:`EXIT_MARKER_WRITE_FAILED`) when a mutation was classified but its marker
line could **not** be written after :data:`_MARKER_WRITE_ATTEMPTS` tries.

That second code is deliberate and is the one thing this hook is loud about.
``PreToolUse`` treats a non-zero, non-2 exit as a *non-blocking* error and
surfaces its stderr, so being loud here costs nothing the design principle
above protects: the command still runs. Swallowing the error instead — as this
hook originally did — fails OPEN and silently, because the Stop gate keys off
the marker's existence: no marker written, no review demanded, mutation gone
unrecorded, and nothing anywhere says so. Windows concurrent appenders make
that reachable in practice (the same contention that forced an append lock on
the ledger writer), which is why the write retries the transient class before
giving up. The exit code, not the stderr banner, is the load-bearing channel —
it survives a closed fd 2, which the banner does not.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ── exit codes ──────────────────────────────────────────────────────────────
# The PreToolUse contract: 2 = block the tool call, any other non-zero = a
# NON-BLOCKING error whose stderr is surfaced. This hook must never return 2 —
# _EXIT_BLOCK is defined only so the invariant has a name to be tested against.
EXIT_OK = 0
EXIT_MARKER_WRITE_FAILED = 1
_EXIT_BLOCK = 2  # reserved by the contract; never returned by this hook

# Marker-write retry policy. The failure this targets is transient contention —
# a concurrent appender or an AV/indexer holding the handle on Windows — which
# clears in milliseconds. A permanent failure (read-only mount, disk full) burns
# all four attempts in ~0.35 s and then reports loudly; that only ever happens
# on a mutating command, so it is not a per-Bash-call cost.
_MARKER_WRITE_ATTEMPTS = 4
_MARKER_RETRY_BASE_S = 0.05

# Marker-append lock. Short by design: the critical section is one buffered
# write of one line, so a wait beyond a few seconds means a wedged holder, not
# a busy one — and a PreToolUse hook that waits is a stalled session. Timing out
# raises into the retry loop above and ends at the loud banner, never at a
# silent skip. _LOCK_STALE_S is likewise short (the ledger writer's 120 s
# covers a whole read-modify-write; this covers microseconds), so a hook killed
# mid-write cannot make every later mutation fail loudly for two minutes.
_LOCK_TIMEOUT_S = 5.0
_LOCK_STALE_S = 30.0


# ── marker record format (THE shared definition; readers import it) ─────────
# One record per mutating command: an ISO8601 UTC timestamp, a TAB, then the
# command VERBATIM — newlines and all. So a record is NOT a line: a `gh pr
# comment --body "<19 lines of markdown>"` occupies twenty physical lines and
# is still one op.
#
# This lives here, in the writer, because the reader that got it wrong was
# reading a format it had assumed rather than one it was told. stop_review_gate
# counted non-empty lines and told a human "21 mutating ops ran this session"
# for two commands; across this repo's 26 branch markers that miscount runs up
# to 12x (main: 2876 lines, 230 records). That number is what someone reads to
# size up a turn's blast radius before deciding how hard to review, so being an
# order of magnitude out is not cosmetic — it is a lie in the one direction
# that gets a review skipped ("nothing much happened") or drowned.
#
# Residual, accepted: a command body whose own line begins with an ISO8601
# stamp + TAB would over-count by one. That means pasting a marker record into
# a commit message; the alternative — a delimiter the writer escapes into the
# command text — would corrupt the audit trail's verbatim copy of what ran.
MARKER_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MARKER_RECORD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\t")


def format_record(stamp: str, command: str) -> str:
    """Serialize one marker record. The single place the wire format is built."""
    return f"{stamp}\t{command}\n"


def count_records(text: str) -> int:
    """Number of logged mutations in marker `text` — records, never lines."""
    return sum(1 for line in text.splitlines() if MARKER_RECORD_RE.match(line))


# ── editable policy constants ───────────────────────────────────────────────
# _FLAGS: zero or more flag tokens, each optionally followed by a separate
# value token (`-R o/r`, `--repo=o/r`, `-n my-ns`). cobra-based CLIs
# resolve the subcommand and verb *past* interspersed flags (`gh -R o/r pr
# merge`, `gh pr --repo o/r merge`, `kubectl --context prod delete pod x` are
# all valid), so every anchored pattern must skip flags in each gap. The value
# token may not start with `-` — that keeps flag-vs-value parsing unambiguous
# so the repeated group cannot backtrack exponentially.
_FLAGS = r"(?:--?\S+(?:[= ][^-\s]\S*)?\s+)*"
_GH = r"\bgh\s+" + _FLAGS
# Segment-head anchor. Unlike the kubectl/gh/helm rows — which match anywhere
# in a segment — the git and bulk-deletion rows are anchored to the segment's
# COMMAND WORD, because git command strings appear inside quoted arguments far
# more often than any other CLI's: `grep -rn 'git push --force' docs/`,
# `git commit -m "stop using git reset --hard"`. An anywhere-match would owe a
# security review for a text search, and this marker blocks turn completion —
# so that noise is not free, it is exactly the "log nobody reads" failure mode.
# Segments are split on &&/||/;/|/newline and stripped before matching, so ^ is
# the command word once leading shell keywords (loop and conditional bodies),
# env assignments, and wrapper words are skipped.
# Known gap accepted with this trade: remote execution (`ssh host git reset
# --hard`) escapes the git rows. It is rare for a local-repo tool, whereas
# quoting git commands in text is routine; the kubectl/gh rows, where the
# balance runs the other way, stay unanchored.
_SEG_HEAD = (
    r"^(?:[({!]\s*)*"
    r"(?:(?:do|then|else|elif|sudo|doas|command|builtin|exec|env|time|nohup|nice)\s+"
    r"|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    # `bash -c '<cmd>'` puts the real command word one quote further in. The
    # optional quote is safe: it can only match where a command word would
    # start, so it cannot re-admit a git string quoted as someone else's
    # argument (`grep 'git push --force'` still begins with `grep`).
    r"(?:(?:bash|sh|zsh|dash)\s+-c\s+)?['\"]?"
)
# git takes global options *before* the subcommand (`git -C /repo push`,
# `git --no-pager log`), so it needs the same flag-skipping gap as gh/kubectl.
_GIT = _SEG_HEAD + r"git\s+" + _FLAGS

# gh api graphql: `-f query=...` also carries read-only queries, so it is only
# mutating when the document contains a mutation. Matched against the WHOLE
# command as well as per segment (see _is_mutating) because real mutation
# documents are usually pretty-printed across lines, which the newline segment
# split would otherwise scatter.
_GH_GRAPHQL_MUTATION = _GH + r"api\s+graphql\b[\s\S]*\bmutation\b"

# MUTATING_OPS: commands that change live infra/CI/secret/remote-repo state. A
# match in any chained segment owes a review, and it wins over READ_ONLY when
# both match the same segment — so keep these patterns precise: a read-only
# form must never match here, because nothing can veto a match. Regexes are
# matched case-sensitively (except where scoped otherwise) against the raw
# command string with re.search.
MUTATING_OPS: tuple[str, ...] = (
    # kubectl: the verb is always the first non-flag token, so anchor it there
    # — an anywhere-match would flag resource names like job/migrate-create-x.
    #
    # SCOPE RULE for this row (a recorded ledger decision): log every verb that
    # can EXECUTE CODE inside the cluster, MOVE DATA across the cluster
    # boundary, or CHANGE CLUSTER STATE — even when the typical invocation is a
    # read. The verb, not the intent, is what a regex can see, and `exec` has
    # sat on this side since the first version of this gate on exactly that
    # basis: most `kubectl exec` calls are `-- cat` or `-- mongosh --eval
    # "db.x.find()"`, and it is logged anyway because the same spelling grants
    # a shell. port-forward / cp / run / proxy / attach / debug
    # are that same capability under other names, so they take the same posture.
    #   * port-forward — a local client speaking straight to prod Mongo/Redis;
    #     a full read/write session, and the writes never appear in any argv.
    #   * cp           — plants a file (a script, a cron entry) inside a pod.
    #   * run          — starts an arbitrary pod, i.e. arbitrary code in-cluster.
    #   * proxy        — an unauthenticated local door onto the whole API server;
    #     every verb above is then reachable by curl, invisible to this hook.
    # Also folded in here are the cluster-state verbs the original enumeration
    # simply missed alongside its already-logged neighbours: uncordon (inverse of
    # the logged cordon), taint, expose, autoscale, certificate (approve/deny
    # mints cluster credentials), attach and debug.
    #
    # `(?![\w-])` rather than `\b` closes a flag-value backtrack: _FLAGS may stop
    # after `-n`, offering the NAMESPACE as the verb, and `\b` would then match
    # `run` inside `kubectl -n run-ns get pods`.
    r"\bkubectl\s+" + _FLAGS + r"(patch|apply|delete|set|scale|rollout|replace|edit|create|annotate|label"
    r"|drain|cordon|uncordon|taint|exec|attach|debug|port-forward|cp|run|proxy"
    r"|expose|autoscale|certificate)(?![\w-])",
    # `kubectl auth` splits by subcommand: can-i/whoami read, `reconcile` writes
    # RBAC objects to the cluster. Its own row so `auth can-i` — named in the
    # read-only contract — stays off the verb alternation entirely.
    r"\bkubectl\s+" + _FLAGS + r"auth\s+" + _FLAGS + r"reconcile\b",
    # doctl resource mutations (verb position varies; anywhere-match)
    r"\bdoctl\b.*\b(create|delete|update|restart|patch)\b",
    # helm release lifecycle
    r"\bhelm\b.*\b(install|upgrade|uninstall|rollback)\b",
    # gh PR / issue lifecycle mutations (`new` is a real alias of `create`;
    # `issue develop` creates a linked remote branch)
    _GH + r"pr\s+" + _FLAGS + r"(merge|close|edit|ready|create|new|review|reopen|comment|lock|unlock|update-branch)\b",
    _GH
    + r"issue\s+"
    + _FLAGS
    + r"(close|edit|create|new|comment|reopen|delete|develop|transfer|pin|unpin|lock|unlock)\b",
    # gh release: default-flag every subcommand that is not explicitly
    # read-only, so future mutating subcommands are caught without an update
    _GH + r"release\s+" + _FLAGS + r"(?!(?:view|list|download|help)\b)[A-Za-z]",
    # GitHub Actions / repo-level state via gh
    _GH + r"workflow\s+" + _FLAGS + r"(run|enable|disable)\b",
    _GH + r"run\s+" + _FLAGS + r"(cancel|rerun|delete)\b",
    _GH + r"(secret|variable)\s+" + _FLAGS + r"(set|delete)\b",
    _GH + r"cache\s+" + _FLAGS + r"delete\b",
    _GH + r"repo\s+" + _FLAGS + r"(create|delete|edit|rename|archive|unarchive|sync)\b",
    # gh api: explicit mutating method in any accepted spelling (`-X DELETE`,
    # `-XDELETE`, `--method=delete` — gh upcases the value itself); or fields
    # without any method flag (gh then defaults the request to POST); or a
    # GraphQL mutation document
    _GH + r"api\b.*(?:-X|--method)[=\s]*(?i:post|put|patch|delete)\b",
    _GH + r"api\b(?!\s+graphql\b)(?!.*(?:-X|--method))(?=.*\s(?:-f|--field|-F|--raw-field|--input)\b)",
    _GH_GRAPHQL_MUTATION,
    # Infisical secret mutations
    r"\binfisical\b.*\b(set|delete)\b",
    # ── git: history rewrites, working-tree discards, ref/remote deletions ──
    # SCOPE RULE for this group: log what DESTROYS or REWRITES existing state,
    # not what adds to it. Deliberately NOT logged (a recorded ledger decision,
    # not an oversight):
    #   * `git add` / `git commit` (incl. --amend) — additive and reflog-
    #     recoverable, and they fire on essentially every turn; the change they
    #     produce is already gated downstream by `gh pr create` and `gh pr
    #     merge`, both of which ARE logged. Amending a *pushed* commit needs a
    #     force-push, which is logged below.
    #   * plain `git push` — a fast-forward to a feature branch loses no remote
    #     history; the destructive spellings (--force*, --delete, :ref,
    #     --mirror, --prune) are enumerated below.
    #   * `git merge` / `git cherry-pick` / `git revert` / `git switch` /
    #     `git checkout -b` — additive or pure navigation.
    # Rationale for drawing the line here rather than logging everything: this
    # marker makes the Stop hook demand a security/testing review before the
    # turn can finish. A marker that fires on every turn is not a signal — it
    # trains agents to export OPS_REVIEW_BYPASS=1, which disables the whole
    # gate rather than just this row. Flip any of the above onto the logged
    # side by moving it into this tuple; the tests pin the current split.
    # `\s\+ref` is the refspec force spelling (`git push origin +main`) and
    # `\s:ref` the refspec delete spelling (`git push origin :stale`); both
    # reach the wire as the flag forms do.
    _GIT + r"push\b.*(?:--force\b|\s-f\b|\s--delete\b|\s-d\b|\s--mirror\b|\s--prune\b"
    r"|\s:[^\s:]+|\s\+[A-Za-z0-9_/.^~-]+)",
    # reset: --hard/--merge/--keep discard working-tree state; --soft/--mixed
    # (the default) only move refs/index and are recoverable, so stay unlogged
    _GIT + r"reset\b.*--(?:hard|merge|keep)\b",
    # checkout/switch/restore forms that overwrite working-tree files: the
    # pathspec separator (`git checkout -- <path>`), a bare `.` pathspec,
    # -f/--force, interactive discard (-p), conflict-side discard
    # (--ours/--theirs), and -B (force-resets an existing branch ref).
    # Plain `git checkout <branch>` / `-b` / `git switch <branch>` are pure
    # navigation and stay unlogged.
    _GIT + r"checkout\b.*(?:\s--(?:\s|$)|\s-[fpB]\b|\s--(?:force|patch|ours|theirs)\b|\s\.(?:\s|$))",
    _GIT + r"switch\b.*(?:\s-f\b|\s--(?:force|discard-changes)\b)",
    _GIT + r"restore\b",  # every form writes the worktree and/or the index
    # stash: default-flag every subcommand *except* the reads and the restoring
    # ones, so bare `git stash` (an alias of push) and any future subcommand
    # land on the logged side. Hiding uncommitted work is precisely the
    # observed failure mode this row exists for.
    _GIT + r"stash\b(?!\s+(?:list|show|pop|apply|branch)\b)",
    # tracked-file removal/rename, and untracked-file deletion (git clean does
    # nothing without -f, and -n/--dry-run only lists — neither owes a review)
    _GIT + r"(?:rm|mv)\b(?!.*(?:\s-[A-Za-z]*n\b|\s--dry-run\b))",
    _GIT + r"clean\b(?!.*(?:\s-[A-Za-z]*n\b|\s--dry-run\b)).*(?:\s-[A-Za-z]*f|\s--force\b)",
    # ref deletion / rename / force-move: any of these can orphan commits
    _GIT + r"branch\b.*(?:\s-[dDmMf]\b|\s--(?:delete|move|force)\b)",
    _GIT + r"tag\b.*(?:\s-[dfF]\b|\s--(?:delete|force)\b)",
    _GIT + r"update-ref\b",
    # history rewrites (`--abort`/`--continue` are included: they rewrite the
    # worktree too, and the cost of the extra marker line is one appended row,
    # not one extra review)
    _GIT + r"(?:rebase|filter-branch|filter-repo)\b",
    _GIT + r"pull\b.*--rebase\b",
    # destroying the recovery net that makes reset/rebase survivable
    _GIT + r"reflog\s+(?:expire|delete)\b",
    _GIT + r"gc\b.*--prune\b",
    _GIT + r"worktree\s+(?:remove|prune)\b",
    # ── bulk filesystem deletion ────────────────────────────────────────────
    # Plain `rm`/`mv` is classified by operand in _is_destructive_fs (a regex
    # cannot tell `rm -rf node_modules` from `rm services/x/api/foo.py`). These
    # two spellings hide the destructive command word from that classifier's
    # first-token anchor, so they get explicit patterns.
    _SEG_HEAD + r"find\b.*\s(?:-delete\b|-exec(?:dir)?\s+(?:rm|rmdir|mv|shred)\b)",
    _SEG_HEAD + r"xargs\b.*\s(?:rm|rmdir|mv|shred)\b",
)

# ── destructive filesystem commands (operand-aware) ─────────────────────────
# `rm`/`mv` cannot be classified by a MUTATING_OPS regex alone: `rm -rf
# node_modules` is routine build hygiene while `rm services/x/api/foo.py` is
# exactly the unlogged tracked-file deletion this gate exists to surface, and
# the two differ only in their OPERANDS. So this one classifier tokenizes the
# segment and inspects them. It stays pure — no filesystem stat, no `git
# ls-files` — because it runs inside a PreToolUse hook on EVERY Bash call, and
# because purity is what keeps it unit-testable alongside the regexes.
_FS_DESTRUCTIVE = frozenset({"rm", "rmdir", "mv", "shred"})

# Words that wrap the real command word at the head of a segment.
_CMD_WRAPPERS = frozenset({"sudo", "doas", "command", "builtin", "exec", "env", "time", "nice", "nohup"})

# `bash -c '<cmd>'` hides the command word one quoting level in. The regex rows
# handle this via _SEG_HEAD; the tokenizer has to unwrap it explicitly.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})

# Leading `VAR=value` assignments precede the command word too.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Operands that are ephemeral by construction: build output, caches, virtual
# envs, temp dirs, session scratch. Necessarily incomplete — but incompleteness
# here costs one extra marker (noise), never an escape, so it errs the safe way.
_EPHEMERAL_OPERAND = re.compile(
    r"(?:^|/)(?:tmp|temp|scratchpad|node_modules|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache"
    r"|\.venv|venv|dist|build|target|coverage|htmlcov|\.next|\.gradle|\.dart_tool|\.terraform)(?:/|$)"
    r"|\.(?:log|tmp|temp|pyc|pyo|swp|orig|rej|bak|class|o)$"
    r"|^\$\{?(?:TMP|TEMP|TMPDIR)\b",
    re.IGNORECASE,
)


def _fs_operands(segment: str, _depth: int = 0) -> list[str] | None:
    """Operands of a destructive filesystem command, or ``None`` if this
    segment does not *start* with one. Flag tokens (including the ``--``
    separator) are dropped. First-token anchoring is what keeps `git rm` on the
    git patterns above and stops `grep rm` from matching."""
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:  # unbalanced quote — degrade to a whitespace split
        tokens = segment.split()
    i = 0
    while i < len(tokens) and (tokens[i] in _CMD_WRAPPERS or _ENV_ASSIGN.match(tokens[i])):
        i += 1
    if i >= len(tokens):
        return None
    head = tokens[i].lstrip("\\").rsplit("/", 1)[-1]  # `\rm`, `/bin/rm`
    if head in _SHELLS and _depth == 0 and "-c" in tokens[i + 1 :]:
        inner = next((t for t in tokens[i + 1 :] if not t.startswith("-")), None)
        return _fs_operands(inner, _depth + 1) if inner else None
    if head not in _FS_DESTRUCTIVE:
        return None
    return [t for t in tokens[i + 1 :] if not t.startswith("-")]


def _is_destructive_fs(segment: str) -> bool:
    """True when the segment removes/renames at least one non-ephemeral path.
    A command with no operands at all (``rm -rf`` alone) is not flagged: it
    destroys nothing, and the shell would reject it anyway."""
    operands = _fs_operands(segment)
    if not operands:
        return False
    return any(not _EPHEMERAL_OPERAND.search(op.replace("\\", "/")) for op in operands)


# READ_ONLY: known-safe command forms. NOTE: this tuple cannot change any
# verdict — mutating is checked first, and a segment matching neither list is
# also left unflagged — so it is retained purely as the policy record of the
# forms asserted safe, pinned by the read-only corpus in the test suite. Keep
# gh entries subcommand-precise anyway (a bare `gh pr` entry once classified
# `gh pr merge` as read-only, back when this list could veto).
READ_ONLY: tuple[str, ...] = (
    r"\bkubectl\b.*\b(get|logs|describe|top|version|api-resources|explain)\b",
    # Both are subcommand-scoped because their parent verb is split: `kubectl
    # config` also sets contexts and credentials, `kubectl auth` also runs
    # `reconcile` (which is on the mutating side above).
    r"\bkubectl\s+" + _FLAGS + r"(?:config\s+view|auth\s+can-i)\b",
    r"\bdoctl\b.*\b(list|get)\b",
    r"\bgh\s+pr\s+(view|list|checks|diff|status)\b",
    r"\bgh\s+run\s+(view|list|watch|download)\b",
    r"\bgh\s+issue\s+(view|list|status)\b",
    r"\bgh\s+release\s+(view|list|download)\b",
    r"\bgh\s+(secret|variable|cache|repo|label)\s+(list|view)\b",
    r"\bgh\s+api\b",  # GETs — mutating gh api forms are matched first above
    # git inspection: reads the object DB / refs / worktree, writes nothing to
    # any of them. `fetch` is here because it only advances remote-tracking
    # refs — it never touches a local branch, the worktree, or the remote.
    r"\bgit\s+(status|log|diff|show|blame|describe|shortlog|whatchanged|grep"
    r"|rev-parse|rev-list|ls-files|ls-remote|ls-tree|cat-file|count-objects|fetch)\b",
    r"\bgit\s+branch\s*$",
    r"\bgit\s+branch\s+(--list|-l|-a|-r|-v|-vv|--all|--remotes|--verbose|--show-current|--contains|--merged|--no-merged)\b",
    r"\bgit\s+(stash|worktree|remote|tag|submodule|reflog|config)\s+(list|show|view|--list|-l)\b",
    r"\bgit\s+(remote|config)\s+(-v|--get|--get-all)\b",
    r"\bgit\s+(clean|rm)\s+(-n|--dry-run)\b",
)


def _branch(repo_root: Path) -> str:
    """Current branch, path-sanitized. Copied from predev_gate._branch so the
    marker naming matches the rest of the harness exactly."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = out or "detached"
    except OSError:
        branch = "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch)


def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            return Path(out)
    except OSError:
        pass
    return Path.cwd()


def _read_input(argv: list[str]) -> tuple[str, str]:
    """Return (tool_name, command). Prefer stdin JSON (current CC contract),
    fall back to an argv join (older argv-based wiring). Never raises."""
    raw = ""
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError):
            raw = ""
    raw = raw.strip()
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            tool_name = str(data.get("tool_name", "") or "")
            tool_input = data.get("tool_input") or {}
            command = ""
            if isinstance(tool_input, dict):
                command = str(tool_input.get("command", "") or "")
            elif isinstance(tool_input, str):
                command = tool_input
            return tool_name, command
    # argv fallback: no tool_name available, treat the joined args as a command.
    return "", " ".join(argv).strip()


# Shell separators between chained statements. We classify each segment
# independently so a mutation chained after a read (`kubectl get x && kubectl
# delete y`) is still caught, while a read that merely mentions a mutating
# keyword downstream (`kubectl get pods | grep delete`) is not.
#
# The split is QUOTE-AWARE: a `;` or `|` inside a quoted argument is data, not
# a statement separator. This is plain shell semantics, and it is load-bearing
# for the _SEG_HEAD-anchored rows above — their premise is that `^` in a
# segment is a command word, which a naive split falsifies the moment a quoted
# argument contains a separator. The commands this repo writes most often are
# prose ABOUT destructive commands (commit messages, PR bodies, `ctx decide`
# rationales), and a naive split turned `"...; git rm and git mv; ..."` inside
# one quoted string into a segment that begins with a destructive command word.
#
# Degradation is toward the old behaviour: an unbalanced quote fails the
# quoted-span alternatives, falls through to the single-char branch, and the
# remaining separators split exactly as before.
_SEG_TOKENS = re.compile(
    r"""'[^']*'             # single-quoted span (POSIX: no escapes inside)
      | "(?:[^"\\]|\\.)*"   # double-quoted span, backslash escapes honoured
      | \\.                 # escaped character outside quotes
      | &&|\|\||[;|\n]      # statement separators
      | [^'"\\&;|\n]+       # ordinary run
      | .                   # leftover: unbalanced quote, bare &
    """,
    re.VERBOSE | re.DOTALL,
)
_SEPARATORS = frozenset({"&&", "||", ";", "|", "\n"})


def _split_segments(command: str) -> list[str]:
    """Split `command` into chained statements on unquoted separators."""
    segments: list[str] = []
    buf: list[str] = []
    for tok in _SEG_TOKENS.findall(command):
        if tok in _SEPARATORS:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(tok)
    segments.append("".join(buf))
    return segments


def _is_mutating(command: str) -> bool:
    if not command.strip():
        return False
    for segment in _split_segments(command):
        seg = segment.strip()
        if not seg:
            continue
        # Mutating wins *within a segment*: an explicit mutation owes a review
        # even when the same clause also matches a read-only form (e.g.
        # `kubectl delete pod $(kubectl get pods -o name)`, `git stash list &&
        # git stash drop`). READ_ONLY only documents the known-safe forms — a
        # clause matching neither list is likewise left unflagged. The
        # filesystem classifier sits on this same mutating-first side.
        if any(re.search(pat, seg) for pat in MUTATING_OPS) or _is_destructive_fs(seg):
            return True
        if any(re.search(pat, seg) for pat in READ_ONLY):
            continue
    # GraphQL mutation documents usually span lines, which the segment split
    # scatters — so this one pattern also gets a whole-command pass. No other
    # pattern may: doctl/helm anywhere-matches across a whole command would
    # re-introduce the `... | grep delete` false positive class.
    return re.search(_GH_GRAPHQL_MUTATION, command) is not None


class MarkerWriteError(OSError):
    """Every attempt to append the marker line failed.

    Subclasses ``OSError`` so a caller that only knows the old contract still
    catches it, and carries :attr:`attempts` so the banner can name each failure
    instead of just the last one — a permission error on attempt 1 followed by
    three sharing violations is a different diagnosis from four of the same.
    """

    def __init__(self, marker: Path, attempts: list[str]) -> None:
        super().__init__(f"could not append to {marker} after {len(attempts)} attempts")
        self.marker = marker
        self.attempts = attempts


@contextmanager
def _marker_lock(marker: Path, timeout: float | None = None):
    """Serialise marker appends across processes.

    Not belt-and-braces — measured. ``open(..., "a")`` is only atomic where the
    OS implements ``O_APPEND`` in the kernel; the Windows CRT emulates it as
    seek-to-end-then-write, which is a race. Twelve concurrent hook processes
    appending 20 lines each landed **113 of 240** on this dev box, with one torn
    line and — the part that matters — **zero exceptions raised**. That is the
    same silent stop-recording the loud-failure work is about, arriving by a
    route no ``except`` can see, and parallel support-squad agents in one repo
    are exactly the shape that produces it.

    ``O_CREAT | O_EXCL`` is the portable atomic test-and-set, and the stale-lock
    steal uses an atomic rename rather than an unlink for the ledger lock's
    reason: two
    waiters seeing one stale lock would both unlink, and the second would remove
    the *fresh* lock the first had just taken. ``abs(age)`` so a future-dated
    mtime reads as stale instead of wedging the marker until the clock catches
    up. A timeout raises ``TimeoutError`` (an ``OSError``) into the retry loop,
    so lock trouble ends loudly, never as a skipped record.
    """
    # Resolved at call time, not bound as a default, so the timings stay
    # patchable from the tests without a 5 s wait per contention case.
    timeout = _LOCK_TIMEOUT_S if timeout is None else timeout
    lock = marker.with_name(marker.name + ".lock")
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # holder released it between the open and the stat
            if abs(age) > _LOCK_STALE_S:
                stolen = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
                try:
                    os.replace(lock, stolen)
                    stolen.unlink()
                except OSError:
                    pass  # another waiter won the steal; re-loop and contend
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{lock.name} held by another process for >{timeout:.0f}s")
            time.sleep(0.01)
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)  # never leak the descriptor if the write fails
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass  # already stolen as stale; the next writer contends normally


def _append_marker(marker: Path, line: str) -> list[str]:
    """Append ``line`` to ``marker`` under :func:`_marker_lock`, retrying the
    transient-contention class.

    Returns the list of errors survived — empty when the first attempt worked,
    non-empty when a retry saved the record. **Raises** :class:`MarkerWriteError`
    when every attempt fails; callers must not swallow it (that swallow was the
    original defect). Kept separate from :func:`main` so the failure path is
    directly unit-testable without a real unwritable filesystem.
    """
    attempts: list[str] = []
    for attempt in range(1, _MARKER_WRITE_ATTEMPTS + 1):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            with _marker_lock(marker), marker.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                # Durability, best-effort: without it a crash between this hook
                # and the command it is recording can lose a buffered marker —
                # silent loss again, by a different route. An fsync that is not
                # supported by the target filesystem must not manufacture a
                # failure, so it gets its own narrow guard rather than the retry.
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            return attempts
        except OSError as exc:
            attempts.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt >= _MARKER_WRITE_ATTEMPTS:
                raise MarkerWriteError(marker, attempts) from exc
            time.sleep(_MARKER_RETRY_BASE_S * (2 ** (attempt - 1)))
    return attempts  # pragma: no cover — the loop either returns or raises


def _stderr(text: str) -> None:
    """Best-effort stderr write. Never raises: a closed fd 2 must not break the
    never-block contract. The EXIT CODE is what carries a failure when this
    channel is gone, which is why the caller does not depend on the result."""
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — see docstring
        pass


def _marker_failure_banner(marker: Path, command: str, survived: list[str]) -> str:
    """The loud one. Deliberately unmissable: this is the gate announcing that
    it has stopped recording, and the whole review chain downstream of the
    marker is now blind for this command."""
    detail = "\n".join(f"    {e}" for e in survived) or "    (no error detail)"
    return (
        "\n"
        "!! OPS MARKER WRITE FAILED — THIS MUTATION IS NOW UNAUDITED !!\n"
        f"   command : {command}\n"
        f"   marker  : {marker}\n"
        f"   errors  : every one of {len(survived)} attempt(s) failed\n"
        f"{detail}\n"
        "   The Stop gate keys off that marker, so it will NOT demand a review\n"
        "   for this command. The command itself was not blocked and still runs.\n"
        "   Do one of these before finishing the turn:\n"
        "     * fix the path and re-run the command, or\n"
        f"     * append '<UTC timestamp><TAB><command>' to {marker.name} by hand, or\n"
        "     * record the security/testing review in the ledger regardless.\n"
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        tool_name, command = _read_input(argv)
    except Exception:  # noqa: BLE001 — a hook must never crash the tool call
        return EXIT_OK

    # Only Bash tool calls are in scope. When invoked via argv fallback the
    # tool_name is empty; in that legacy path we still inspect the command.
    if tool_name and tool_name != "Bash":
        return EXIT_OK

    if not _is_mutating(command):
        return EXIT_OK

    root = _repo_root()
    marker = root / ".claude" / f"ops-review-owed-{_branch(root)}"
    stamp = datetime.now(timezone.utc).strftime(MARKER_TIMESTAMP_FORMAT)
    try:
        survived = _append_marker(marker, format_record(stamp, command))
    except OSError as exc:
        # LOUD, and never silent. Not blocking either: EXIT_MARKER_WRITE_FAILED
        # is 1, not the contract's blocking 2, so the command still runs — but a
        # lost audit record can no longer pass for a clean turn.
        attempts = getattr(exc, "attempts", None) or [f"{type(exc).__name__}: {exc}"]
        _stderr(_marker_failure_banner(marker, command, attempts))
        return EXIT_MARKER_WRITE_FAILED

    notice = "⚑ ops mutation logged — a security/testing review is now owed before this turn can finish\n"
    if survived:
        # The write only landed on a retry. Say so: silent recovery here would
        # hide exactly the contention that makes the failure path reachable.
        notice += f"  (marker write recovered after {len(survived)} failed attempt(s): {survived[-1]})\n"
    _stderr(notice)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
