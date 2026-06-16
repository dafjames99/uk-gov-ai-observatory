"""Contracts Finder OCDS ingestion — fetch, parse, and upsert procurement notices."""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.common.http import RateLimitedSession
from src.ingest.ai_relevance import config_version, is_ai_relevant

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
_PAGE_SIZE = 100
_ALL_STAGES = "award,planning,tender,contract"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_releases(
    session: RateLimitedSession,
    from_date: date,
    to_date: date,
    stages: str = _ALL_STAGES,
) -> tuple[list[dict], list[dict]]:
    """Fetch all OCDS releases from Contracts Finder for the given date range.

    Paginates until the API returns an empty releases array.

    Args:
        session: A configured RateLimitedSession.
        from_date: Start of the published date range (inclusive).
        to_date: End of the published date range (inclusive).
        stages: Comma-separated stage filter string.

    Returns:
        Tuple of (all_releases, raw_pages) where raw_pages is a list of raw
        API response dicts suitable for Bronze storage.
    """
    # The API uses cursor-based pagination; the 'links.next' URL carries the
    # cursor token for the following page. We follow it directly rather than
    # incrementing a page counter, which would re-request the same results.
    initial_params = {
        "publishedFrom": f"{from_date}T00:00:00",
        "publishedTo": f"{to_date}T23:59:59",
        "stages": stages,
        "size": _PAGE_SIZE,
    }

    all_releases: list[dict] = []
    raw_pages: list[dict] = []
    next_url: str | None = None
    page = 1

    while True:
        logger.info("Fetching Contracts Finder page %d (%s → %s)", page, from_date, to_date)

        try:
            if next_url:
                data = session.get_json(next_url)
            else:
                data = session.get_json(_BASE_URL, params=initial_params)
        except Exception:
            logger.exception("Failed fetching page %d — stopping pagination", page)
            break

        raw_pages.append(data)
        releases = data.get("releases", [])

        if not releases:
            logger.info("Empty releases on page %d — pagination complete", page)
            break

        all_releases.extend(releases)
        logger.debug("Page %d: %d releases (total so far: %d)", page, len(releases), len(all_releases))

        next_url = (data.get("links") or {}).get("next")
        if not next_url:
            logger.info("No 'next' link on page %d — pagination complete", page)
            break

        page += 1

    return all_releases, raw_pages


def save_bronze(raw_pages: list[dict], run_date: date, bronze_root: Path) -> Path:
    """Write raw API response pages to the Bronze layer.

    Args:
        raw_pages: List of raw API response dicts.
        run_date: The date this run was executed (used for directory naming).
        bronze_root: Root directory for Bronze storage.

    Returns:
        Path to the directory where pages were written.
    """
    out_dir = bronze_root / "contracts_finder" / run_date.isoformat()
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


def _extract_cpv_codes(tender: dict) -> list[str]:
    codes: list[str] = []
    for item in tender.get("items", []):
        clf = item.get("classification", {})
        if clf.get("scheme", "").upper() == "CPV" and clf.get("id"):
            codes.append(str(clf["id"]))
    top = tender.get("classification", {})
    if top.get("scheme", "").upper() == "CPV" and top.get("id"):
        top_id = str(top["id"])
        if top_id not in codes:
            codes.insert(0, top_id)
    return codes


def _extract_value(release: dict) -> tuple[float | None, str]:
    for award in release.get("awards", []):
        v = award.get("value", {})
        if v.get("amount") is not None:
            return float(v["amount"]), v.get("currency", "GBP")
    tender = release.get("tender", {})
    v = tender.get("value", {})
    if v.get("amount") is not None:
        return float(v["amount"]), v.get("currency", "GBP")
    return None, "GBP"


def _extract_supplier(release: dict) -> tuple[str, str | None]:
    for award in release.get("awards", []):
        suppliers = award.get("suppliers", [])
        if suppliers:
            s = suppliers[0]
            name = s.get("name") or "supplier_unknown"
            sid = s.get("identifier", {}).get("id")
            return name, sid
    return "supplier_unknown", None


def _extract_dates(release: dict) -> tuple[str | None, str | None, str | None]:
    """Return (published_date, contract_start, contract_end) as ISO date strings."""
    published = release.get("date") or release.get("publishedDate")
    published_str = published[:10] if published else None

    start = end = None
    # Prefer contract period from award, then tender
    for award in release.get("awards", []):
        period = award.get("contractPeriod", {})
        if period:
            start = period.get("startDate", "")[:10] or None
            end = period.get("endDate", "")[:10] or None
            break
    if not start and not end:
        period = release.get("tender", {}).get("contractPeriod", {})
        start = period.get("startDate", "")[:10] or None
        end = period.get("endDate", "")[:10] or None

    # Flag anomalous dates instead of imputing
    if start and end and end < start:
        logger.warning(
            "Anomalous contract period (end < start) on %s: %s → %s — nulling both",
            release.get("ocid"),
            start,
            end,
        )
        start = end = None

    return published_str, start, end


def parse_release(release: dict) -> dict[str, Any] | None:
    """Parse a single OCDS release dict into a Silver-layer notice dict.

    Returns None if the release lacks the minimum required fields (ocid, id).

    Args:
        release: Raw OCDS release dict.

    Returns:
        Normalised notice dict, or None.
    """
    ocid = release.get("ocid")
    release_id = release.get("id")
    if not ocid or not release_id:
        logger.debug("Skipping release missing ocid/id: %s", release)
        return None

    tender = release.get("tender", {})
    buyer = release.get("buyer", {})

    title = tender.get("title") or release.get("planning", {}).get("project", {}).get("title")
    description = tender.get("description")
    cpv_codes = _extract_cpv_codes(tender)
    value_amount, currency = _extract_value(release)
    supplier_name, supplier_id = _extract_supplier(release)
    published_date, contract_start, contract_end = _extract_dates(release)

    tags = release.get("tag", [])
    stage = tags[0] if tags else release.get("stage")

    ai_rel = is_ai_relevant(title, description, cpv_codes)
    rel_version = config_version()

    return {
        "notice_id": f"{ocid}::{release_id}",
        "source": "contracts_finder",
        "stage": stage,
        "title": _force_utf8(title),
        "description": _force_utf8(description),
        "value_amount": value_amount,
        "currency": currency,
        "buyer_name": buyer.get("name"),
        "buyer_org_id": buyer.get("identifier", {}).get("id"),
        "supplier_name": supplier_name,
        "supplier_id": supplier_id,
        "cpv_codes": json.dumps(cpv_codes),
        "published_date": published_date,
        "contract_start": contract_start,
        "contract_end": contract_end,
        "ai_relevant": ai_rel,
        "ai_relevance_version": rel_version,
        "link_status": "ok",
        "source_url": f"https://www.contractsfinder.service.gov.uk/Notice/{ocid}",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def _force_utf8(value: str | None) -> str | None:
    if value is None:
        return None
    return value.encode("utf-8", errors="replace").decode("utf-8")


# ---------------------------------------------------------------------------
# Silver upsert
# ---------------------------------------------------------------------------


def upsert_notices(
    notices: list[dict[str, Any]],
    conn: duckdb.DuckDBPyConnection,
    ai_relevant_only: bool = True,
) -> int:
    """Upsert parsed notices into the Silver procurement_notices table.

    Only inserts new rows — existing notice_ids are skipped.

    Args:
        notices: List of normalised notice dicts from parse_release().
        conn: An open DuckDB connection.
        ai_relevant_only: If True (default), only persist AI-relevant notices.

    Returns:
        Number of new rows inserted.
    """
    if not notices:
        return 0

    df = pd.DataFrame(notices)

    if ai_relevant_only:
        df = df[df["ai_relevant"] == True]  # noqa: E712

    if df.empty:
        return 0

    existing = {
        r[0]
        for r in conn.execute("SELECT notice_id FROM procurement_notices").fetchall()
    }
    new_df = df[~df["notice_id"].isin(existing)].drop_duplicates(subset=["notice_id"])

    if new_df.empty:
        logger.info("No new notices to insert (all %d already present)", len(df))
        return 0

    # Insert by explicit column names (not positional SELECT *) so that columns
    # present in the table but not emitted by parse_release default to NULL.
    cols = ", ".join(new_df.columns)
    conn.register("_notices_tmp", new_df)
    conn.execute(
        f"INSERT INTO procurement_notices ({cols}) SELECT {cols} FROM _notices_tmp"
    )
    conn.unregister("_notices_tmp")

    logger.info("Inserted %d new notices into Silver", len(new_df))
    return len(new_df)
