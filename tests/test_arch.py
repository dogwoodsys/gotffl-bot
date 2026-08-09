"""Architecture rules, enforced rather than reviewed.

These exist because the failures they prevent are the expensive ones: a
bespoke dedup table (LL-063 shipped four duplicate messages to a real family),
a reader that can post, or a credential in an environment variable.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = ROOT / "functions"
SHARED = ROOT / "layers" / "shared" / "python" / "shared"

FUNCTION_SOURCES = sorted(FUNCTIONS.glob("*/[!t]*.py"))
ALL_SOURCES = FUNCTION_SOURCES + sorted(SHARED.glob("*.py"))


def test_sources_were_found():
    """Guard against a glob that silently matches nothing and passes everything."""
    assert len(FUNCTION_SOURCES) >= 12
    assert len(ALL_SOURCES) >= 20


@pytest.mark.parametrize("path", ALL_SOURCES, ids=lambda p: p.name)
def test_no_print_statements(path):
    """Structured logging only — except the EMF metric line, which must be raw."""
    source = path.read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("print("):
            assert "_emit_published_metric" in source, f"{path.name}: use get_logger, not print"


@pytest.mark.parametrize("path", FUNCTION_SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_bespoke_dedup_state(path):
    """Dedup lives in shared/idempotency.py. Module-level mutable state does not
    survive concurrent invocations and vanishes on cold start."""
    source = path.read_text()
    bad = re.compile(r"^(_?seen|_?processed|_?sent|_?dedup)\w*\s*[:=]", re.MULTILINE)
    assert not bad.search(source), f"{path.name}: module-level dedup state"


@pytest.mark.parametrize("path", FUNCTION_SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_functions_do_not_write_dedup_markers_directly(path):
    """A handler that writes its own marker bypasses the claim/confirm ordering."""
    source = path.read_text()
    if "idempotency" in source:
        assert "put_item" not in source or "SHADOW" in source, (
            f"{path.name}: write dedup state through shared.idempotency"
        )


def test_only_the_publisher_imports_the_x_client():
    for path in FUNCTION_SOURCES:
        if path.parent.name == "publish":
            continue
        assert "x_client" not in path.read_text(), f"{path}: readers must not import the X client"


def test_publisher_does_not_import_the_yahoo_client():
    for path in (FUNCTIONS / "publish").glob("*.py"):
        assert "shared.yahoo" not in path.read_text(), "the publisher must not read Yahoo"


@pytest.mark.parametrize("path", ALL_SOURCES, ids=lambda p: p.name)
def test_no_hardcoded_credentials(path):
    """Credentials come from Parameter Store at runtime, never from source."""
    source = path.read_text()
    for pattern in (r"AKIA[0-9A-Z]{16}", r"sk-[A-Za-z0-9]{20,}", r"Bearer\s+[A-Za-z0-9._-]{20,}"):
        assert not re.search(pattern, source), f"{path.name}: possible hardcoded credential"


@pytest.mark.parametrize(
    "fn", ["poll_transactions", "post_scores", "post_standings", "post_matchups", "publish"]
)
def test_every_function_has_the_standard_layout(fn):
    for required in ("handler.py", "processor.py", "validator.py"):
        assert (FUNCTIONS / fn / required).exists(), f"{fn} is missing {required}"


@pytest.mark.parametrize(
    "fn", ["poll_transactions", "post_scores", "post_standings", "post_matchups", "publish"]
)
def test_handlers_contain_no_business_logic(fn):
    """Handler orchestrates; processor does the work."""
    source = (FUNCTIONS / fn / "handler.py").read_text()
    for banned in ("boto3", "requests", "render_", "YahooClient", "XClient"):
        assert banned not in source, f"{fn}/handler.py should delegate {banned} to the processor"
