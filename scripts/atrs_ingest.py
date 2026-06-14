"""Fetch all ATRS records from GOV.UK and load into Silver.

Fetches the full listing via the GOV.UK Search API, then retrieves each
record's structured data from the Content API. All raw responses are saved
to Bronze before any Silver writes.

Usage:
    uv run python scripts/atrs_ingest.py
    uv run python scripts/atrs_ingest.py --dry-run
    uv run python scripts/atrs_ingest.py --skip-bronze
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.http import build_session
from src.ingest.atrs import (
    fetch_all_slugs,
    fetch_record,
    parse_record,
    save_bronze,
    upsert_records,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BRONZE_ROOT = Path(__file__).parents[1] / "data" / "bronze"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest ATRS records from GOV.UK into Silver.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but do not write to the database.",
    )
    p.add_argument(
        "--skip-bronze",
        action="store_true",
        help="Do not write raw responses to the Bronze layer.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N records (useful for testing).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    session = build_session(rate_limit_delay=0.5)

    # --- Discover all record slugs ---
    logger.info("Fetching ATRS record listing...")
    listing_pages: list[dict] = []
    slugs: list[str] = []
    start = 0
    page_size = 100

    while True:
        params = {
            "filter_document_type": "algorithmic_transparency_record",
            "count": page_size,
            "start": start,
            "fields": "link,title,public_timestamp",
        }
        data = session.get_json("https://www.gov.uk/api/search.json", params=params)
        listing_pages.append(data)
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

    logger.info("Found %d ATRS records", len(slugs))

    if args.limit:
        slugs = slugs[: args.limit]
        logger.info("--limit applied: processing %d records", len(slugs))

    # --- Fetch individual records from Content API ---
    record_responses: dict[str, dict] = {}
    fetch_errors = 0

    for i, slug in enumerate(slugs, start=1):
        try:
            raw = fetch_record(session, slug)
            record_responses[slug] = raw
            if i % 10 == 0 or i == len(slugs):
                logger.info("Fetched %d / %d records", i, len(slugs))
        except Exception:
            fetch_errors += 1
            logger.warning("Failed to fetch %s", slug, exc_info=True)

    logger.info(
        "Fetched %d records (%d errors)", len(record_responses), fetch_errors
    )

    # --- Bronze ---
    if not args.skip_bronze and record_responses:
        save_bronze(listing_pages, record_responses, run_date=date.today(), bronze_root=BRONZE_ROOT)

    # --- Parse ---
    records = []
    parse_errors = 0
    for slug, raw in record_responses.items():
        try:
            record = parse_record(raw)
            if record:
                records.append(record)
        except Exception:
            parse_errors += 1
            logger.warning("Failed to parse %s", slug, exc_info=True)

    logger.info("Parsed %d records (%d parse errors)", len(records), parse_errors)

    if args.dry_run:
        logger.info("Dry run — skipping database write.")
        for r in records[:5]:
            logger.info(
                "  [%s] %s | %s | %s",
                r["standard_version"],
                r["organisation_name"],
                r["phase"],
                r["one_sentence_desc"][:80] if r["one_sentence_desc"] else "—",
            )
        return 0

    # --- Silver upsert ---
    conn = get_connection()
    init_schema(conn)

    inserted = upsert_records(records, conn)
    total_silver = conn.execute("SELECT COUNT(*) FROM atrs_records").fetchone()[0]

    conn.close()

    logger.info(
        "Done. Inserted %d new ATRS records. Silver total: %d.",
        inserted,
        total_silver,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
