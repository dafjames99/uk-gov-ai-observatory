"""Tests for OCDS release parsing and Silver upsert."""

import json
from datetime import date
from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.ingest.procurement import parse_release, upsert_notices

# ---------------------------------------------------------------------------
# Minimal OCDS release fixture
# ---------------------------------------------------------------------------

_AWARD_RELEASE = {
    "ocid": "ocds-b5fd17-test-001",
    "id": "ocds-b5fd17-test-001-award-1",
    "date": "2025-03-15T10:00:00Z",
    "tag": ["award"],
    "tender": {
        "title": "Machine Learning Fraud Detection Platform",
        "description": "Automated fraud detection using machine learning algorithms.",
        "value": {"amount": 250000, "currency": "GBP"},
        "items": [
            {
                "id": "1",
                "classification": {"scheme": "CPV", "id": "72212000", "description": "Programming services"},
            }
        ],
        "contractPeriod": {"startDate": "2025-04-01T00:00:00Z", "endDate": "2026-03-31T00:00:00Z"},
    },
    "buyer": {
        "name": "Department for Work and Pensions",
        "identifier": {"scheme": "GB-GOR", "id": "D10"},
    },
    "awards": [
        {
            "id": "award-1",
            "value": {"amount": 240000, "currency": "GBP"},
            "suppliers": [
                {"name": "TechCorp Ltd", "identifier": {"scheme": "GB-COH", "id": "12345678"}}
            ],
            "contractPeriod": {"startDate": "2025-04-15T00:00:00Z", "endDate": "2026-04-14T00:00:00Z"},
        }
    ],
}

_TENDER_NO_AWARD = {
    "ocid": "ocds-b5fd17-test-002",
    "id": "ocds-b5fd17-test-002-tender-1",
    "date": "2025-03-20T09:00:00Z",
    "tag": ["tender"],
    "tender": {
        "title": "Office Cleaning Services",
        "description": "Weekly cleaning contract for government offices.",
        "value": {"amount": 50000, "currency": "GBP"},
        "items": [
            {"id": "1", "classification": {"scheme": "CPV", "id": "90911200", "description": "Office cleaning"}}
        ],
    },
    "buyer": {"name": "Cabinet Office", "identifier": {"scheme": "GB-GOR", "id": "CO"}},
}

_ANOMALOUS_DATES = {
    "ocid": "ocds-b5fd17-test-003",
    "id": "ocds-b5fd17-test-003-award-1",
    "date": "2025-03-10T00:00:00Z",
    "tag": ["award"],
    "tender": {"title": "AI Data Processing Service", "description": "Uses artificial intelligence."},
    "buyer": {"name": "HMRC", "identifier": {"id": "HMRC"}},
    "awards": [
        {
            "suppliers": [{"name": "DataCo", "identifier": {"id": "99999999"}}],
            "contractPeriod": {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2025-01-01T00:00:00Z",  # end before start
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# parse_release tests
# ---------------------------------------------------------------------------


def test_parse_award_release():
    n = parse_release(_AWARD_RELEASE)
    assert n is not None
    assert n["notice_id"] == "ocds-b5fd17-test-001::ocds-b5fd17-test-001-award-1"
    assert n["source"] == "contracts_finder"
    assert n["stage"] == "award"
    assert n["title"] == "Machine Learning Fraud Detection Platform"
    assert n["value_amount"] == 240000.0  # from award, not tender
    assert n["currency"] == "GBP"
    assert n["buyer_name"] == "Department for Work and Pensions"
    assert n["supplier_name"] == "TechCorp Ltd"
    assert n["supplier_id"] == "12345678"
    assert json.loads(n["cpv_codes"]) == ["72212000"]
    assert n["published_date"] == "2025-03-15"
    assert n["contract_start"] == "2025-04-15"
    assert n["contract_end"] == "2026-04-14"
    assert n["ai_relevant"] is True
    assert n["link_status"] == "ok"


def test_parse_non_ai_notice():
    n = parse_release(_TENDER_NO_AWARD)
    assert n is not None
    assert n["ai_relevant"] is False
    assert n["supplier_name"] == "supplier_unknown"
    assert n["stage"] == "tender"


def test_parse_anomalous_dates_nulled():
    n = parse_release(_ANOMALOUS_DATES)
    assert n is not None
    assert n["contract_start"] is None
    assert n["contract_end"] is None


def test_parse_missing_ocid_returns_none():
    assert parse_release({"id": "no-ocid"}) is None


def test_parse_missing_id_returns_none():
    assert parse_release({"ocid": "ocds-b5fd17-xyz"}) is None


# ---------------------------------------------------------------------------
# upsert_notices tests
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_upsert_inserts_ai_relevant_only(conn):
    notices = [parse_release(_AWARD_RELEASE), parse_release(_TENDER_NO_AWARD)]
    inserted = upsert_notices(notices, conn, ai_relevant_only=True)
    assert inserted == 1
    count = conn.execute("SELECT COUNT(*) FROM procurement_notices").fetchone()[0]
    assert count == 1


def test_upsert_idempotent(conn):
    notices = [parse_release(_AWARD_RELEASE)]
    assert upsert_notices(notices, conn) == 1
    assert upsert_notices(notices, conn) == 0  # second run inserts nothing


def test_upsert_all_when_flag_off(conn):
    notices = [parse_release(_AWARD_RELEASE), parse_release(_TENDER_NO_AWARD)]
    inserted = upsert_notices(notices, conn, ai_relevant_only=False)
    assert inserted == 2
