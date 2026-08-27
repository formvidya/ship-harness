#!/usr/bin/env python3
"""Generic code-quality gate.

Runs each DECLARED language's format-check + lint on the files that language owns
in the PR diff. "Language is a declared list in config; each language is an
adapter" (the review's stack-monoculture fix): the gate has no hard-coded ruff,
prettier, eslint, or dart -- those are the ``lint``/``format`` commands inside
``languages[]`` in ``.context/config.yml``.

    languages:
      - id: python
        detect: ["**/*.py"]
        format: "ruff format --check {files}"
        lint:   "ruff check --output-format=json {files}"
      - id: dart
        detect: ["**/*.dart"]
        format: "dart format --output=none --set-exit-if-changed {dir}"
        lint:   "flutter analyze {dir}"

``{files}`` -> the space-joined changed files of that language; ``{dir}`` -> their
common parent. Only languages with changed files run, so a docs-only PR is a
no-op. A configured tool that isn't installed is a FAILURE (a gate with no
consequence is theater) with a clear message.

Modes: CI (``BASE_SHA``/``HEAD_SHA``) and ``--staged``. Override:
``[skip-quality-check]`` in any commit message in the range (logged).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_CTX = Path(__file__).resolve().parents[2] / "context-harness" / "ctx"
sys.path.insert(0, str(_CTX))
from config import Config, _glob_match, load_config  # noqa: E402

SKIP_MARKER = "[skip-quality-check]"


def _git(cfg: Config, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cfg.repo_root, capture_output=True, text=True, check=True).stdout


def changed_files(cfg: Config, base, head, staged) -> list[str]:
    if staged:
        out = _git(cfg, "diff", "--cached", "--name-only")
    else:
        if not base or not head:
            raise SystemExit("CI mode requires BASE_SHA and HEAD_SHA.")
        out = _git(cfg, "diff", "--name-only", f"{base}..{head}")
    return [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]


def _common_dir(files: list[str]) -> str:
    if not files:
        return "."
    parents = [str(Path(f).parent.as_posix()) for f in files]
    return os.path.commonpath(parents).replace("\\", "/") or "."


def _files_for(lang: dict, changed: list[str], cfg: Config) -> list[str]:
    detect = lang.get("detect", [])
    return [
        f
        for f in changed
        if any(_glob_match(g, f) for g in detect)
        and not cfg.is_exempt(f)
        and (cfg.repo_root / f).is_file()  # skip deleted files
    ]


def _run_cmd(cfg: Config, template: str, files: list[str]) -> tuple[int, str]:
    # Build an argv LIST (no shell): config-specified commands can't spawn a
    # shell, and file paths stay separate argv items (no quoting/injection
    # surface). A command that genuinely needs a shell writes `bash -lc '...'`.
    argv: list[str] = []
    for tok in shlex.split(template):
        if tok == "{files}":
            argv.extend(files)
        elif "{dir}" in tok:
            argv.append(tok.replace("{dir}", _common_dir(files)))
        elif "{files}" in tok:
            argv.extend(tok.replace("{files}", f) for f in files)
        else:
            argv.append(tok)
    try:
        r = subprocess.run(argv, cwd=cfg.repo_root, capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr)
    except FileNotFoundError:
        return 127, f"{argv[0] if argv else '?'}: command not found"
    except OSError as e:  # pragma: no cover
        return 127, str(e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    changed = changed_files(cfg, base, head, args.staged)
    print(f"code-quality: mode={'staged' if args.staged else 'CI'}, {len(changed)} file(s) in diff")

    if SKIP_MARKER in _commit_messages(cfg, base, head, args.staged):
        print(f"  [!] SKIPPED via {SKIP_MARKER} (logged)")
        return 0

    languages = cfg.raw.get("languages", []) or []
    failures: list[str] = []
    ran_any = False
    for lang in languages:
        lid = lang.get("id", "?")
        files = _files_for(lang, changed, cfg)
        if not files:
            continue
        ran_any = True
        print(f"\n  [{lid}] {len(files)} file(s)")
        for key in ("format", "lint"):
            template = lang.get(key)
            if not template:
                continue
            code, out = _run_cmd(cfg, template, files)
            if code == 127 or "not found" in out.lower():
                failures.append(
                    f"{lid} {key}: tool not installed -- `{template.split()[0]}` "
                    f"(install it in CI or remove the language from config)"
                )
                print(f"    {key}: TOOL MISSING")
            elif code != 0:
                snippet = out.strip().splitlines()[-3:] if out.strip() else []
                failures.append(f"{lid} {key} failed:\n        " + "\n        ".join(snippet))
                print(f"    {key}: FAIL")
            else:
                print(f"    {key}: ok")

    if not ran_any:
        print("\ncode-quality: no files for any declared language -> PASS")
        return 0
    if failures:
        print("\ncode-quality: FAIL")
        for f in failures:
            print(f"  -> {f}")
        print(f"\nFix the findings, OR add '{SKIP_MARKER}' to a commit message (logged).")
        return 1
    print("\ncode-quality: PASS")
    return 0


def _commit_messages(cfg: Config, base, head, staged) -> str:
    try:
        if staged:
            return _git(cfg, "log", "-1", "--format=%B")
        return _git(cfg, "log", "--format=%B", f"{base}..{head}")
    except subprocess.CalledProcessError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
