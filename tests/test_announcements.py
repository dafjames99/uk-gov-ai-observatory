"""Tests for GOV.UK announcements parsing and upsert."""

import json

import pytest

from src.common.db import get_connection, init_schema
from src.ingest.announcements import (
    parse_announcement,
    upsert_announcements,
)

_AI_RESULT = {
    "link": "/government/news/new-ai-growth-zone-confirmed",
    "title": "New AI Growth Zone confirmed in North Wales",
    "description": "The government has confirmed a new artificial intelligence growth zone.",
    "content_store_document_type": "press_release",
    "public_timestamp": "2026-01-29T09:00:00Z",
    "organisations": [
        {
            "link": "/government/organisations/department-for-science-innovation-and-technology",
            "acronym": "DSIT",
            "title": "Department for Science, Innovation and Technology",
        }
    ],
}

_NON_AI_RESULT = {
    "link": "/government/news/bluetongue-latest-situation",
    "title": "Bluetongue: latest situation",
    "description": "An update on the bluetongue virus situation in livestock.",
    "content_store_document_type": "news_story",
    "public_timestamp": "2026-06-16T10:42:16Z",
    "organisations": [],
}


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_parse_ai_announcement():
    rec = parse_announcement(_AI_RESULT)
    assert rec["announcement_id"] == "/government/news/new-ai-growth-zone-confirmed"
    assert rec["document_type"] == "press_release"
    assert rec["ai_relevant"] is True
    assert rec["ai_confidence"] == "strong"  # "AI"/"artificial intelligence"
    assert rec["source_url"] == "https://www.gov.uk/government/news/new-ai-growth-zone-confirmed"
    orgs = json.loads(rec["organisations"])
    assert orgs[0]["slug"] == "department-for-science-innovation-and-technology"
    assert orgs[0]["acronym"] == "DSIT"


def test_parse_non_ai_announcement_scored_none():
    rec = parse_announcement(_NON_AI_RESULT)
    assert rec["ai_relevant"] is False
    assert rec["ai_confidence"] == "none"


def test_parse_missing_link_returns_none():
    assert parse_announcement({"title": "No link"}) is None


def test_upsert_ai_relevant_only(conn):
    records = [parse_announcement(_AI_RESULT), parse_announcement(_NON_AI_RESULT)]
    inserted = upsert_announcements(records, conn, ai_relevant_only=True)
    assert inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM gov_announcements").fetchone()[0] == 1


def test_upsert_idempotent(conn):
    records = [parse_announcement(_AI_RESULT)]
    assert upsert_announcements(records, conn) == 1
    assert upsert_announcements(records, conn) == 0
