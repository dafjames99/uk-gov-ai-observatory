"""GOV.UK announcements ingestion — the Axis B 'intent' feed.

Pulls AI-related announcements (press releases, policy papers, consultations,
speeches, etc.) from the GOV.UK Search API — the same API used for ATRS, just
filtered to announcement document types. The full-text query is a loose
candidate generator; each result is re-scored with the shared AI-relevance
classifier so only genuinely AI-related announcements are persisted.
"""

import json
import logging
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from src.common.http import RateLimitedSession
from src.ingest.ai_relevance import classify, config_version

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.gov.uk/api/search.json"
_PAGE_SIZE = 100
_FIELDS = [
    "link",
    "title",
    "description",
    "public_timestamp",
    "organisations",
    "content_store_document_type",
]
_CONFIG_PATH = Path(__file__).parents[2] / "config" / "announcements.yaml"


@lru_cache(maxsize=1)
def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_config(path: Path | None = None) -> dict:
    """Return the announcements ingestion config, cached after first load."""
    return _load_config(str(path or _CONFIG_PATH))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_announcements(
    session: RateLimitedSession,
    config: dict | None = None,
    organisations: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch candidate announcements for every configured search query.

    Results are unioned across queries and deduplicated on `link`. Uses the
    Search API's default relevance order (ordering by recency turns the query
    into a loose OR-match and surfaces unrelated content).

    Args:
        session: A configured RateLimitedSession.
        config: Override config dict (uses announcements.yaml if None).
        organisations: Optional list of organisation slugs to restrict to.

    Returns:
        Tuple of (unique_results, raw_pages) where raw_pages backs Bronze.
    """
    cfg = config or load_config()
    doc_types = cfg.get("document_types", [])
    queries = cfg.get("search_queries", [])
    max_per_query = cfg.get("max_per_query", 300)

    seen: set[str] = set()
    unique: list[dict] = []
    raw_pages: list[dict] = []

    for query in queries:
        fetched = 0
        start = 0
        while fetched < max_per_query:
            params: list[tuple[str, str]] = [("q", query), ("start", str(start)), ("count", str(_PAGE_SIZE))]
            params += [("filter_content_store_document_type", dt) for dt in doc_types]
            params += [("fields", f) for f in _FIELDS]
            params += [("filter_organisations", o) for o in (organisations or [])]

            logger.info("Search '%s' (start=%d)", query, start)
            data = session.get_json(_SEARCH_URL, params=params)
            raw_pages.append(data)
            results = data.get("results", [])
            if not results:
                break

            for r in results:
                link = r.get("link")
                if link and link not in seen:
                    seen.add(link)
                    unique.append(r)

            fetched += len(results)
            start += len(results)
            if start >= data.get("total", 0):
                break

    logger.info("Fetched %d unique announcements across %d queries", len(unique), len(queries))
    return unique, raw_pages


def save_bronze(raw_pages: list[dict], run_date: date, bronze_root: Path) -> Path:
    """Write raw Search API response pages to the Bronze layer."""
    out_dir = bronze_root / "announcements" / run_date.isoformat()
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


def _extract_organisations(raw: dict) -> list[dict[str, str | None]]:
    orgs: list[dict[str, str | None]] = []
    for o in raw.get("organisations", []) or []:
        link = o.get("link") or ""
        orgs.append(
            {
                "slug": link.rstrip("/").split("/")[-1] or None,
                "acronym": o.get("acronym"),
                "title": o.get("title"),
            }
        )
    return orgs


def parse_announcement(raw: dict) -> dict[str, Any] | None:
    """Parse a Search API result into a Silver gov_announcements dict.

    Returns None if the result lacks a link (its stable identifier).
    """
    link = raw.get("link")
    if not link:
        return None

    title = raw.get("title")
    description = raw.get("description")
    organisations = _extract_organisations(raw)
    confidence = classify(title, description, None)

    return {
        "announcement_id": link,
        "title": title,
        "document_type": raw.get("content_store_document_type"),
        "organisations": json.dumps(organisations) if organisations else None,
        "public_timestamp": raw.get("public_timestamp"),
        "updated_timestamp": None,
        "summary": description,
        "body_excerpt": None,
        "ai_relevant": confidence != "none",
        "ai_confidence": confidence,
        "ai_relevance_version": config_version(),
        "topic_tags": None,
        "enrichment_version": None,
        "source_url": f"https://www.gov.uk{link}",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Silver upsert
# ---------------------------------------------------------------------------


def upsert_announcements(
    records: list[dict[str, Any]],
    conn: duckdb.DuckDBPyConnection,
    ai_relevant_only: bool = True,
) -> int:
    """Upsert parsed announcements into Silver gov_announcements.

    Insert-only on announcement_id (idempotent). Optionally keeps only
    AI-relevant rows.

    Returns:
        Number of new rows inserted.
    """
    if not records:
        return 0

    df = pd.DataFrame(records)
    if ai_relevant_only:
        df = df[df["ai_relevant"] == True]  # noqa: E712
    if df.empty:
        return 0

    existing = {
        r[0]
        for r in conn.execute("SELECT announcement_id FROM gov_announcements").fetchall()
    }
    new_df = df[~df["announcement_id"].isin(existing)].drop_duplicates(subset=["announcement_id"])
    if new_df.empty:
        logger.info("No new announcements to insert (all %d already present)", len(df))
        return 0

    cols = ", ".join(new_df.columns)
    conn.register("_ann_tmp", new_df)
    conn.execute(f"INSERT INTO gov_announcements ({cols}) SELECT {cols} FROM _ann_tmp")
    conn.unregister("_ann_tmp")

    logger.info("Inserted %d new announcements into Silver", len(new_df))
    return len(new_df)
