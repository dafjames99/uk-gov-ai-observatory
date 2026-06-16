"""Tests for Written Parliamentary Questions parsing and upsert."""

import pytest

from src.common.db import get_connection, init_schema
from src.ingest.written_questions import parse_question, upsert_questions

_AI_Q = {
    "id": 1913020,
    "uin": "7108",
    "house": "Commons",
    "dateTabled": "2026-06-05T00:00:00",
    "dateAnswered": "2026-06-16T00:00:00",
    "askingMember": {"name": "Jane Smith MP"},
    "answeringBodyName": "Department for Science, Innovation and Technology",
    "heading": "Artificial Intelligence",
    "questionText": "To ask about the use of large language models across government.",
    "answerText": "The Department is exploring generative AI tools.",
}

_NON_AI_Q = {
    "id": 555,
    "uin": "999",
    "house": "Lords",
    "dateTabled": "2026-05-01T00:00:00",
    "dateAnswered": None,
    "askingMember": {"nameDisplayAs": "Lord Example"},
    "answeringBodyName": "Department for Environment, Food and Rural Affairs",
    "heading": "Livestock: Artificial Insemination",
    "questionText": "To ask about cattle breeding programmes.",
    "answerText": None,
}


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_parse_ai_question():
    rec = parse_question(_AI_Q)
    assert rec["question_id"] == "1913020"  # durable API id, not the reusable UIN
    assert rec["house"] == "Commons"
    assert rec["date_tabled"] == "2026-06-05"
    assert rec["date_answered"] == "2026-06-16"
    assert rec["member_name"] == "Jane Smith MP"
    assert rec["department"] == "Department for Science, Innovation and Technology"
    assert rec["ai_relevance_flag"] is True
    assert rec["source_url"] == (
        "https://questions-statements.parliament.uk/written-questions/detail/2026-06-05/7108"
    )


def test_parse_non_ai_question_flagged_false():
    # 'Artificial Insemination' in the heading must not score as AI.
    rec = parse_question(_NON_AI_Q)
    assert rec["ai_relevance_flag"] is False
    assert rec["member_name"] == "Lord Example"
    assert rec["date_answered"] is None


def test_parse_missing_id_returns_none():
    assert parse_question({"uin": "1"}) is None


def test_upsert_ai_relevant_only(conn):
    records = [parse_question(_AI_Q), parse_question(_NON_AI_Q)]
    assert upsert_questions(records, conn, ai_relevant_only=True) == 1
    assert conn.execute("SELECT COUNT(*) FROM written_questions").fetchone()[0] == 1


def test_upsert_idempotent(conn):
    records = [parse_question(_AI_Q)]
    assert upsert_questions(records, conn) == 1
    assert upsert_questions(records, conn) == 0
