"""Contracts Finder OCDS ingestion — fetch, parse, and upsert procurement notices."""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.common.http import RateLimitedSession
from src.ingest.ai_relevance import classify, config_version

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


@dataclass(frozen=True)
class Source:
    """Configuration for an OCDS procurement source.

    Contracts Finder and Find a Tender share the OCDS release shape and a
    'links.next' cursor pagination model; only the endpoint, date-filter
    parameter names, page-size parameter and public notice URL differ.
    """

    name: str
    base_url: str
    from_param: str
    to_param: str
    size_param: str
    notice_url_template: str
    default_stages: str | None  # None → omit the param (source returns all stages)


CONTRACTS_FINDER = Source(
    name="contracts_finder",
    base_url="https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
    from_param="publishedFrom",
    to_param="publishedTo",
    size_param="size",
    notice_url_template="https://www.contractsfinder.service.gov.uk/Notice/{ocid}",
    default_stages="award,planning,tender,contract",
)

FIND_A_TENDER = Source(
    name="find_a_tender",
    base_url="https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
    from_param="updatedFrom",
    to_param="updatedTo",
    size_param="limit",
    notice_url_template="https://www.find-tender.service.gov.uk/Notice/{release_id}",
    # FTS rejects a comma-separated stages list (returns nothing); omit it and
    # take all stages rather than making one request per stage.
    default_stages=None,
)

SOURCES: dict[str, Source] = {s.name: s for s in (CONTRACTS_FINDER, FIND_A_TENDER)}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_releases(
    session: RateLimitedSession,
    from_date: date,
    to_date: date,
    source: Source = CONTRACTS_FINDER,
    stages: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch all OCDS releases from a procurement source for a date range.

    Paginates until the API returns an empty releases array. Find a Tender
    filters on the *updated* timestamp; Contracts Finder on *published* —
    handled via the source's from/to parameter names.

    Args:
        session: A configured RateLimitedSession.
        from_date: Start of the date range (inclusive).
        to_date: End of the date range (inclusive).
        source: Which procurement source to query.
        stages: Comma-separated stage filter; defaults to the source's stages.

    Returns:
        Tuple of (all_releases, raw_pages) where raw_pages is a list of raw
        API response dicts suitable for Bronze storage.
    """
    # Both sources use cursor-based pagination; the 'links.next' URL carries the
    # cursor token for the following page. We follow it directly rather than
    # incrementing a page counter, which would re-request the same results.
    initial_params = {
        source.from_param: f"{from_date}T00:00:00",
        source.to_param: f"{to_date}T23:59:59",
        source.size_param: _PAGE_SIZE,
    }
    effective_stages = stages or source.default_stages
    if effective_stages:
        initial_params["stages"] = effective_stages

    all_releases: list[dict] = []
    raw_pages: list[dict] = []
    next_url: str | None = None
    page = 1

    while True:
        logger.info("Fetching %s page %d (%s → %s)", source.name, page, from_date, to_date)

        try:
            if next_url:
                data = session.get_json(next_url)
            else:
                data = session.get_json(source.base_url, params=initial_params)
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


def save_bronze(
    raw_pages: list[dict],
    run_date: date,
    bronze_root: Path,
    source_name: str = "contracts_finder",
) -> Path:
    """Write raw API response pages to the Bronze layer.

    Args:
        raw_pages: List of raw API response dicts.
        run_date: The date this run was executed (used for directory naming).
        bronze_root: Root directory for Bronze storage.
        source_name: Source key, used as the Bronze sub-directory.

    Returns:
        Path to the directory where pages were written.
    """
    out_dir = bronze_root / source_name / run_date.isoformat()
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


def _extract_documents(release: dict) -> list[dict[str, str | None]]:
    """Collect document links from the tender and any awards.

    OCDS documents carry the real detail behind thin notice descriptions
    (tender packs, specifications, award letters). Deduplicated on URL.

    Returns:
        List of {title, url, documentType} dicts.
    """
    docs: list[dict[str, str | None]] = []
    seen: set[str] = set()

    def _add(d: dict) -> None:
        url = d.get("url")
        if not url or url in seen:
            return
        seen.add(url)
        docs.append(
            {
                "title": d.get("title") or d.get("description"),
                "url": url,
                "documentType": d.get("documentType"),
            }
        )

    for d in release.get("tender", {}).get("documents", []) or []:
        _add(d)
    for award in release.get("awards", []) or []:
        for d in award.get("documents", []) or []:
            _add(d)
    return docs


def _extract_framework_id(release: dict) -> str | None:
    """Return the framework identifier if this notice is a call-off.

    CF/FTS OCDS does not populate tender.techniques reliably; the framework
    link lives in relatedProcesses with a 'framework' relationship.
    """
    for rp in release.get("relatedProcesses", []) or []:
        rel = rp.get("relationship") or []
        if isinstance(rel, str):
            rel = [rel]
        if any("framework" in str(r).lower() for r in rel):
            return rp.get("identifier") or rp.get("id")
    return None


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


def parse_release(
    release: dict, source: Source = CONTRACTS_FINDER
) -> dict[str, Any] | None:
    """Parse a single OCDS release dict into a Silver-layer notice dict.

    Returns None if the release lacks the minimum required fields (ocid, id).

    Args:
        release: Raw OCDS release dict.
        source: The procurement source the release came from (sets the
            `source` field and public notice URL).

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
    documents = _extract_documents(release)
    awards = release.get("awards") or []
    framework_id = _extract_framework_id(release)
    procurement_method = tender.get("procurementMethod")

    tags = release.get("tag", [])
    stage = tags[0] if tags else release.get("stage")

    ai_confidence = classify(title, description, cpv_codes)
    ai_rel = ai_confidence != "none"
    rel_version = config_version()

    return {
        "notice_id": f"{ocid}::{release_id}",
        "source": source.name,
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
        "documents": json.dumps(documents) if documents else None,
        "awards": json.dumps(awards) if awards else None,
        "framework_id": framework_id,
        "procurement_method": procurement_method,
        "published_date": published_date,
        "contract_start": contract_start,
        "contract_end": contract_end,
        "ai_relevant": ai_rel,
        "ai_confidence": ai_confidence,
        "ai_relevance_version": rel_version,
        "link_status": "ok",
        "source_url": source.notice_url_template.format(ocid=ocid, release_id=release_id),
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
