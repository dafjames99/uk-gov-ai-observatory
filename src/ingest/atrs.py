"""ATRS ingestion — fetch records from GOV.UK APIs and parse into Silver."""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from bs4 import BeautifulSoup

from src.common.http import RateLimitedSession

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.gov.uk/api/search.json"
_CONTENT_URL = "https://www.gov.uk/api/content"
_SEARCH_PAGE_SIZE = 100

# h3[id] anchors to try when extracting Tier 2 fields.
# Each list is tried in order; first non-empty value wins.
_MODEL_ARCH_IDS = ["model-architecture", "model-type", "algorithm-type", "system-architecture"]
_SENSITIVE_ATTR_IDS = ["sensitive-attributes", "personal-and-sensitive-data", "equalities-characteristics"]
_DPIA_SECTION_IDS = ["impact-assessments", "dpia", "data-protection-impact-assessment"]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_all_slugs(session: RateLimitedSession) -> list[str]:
    """Return base_path slugs for every published ATRS record.

    Uses the GOV.UK Search API, paginating until exhausted.

    Args:
        session: A configured RateLimitedSession.

    Returns:
        List of base_path strings, e.g. ["/algorithmic-transparency-records/eps-assist-me", ...]
    """
    slugs: list[str] = []
    start = 0

    while True:
        params = {
            "filter_document_type": "algorithmic_transparency_record",
            "count": _SEARCH_PAGE_SIZE,
            "start": start,
            "fields": "link,title,public_timestamp",
        }
        logger.info("Fetching ATRS record listing (start=%d)", start)
        data = session.get_json(_SEARCH_URL, params=params)
        results = data.get("results", [])

        if not results:
            break

        for r in results:
            link = r.get("link")
            if link:
                slugs.append(link)

        total = data.get("total", 0)
        start += len(results)

        if start >= total:
            break

    logger.info("Discovered %d ATRS records", len(slugs))
    return slugs


def fetch_record(session: RateLimitedSession, slug: str) -> dict:
    """Fetch a single ATRS record from the GOV.UK Content API.

    Args:
        session: A configured RateLimitedSession.
        slug: The base_path of the record, e.g. "/algorithmic-transparency-records/eps-assist-me".

    Returns:
        Raw Content API response dict.
    """
    url = f"{_CONTENT_URL}{slug}"
    return session.get_json(url)


def save_bronze(
    listing_pages: list[dict],
    record_responses: dict[str, dict],
    run_date: date,
    bronze_root: Path,
) -> Path:
    """Write raw API responses to the Bronze layer.

    Args:
        listing_pages: Raw Search API response dicts (one per page).
        record_responses: Mapping of slug → raw Content API response dict.
        run_date: Date the run was executed.
        bronze_root: Root Bronze directory.

    Returns:
        Path to the run directory.
    """
    out_dir = bronze_root / "atrs" / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    listing_dir = out_dir / "listing"
    listing_dir.mkdir(exist_ok=True)
    for i, page in enumerate(listing_pages, start=1):
        (listing_dir / f"page_{i:04d}.json").write_text(
            json.dumps(page, ensure_ascii=False), encoding="utf-8"
        )

    records_dir = out_dir / "records"
    records_dir.mkdir(exist_ok=True)
    for slug, raw in record_responses.items():
        filename = slug.split("/")[-1] + ".json"
        (records_dir / filename).write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

    logger.info(
        "Saved Bronze: %d listing pages + %d record responses to %s",
        len(listing_pages),
        len(record_responses),
        out_dir,
    )
    return out_dir


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _extract_body_fields(body_html: str) -> dict[str, str | None]:
    """Extract Tier 2 fields from the embedded HTML body.

    Builds a mapping of h3[id] → concatenated text of following paragraphs,
    then looks up known field IDs with version-aware fallback lists.

    Args:
        body_html: The full HTML string from details.body.

    Returns:
        Dict with keys: model_architecture, sensitive_attributes, dpi_assessment_url.
    """
    soup = BeautifulSoup(body_html, "html.parser")

    # Build {h3_id: text} for all h3 sections
    sections: dict[str, str] = {}
    section_links: dict[str, list[str]] = {}

    for h3 in soup.find_all("h3"):
        sid = h3.get("id")
        if not sid:
            continue
        texts: list[str] = []
        hrefs: list[str] = []
        for sibling in h3.next_siblings:
            if getattr(sibling, "name", None) in ("h2", "h3"):
                break
            if getattr(sibling, "name", None) == "p":
                texts.append(sibling.get_text(separator=" ", strip=True))
                for a in sibling.find_all("a", href=True):
                    hrefs.append(a["href"])
            elif getattr(sibling, "name", None) == "ul":
                texts.append(
                    ", ".join(li.get_text(strip=True) for li in sibling.find_all("li"))
                )
        sections[sid] = " ".join(texts).strip()
        section_links[sid] = hrefs

    def _join_matching(id_patterns: list[str]) -> str | None:
        parts: list[str] = []
        for sid, text in sections.items():
            if any(sid == pat or sid.startswith(pat + "-") for pat in id_patterns):
                if text:
                    parts.append(text)
        return " | ".join(parts) or None

    def _first_link(id_patterns: list[str]) -> str | None:
        for sid in sections:
            if any(sid == pat or sid.startswith(pat + "-") for pat in id_patterns):
                for href in section_links.get(sid, []):
                    if href.startswith("http"):
                        return href
        return None

    return {
        "model_architecture": _join_matching(_MODEL_ARCH_IDS),
        "sensitive_attributes": _join_matching(_SENSITIVE_ATTR_IDS),
        "dpi_assessment_url": _first_link(_DPIA_SECTION_IDS),
    }


def parse_record(raw: dict) -> dict[str, Any] | None:
    """Parse a GOV.UK Content API response into a Silver atrs_records dict.

    Returns None if the raw response is missing a base_path.

    Args:
        raw: Raw Content API response dict.

    Returns:
        Normalised record dict, or None.
    """
    base_path = raw.get("base_path")
    if not base_path:
        logger.debug("Skipping record missing base_path")
        return None

    record_id = base_path.rstrip("/").split("/")[-1]
    title = raw.get("title", "")
    # Org name is consistently the part before the first colon in the title
    org_name = title.split(":", 1)[0].strip() if ":" in title else title.strip()

    details = raw.get("details", {})
    metadata = details.get("metadata", {})
    body_html = details.get("body", "")

    tier2 = _extract_body_fields(body_html)

    return {
        "record_id": record_id,
        "organisation_name": org_name,
        "phase": metadata.get("algorithmic_transparency_record_phase"),
        "one_sentence_desc": raw.get("description"),
        "model_architecture": tier2["model_architecture"],
        "sensitive_attributes": tier2["sensitive_attributes"],
        "dpi_assessment_url": tier2["dpi_assessment_url"],
        "date_published": metadata.get("algorithmic_transparency_record_date_published"),
        "standard_version": metadata.get("algorithmic_transparency_record_atrs_version"),
        "source_url": f"https://www.gov.uk{base_path}",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Silver upsert
# ---------------------------------------------------------------------------


def upsert_records(
    records: list[dict[str, Any]],
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Upsert parsed ATRS records into the Silver atrs_records table.

    Only inserts new rows — existing record_ids are skipped.

    Args:
        records: List of normalised record dicts from parse_record().
        conn: An open DuckDB connection.

    Returns:
        Number of new rows inserted.
    """
    if not records:
        return 0

    df = pd.DataFrame(records)
    existing = {
        r[0]
        for r in conn.execute("SELECT record_id FROM atrs_records").fetchall()
    }
    new_df = df[~df["record_id"].isin(existing)]

    if new_df.empty:
        logger.info("No new ATRS records to insert (all %d already present)", len(df))
        return 0

    # Insert by explicit column names (not positional SELECT *) so the insert
    # stays correct if the table gains columns the parser doesn't emit.
    cols = ", ".join(new_df.columns)
    conn.register("_atrs_tmp", new_df)
    conn.execute(f"INSERT INTO atrs_records ({cols}) SELECT {cols} FROM _atrs_tmp")
    conn.unregister("_atrs_tmp")

    logger.info("Inserted %d new ATRS records into Silver", len(new_df))
    return len(new_df)
