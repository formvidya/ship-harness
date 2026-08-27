"""Unit tests for the generic change-risk classifier.

Tests the pure classify() with an injected diff function -- no git. Covers the
scope tiers (docs/tooling -> LOW, multi-unit/unclassified -> HIGH), protected
paths, and the content signals (secrets, schema add AND delete, dockerfile port).
Run: python -m pytest tools/harness/methodology-harness/tests/ -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import classify as cl  # noqa: E402

_RISK = {
    "protected_paths": ["services/*/src/core/auth.py", "**/*.tf"],
    "safe_nonservice": ["docs/**", "tools/**", ".github/**", ".context/**", "**/*.md"],
    "scope_roots": ["services/**"],
    "max_risk_files": 10,
    "signals": [
        {"name": "secrets", "path_glob": "**", "added_regex": r"(JWT_SECRET|AWS_SECRET).*="},
        {
            "name": "schema",
            "path_glob": "**/models/*.py",
            "added_regex": r"Field\(|Column\(",
            "removed_regex": r"Field\(|Column\(",
        },
        {"name": "dockerfile_port", "path_glob": "**/Dockerfile", "added_regex": r"EXPOSE\s"},
    ],
}


def _cfg():
    return SimpleNamespace(raw={"gates": {"change_review": {"risk": _RISK}}}, code_roots=("services/**",))


def _verdict(files, diffs=None):
    diffs = diffs or {}
    return cl.classify(_cfg(), files, lambda p: diffs.get(p, ""))


def test_docs_tooling_is_low():
    assert _verdict(["docs/x.md", "tools/y.py", ".github/workflows/z.yml"]).level == "LOW"


def test_single_service_is_low():
    assert _verdict(["services/billing/src/api/x.py", "services/billing/VERSION"]).level == "LOW"


def test_two_services_high():
    assert _verdict(["services/billing/src/x.py", "services/search/src/y.py"]).level == "HIGH"


def test_protected_path_high():
    assert _verdict(["services/billing/src/core/auth.py"]).level == "HIGH"
    assert _verdict(["infra/main.tf"]).level == "HIGH"


def test_stray_toplevel_high():
    assert _verdict(["services/search/src/x.py", "weird_root.py"]).level == "HIGH"


def test_big_docs_pr_low():
    assert _verdict([f"docs/d{i}.md" for i in range(15)]).level == "LOW"


def test_big_service_refactor_high():
    assert _verdict([f"services/search/src/f{i}.py" for i in range(12)]).level == "HIGH"


def test_secrets_signal():
    v = _verdict(["services/search/src/cfg.py"], {"services/search/src/cfg.py": "+JWT_SECRET = os.environ['X']\n"})
    assert v.level == "HIGH" and any("secrets" in r for r in v.reasons)


def test_markdown_mentioning_secret_keyword_is_low():
    # Regression: an agent doc / markdown that merely *mentions* a secret keyword
    # (e.g. a `JWT_SECRET = ...` example) is NOT a secrets change. Content signals
    # must be code-only -- this is the false positive that escalated a docs-only
    # PR to HIGH because an agent markdown file quoted a secret name in an example.
    add = "+Example: `JWT_SECRET = os.environ['X']`\n"
    assert _verdict([".context/agents/change-manager.md"], {".context/agents/change-manager.md": add}).level == "LOW"
    assert _verdict(["docs/runbook.md"], {"docs/runbook.md": add}).level == "LOW"


def test_schema_deletion_flags():
    # removing a Field is a breaking change -> must flag (the bug the review caught)
    v = _verdict(
        ["services/search/src/models/user.py"], {"services/search/src/models/user.py": "-    email = Field(...)\n"}
    )
    assert v.level == "HIGH" and any("schema" in r for r in v.reasons)


def test_schema_addition_flags():
    v = _verdict(
        ["services/search/src/models/user.py"], {"services/search/src/models/user.py": "+    age = Field(...)\n"}
    )
    assert v.level == "HIGH"


def test_dockerfile_expose_flags():
    v = _verdict(["services/search/Dockerfile"], {"services/search/Dockerfile": "+EXPOSE 9000\n"})
    assert v.level == "HIGH" and any("dockerfile" in r for r in v.reasons)


def test_unrelated_dockerfile_change_is_not_flagged_by_port_signal():
    # a Dockerfile change without EXPOSE is still a single-unit service change (LOW)
    v = _verdict(["services/search/Dockerfile"], {"services/search/Dockerfile": "+RUN pip install x\n"})
    assert v.level == "LOW"
