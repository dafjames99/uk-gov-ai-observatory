"""UK Parliament Written Questions ingestion — the Axis B 'scrutiny' feed.

Pulls AI-related Written Parliamentary Questions from the Questions &
Statements API (questions-statements-api.parliament.uk). Specific AI search
terms generate candidates (bare 'AI' is avoided — Parliament has many
'artificial insemination' questions); each candidate is re-scored with the
shared AI-relevance classifier for precision.

Licence: Open Parliament Licence (distinct from OGL v3.0 — attribute
separately).
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.common.http import RateLimitedSession
from src.ingest.ai_relevance import classify, config_version

logger = logging.getLogger(__name__)

_API_URL = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_DETAIL_URL = "https://questions-statements.parliament.uk/written-questions/detail"
_PAGE_SIZE = 100

DEFAULT_SEARCH_TERMS = [
    "artificial intelligence",
    "machine learning",
    "large language model",
    "generative AI",
    "facial recognition",
    "automated decision",
]
DEFAULT_MAX_PER_TERM = 300


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_questions(
    session: RateLimitedSession,
    search_terms: list[str] | None = None,
    max_per_term: int = DEFAULT_MAX_PER_TERM,
) -> tuple[list[dict], list[dict]]:
    """Fetch candidate written questions for each search term.

    Unioned across terms and deduplicated on the durable question id.

    Returns:
        Tuple of (unique question value-dicts, raw_pages) for Bronze.
    """
    terms = search_terms or DEFAULT_SEARCH_TERMS
    seen: set[int] = set()
    unique: list[dict] = []
    raw_pages: list[dict] = []

    for term in terms:
        fetched = 0
        skip = 0
        while fetched < max_per_term:
            params = {"searchTerm": term, "take": _PAGE_SIZE, "skip": skip}
            logger.info("WPQ search '%s' (skip=%d)", term, skip)
            data = session.get_json(_API_URL, params=params)
            raw_pages.append(data)
            results = data.get("results", [])
            if not results:
                break

            for item in results:
                value = item.get("value", item)
                qid = value.get("id")
                if qid is not None and qid not in seen:
                    seen.add(qid)
                    unique.append(value)

            fetched += len(results)
            skip += len(results)
            if skip >= data.get("totalResults", 0):
                break

    logger.info("Fetched %d unique written questions across %d terms", len(unique), len(terms))
    return unique, raw_pages


def save_bronze(raw_pages: list[dict], run_date: date, bronze_root: Path) -> Path:
    """Write raw API response pages to the Bronze layer."""
    out_dir = bronze_root / "wpq" / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(raw_pages, start=1):
        (out_dir / f"page_{i:04d}.json").write_text(
            json.dumps(page, ensure_ascii=False), encoding="utf-8"
        )
    logger.info("Saved %d Bronze pages to %s", len(raw_pages), out_dir)
    return out_dir


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _member_name(value: dict) -> str | None:
    member = value.get("askingMember") or {}
    return member.get("name") or member.get("nameDisplayAs") or member.get("listAs")


def parse_question(value: dict) -> dict[str, Any] | None:
    """Parse a written-question value dict into a Silver written_questions dict.

    Returns None if the question lacks a stable id.
    """
    qid = value.get("id")
    if qid is None:
        return None

    date_tabled = (value.get("dateTabled") or "")[:10] or None
    date_answered = (value.get("dateAnswered") or "")[:10] or None
    uin = value.get("uin")
    heading = value.get("heading")
    question_text = value.get("questionText")

    confidence = classify(heading, question_text, None)

    source_url = None
    if date_tabled and uin:
        source_url = f"{_DETAIL_URL}/{date_tabled}/{uin}"

    return {
        "question_id": str(qid),
        "house": value.get("house"),
        "date_tabled": date_tabled,
        "date_answered": date_answered,
        "member_name": _member_name(value),
        "department": value.get("answeringBodyName"),
        "question_text": question_text,
        "answer_text": value.get("answerText"),
        "ai_relevance_flag": confidence != "none",
        "topic_tags": None,
        "enrichment_version": None,
        "source_url": source_url,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Silver upsert
# ---------------------------------------------------------------------------


def upsert_questions(
    records: list[dict[str, Any]],
    conn: duckdb.DuckDBPyConnection,
    ai_relevant_only: bool = True,
) -> int:
    """Upsert parsed questions into Silver written_questions (insert-only)."""
    if not records:
        return 0

    df = pd.DataFrame(records)
    if ai_relevant_only:
        df = df[df["ai_relevance_flag"] == True]  # noqa: E712
    if df.empty:
        return 0

    existing = {
        r[0]
        for r in conn.execute("SELECT question_id FROM written_questions").fetchall()
    }
    new_df = df[~df["question_id"].isin(existing)].drop_duplicates(subset=["question_id"])
    if new_df.empty:
        logger.info("No new written questions to insert (all %d already present)", len(df))
        return 0

    cols = ", ".join(new_df.columns)
    conn.register("_wpq_tmp", new_df)
    conn.execute(f"INSERT INTO written_questions ({cols}) SELECT {cols} FROM _wpq_tmp")
    conn.unregister("_wpq_tmp")

    logger.info("Inserted %d new written questions into Silver", len(new_df))
    return len(new_df)
