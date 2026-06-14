"""Tests for ATRS record parsing and Silver upsert."""

import pytest

from src.common.db import get_connection, init_schema
from src.ingest.atrs import _extract_body_fields, parse_record, upsert_records

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BODY_V4 = """
<h2 id="summary">1. Summary</h2>
<h3 id="name">1 - Name</h3>
<p>EPS Assist Me</p>
<h3 id="description">2 - Description</h3>
<p>A chatbot that answers questions about the Electronic Prescription Service.</p>
<h2 id="tool-specification">4. Tool Specification</h2>
<h3 id="model-architecture">33 - Model architecture</h3>
<p>Large language model (GPT-4 based) with retrieval-augmented generation.</p>
<h3 id="sensitive-attributes">47 - Sensitive attributes</h3>
<p>NINOs, telephone numbers, addresses, postcodes.</p>
<h3 id="sensitive-attributes-1">54 - Sensitive attributes (dataset 2)</h3>
<p>Protected characteristics not used as model input.</p>
<h2 id="impact">5. Impact Assessments</h2>
<h3 id="impact-assessments">58 - Impact assessments</h3>
<p>DPIA completed. See <a href="https://example.gov.uk/dpia/eps-assist-me">full DPIA</a>.</p>
"""

_BODY_NO_TIER2 = """
<h2 id="summary">1. Summary</h2>
<h3 id="name">1 - Name</h3>
<p>Simple Tool</p>
<h3 id="description">2 - Description</h3>
<p>A simple algorithmic tool.</p>
"""

_RAW_RECORD_V4 = {
    "base_path": "/algorithmic-transparency-records/eps-assist-me",
    "title": "NHS England: EPS Assist Me",
    "description": "A chatbot for Electronic Prescription Service queries.",
    "details": {
        "metadata": {
            "algorithmic_transparency_record_atrs_version": "v4.0",
            "algorithmic_transparency_record_date_published": "2026-05-28",
            "algorithmic_transparency_record_phase": "private-beta",
            "algorithmic_transparency_record_organisation": "nhs-england",
        },
        "body": _BODY_V4,
    },
}

_RAW_RECORD_NO_COLON_TITLE = {
    "base_path": "/algorithmic-transparency-records/some-tool",
    "title": "Some Council Tool",
    "description": "A tool with no colon in title.",
    "details": {
        "metadata": {
            "algorithmic_transparency_record_atrs_version": "v3.0",
            "algorithmic_transparency_record_date_published": "2025-01-28",
            "algorithmic_transparency_record_phase": "production",
        },
        "body": _BODY_NO_TIER2,
    },
}

# ---------------------------------------------------------------------------
# _extract_body_fields tests
# ---------------------------------------------------------------------------


def test_extracts_model_architecture():
    result = _extract_body_fields(_BODY_V4)
    assert result["model_architecture"] is not None
    assert "GPT-4" in result["model_architecture"]


def test_extracts_sensitive_attributes_joins_multiple():
    result = _extract_body_fields(_BODY_V4)
    assert result["sensitive_attributes"] is not None
    assert "NINOs" in result["sensitive_attributes"]
    assert "Protected characteristics" in result["sensitive_attributes"]


def test_extracts_dpia_url():
    result = _extract_body_fields(_BODY_V4)
    assert result["dpi_assessment_url"] == "https://example.gov.uk/dpia/eps-assist-me"


def test_missing_tier2_returns_none():
    result = _extract_body_fields(_BODY_NO_TIER2)
    assert result["model_architecture"] is None
    assert result["sensitive_attributes"] is None
    assert result["dpi_assessment_url"] is None


def test_empty_body_returns_none():
    result = _extract_body_fields("")
    assert result == {"model_architecture": None, "sensitive_attributes": None, "dpi_assessment_url": None}


# ---------------------------------------------------------------------------
# parse_record tests
# ---------------------------------------------------------------------------


def test_parse_record_core_fields():
    r = parse_record(_RAW_RECORD_V4)
    assert r is not None
    assert r["record_id"] == "eps-assist-me"
    assert r["organisation_name"] == "NHS England"
    assert r["phase"] == "private-beta"
    assert r["standard_version"] == "v4.0"
    assert r["date_published"] == "2026-05-28"
    assert r["one_sentence_desc"] == "A chatbot for Electronic Prescription Service queries."
    assert r["source_url"] == "https://www.gov.uk/algorithmic-transparency-records/eps-assist-me"
    assert r["ingested_at"] is not None


def test_parse_record_tier2_fields():
    r = parse_record(_RAW_RECORD_V4)
    assert r["model_architecture"] is not None
    assert r["sensitive_attributes"] is not None
    assert r["dpi_assessment_url"] == "https://example.gov.uk/dpia/eps-assist-me"


def test_parse_record_title_without_colon():
    r = parse_record(_RAW_RECORD_NO_COLON_TITLE)
    assert r["organisation_name"] == "Some Council Tool"


def test_parse_record_missing_base_path_returns_none():
    assert parse_record({"title": "No base path"}) is None


# ---------------------------------------------------------------------------
# upsert_records tests
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_upsert_inserts_records(conn):
    records = [parse_record(_RAW_RECORD_V4), parse_record(_RAW_RECORD_NO_COLON_TITLE)]
    inserted = upsert_records(records, conn)
    assert inserted == 2
    count = conn.execute("SELECT COUNT(*) FROM atrs_records").fetchone()[0]
    assert count == 2


def test_upsert_idempotent(conn):
    records = [parse_record(_RAW_RECORD_V4)]
    assert upsert_records(records, conn) == 1
    assert upsert_records(records, conn) == 0


def test_upsert_empty_list(conn):
    assert upsert_records([], conn) == 0
