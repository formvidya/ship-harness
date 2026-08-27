# Installing context-harness in a new project

The harness folder is project-agnostic. Two ways in: the turnkey installer
(recommended), or the manual steps below it.

## Turnkey (recommended)

```bash
cp -r /path/to/tools/context-harness <your-repo>/tools/context-harness
cd <your-repo>
pip install pyyaml
python tools/harness/context-harness/install.py
```

`install.py` is idempotent and scaffolds everything: `.context/config.yml` (from
the template), the `context-check.yml` CI gate, the pre-dev hook, a merged
`settings.json` entry, the PR template, `.gitignore` entries, and runs
`ctx bootstrap` to render the context-keeper agent + ledger contract. Then edit
`.context/config.yml` to match your project and re-run `ctx bootstrap`. The only
manual steps it can't do: `pip install pyyaml` and making
`Context Check / per-change record present & valid` a required status check.

---

## Manual steps (what the installer does, if you prefer by hand)

## 1. Copy the harness

```bash
cp -r /path/to/tools/context-harness <your-repo>/tools/context-harness
pip install pyyaml
```

## 2. Fill the one config file

```bash
cp tools/harness/context-harness/templates/config.example.yml .context/config.yml
```

Edit `.context/config.yml` — the only project-specific surface:

- `project.{name, description, languages}`
- `code_roots` — globs the gate watches (a non-exempt change here needs a record)
- `exempt_globs` — paths that never need a record (docs, tests, CI)
- `reference_architecture` — your architecture doc path (optional; drives the
  architecture-drift detector)
- `ledger.*` — where records/digests/registry live
- `topic_seeds`, `risk_policy`, `role_bindings` — optional tuning

## 3. Render the CI gate

```bash
cp tools/harness/context-harness/templates/workflows/context-check.yml .github/workflows/
```

The template needs no edits — it calls
`tools/harness/context-harness/ctx/check_context_record.py`, which reads your config.

## 4. Make it required

Add **`Context Check / per-change record present & valid`** as a required status
check in branch protection. This is the load-bearing layer: it is the only one
that covers a human merging via the GitHub web UI.

## 5. (Optional) local pre-commit mirror

Add to `.pre-commit-config.yaml`:

```yaml
- id: context-record-check
  name: Context record check
  entry: python tools/harness/context-harness/ctx/check_context_record.py --staged
  language: system
  pass_filenames: false
  files: ^(<your code_roots>)
  stages: [pre-commit]
```

## Verify

```bash
# should PASS (no code-root files changed)
BASE_SHA=HEAD~1 HEAD_SHA=HEAD PR_NUMBER=1 \
  python tools/harness/context-harness/ctx/check_context_record.py
```

The rest of the ledger — the Historian write-path, query-before-dev, and the
Tier-2 synthesis layer — installs on top of this. See `docs/USAGE.md` for the
commands they add and `docs/SCHEMA.md` for the record format.
