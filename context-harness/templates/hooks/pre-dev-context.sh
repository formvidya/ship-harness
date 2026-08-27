#!/usr/bin/env bash
# Claude Code PreToolUse hook (Edit|Write): nudge to consult the context ledger
# before editing a code-root file. Thin wrapper — the config-aware
# decision lives in predev_gate.py so nothing project-specific is hard-coded here.
#
# Wired in .claude/settings.json under hooks.PreToolUse (matcher "Edit|Write").
# Override: CTX_SKIP_PREDEV=1, or just run `ctx query` (writes the marker).
exec python tools/harness/context-harness/ctx/predev_gate.py "$1"
