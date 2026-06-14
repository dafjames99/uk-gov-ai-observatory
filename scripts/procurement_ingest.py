"""Fetch AI-relevant procurement notices from Contracts Finder and load into Silver.

Usage:
    uv run python scripts/procurement_ingest.py
    uv run python scripts/procurement_ingest.py --from-date 2025-01-01 --to-date 2025-06-01
    uv run python scripts/procurement_ingest.py --dry-run
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.http import build_session
from src.ingest.procurement import fetch_releases, parse_release, save_bronze, upsert_notices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BRONZE_ROOT = Path(__file__).parents[1] / "data" / "bronze"
DEFAULT_LOOKBACK_DAYS = 7


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest Contracts Finder notices into Silver.")
    p.add_argument(
        "--from-date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS),
        help="Start of published date range (YYYY-MM-DD). Default: 7 days ago.",
    )
    p.add_argument(
        "--to-date",
        type=date.fromisoformat,
        default=date.today(),
        help="End of published date range (YYYY-MM-DD). Default: today.",
    )
    p.add_argument(
        "--stages",
        default="award,planning,tender,contract",
        help="Comma-separated OCDS stage filter.",
    )
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
    return p.parse_args()


def main() -> int:
    args = parse_args()

    logger.info(
        "Starting Contracts Finder ingest: %s → %s (stages: %s)",
        args.from_date,
        args.to_date,
        args.stages,
    )

    session = build_session(rate_limit_delay=2.0)

    releases, raw_pages = fetch_releases(
        session,
        from_date=args.from_date,
        to_date=args.to_date,
        stages=args.stages,
    )

    logger.info("Fetched %d total releases across %d pages", len(releases), len(raw_pages))

    if not args.skip_bronze and raw_pages:
        save_bronze(raw_pages, run_date=date.today(), bronze_root=BRONZE_ROOT)

    notices = []
    parse_errors = 0
    for release in releases:
        try:
            notice = parse_release(release)
            if notice:
                notices.append(notice)
        except Exception:
            parse_errors += 1
            logger.warning("Failed to parse release %s", release.get("ocid"), exc_info=True)

    ai_relevant_count = sum(1 for n in notices if n["ai_relevant"])
    logger.info(
        "Parsed %d notices (%d AI-relevant, %d parse errors)",
        len(notices),
        ai_relevant_count,
        parse_errors,
    )

    if args.dry_run:
        logger.info("Dry run — skipping database write.")
        return 0

    conn = get_connection()
    init_schema(conn)

    inserted = upsert_notices(notices, conn)
    total_silver = conn.execute(
        "SELECT COUNT(*) FROM procurement_notices"
    ).fetchone()[0]

    conn.close()

    logger.info(
        "Done. Inserted %d new notices. Silver total: %d.",
        inserted,
        total_silver,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
