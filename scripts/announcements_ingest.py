"""Fetch AI-related GOV.UK announcements and load into Silver (Axis B intent).

Usage:
    uv run python -m scripts.announcements_ingest
    uv run python -m scripts.announcements_ingest --org department-for-science-innovation-and-technology
    uv run python -m scripts.announcements_ingest --dry-run
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.http import build_session
from src.ingest.announcements import (
    fetch_announcements,
    parse_announcement,
    save_bronze,
    upsert_announcements,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BRONZE_ROOT = Path(__file__).parents[1] / "data" / "bronze"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest AI-related GOV.UK announcements.")
    p.add_argument(
        "--org",
        action="append",
        dest="orgs",
        help="Restrict to an organisation slug (repeatable).",
    )
    p.add_argument("--keep-all", action="store_true", help="Persist all results, not just AI-relevant.")
    p.add_argument("--dry-run", action="store_true", help="Fetch and parse but do not write to the DB.")
    p.add_argument("--skip-bronze", action="store_true", help="Do not write raw responses to Bronze.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    session = build_session(rate_limit_delay=1.0)
    results, raw_pages = fetch_announcements(session, organisations=args.orgs)

    if not args.skip_bronze and raw_pages:
        save_bronze(raw_pages, run_date=date.today(), bronze_root=BRONZE_ROOT)

    records = []
    for raw in results:
        rec = parse_announcement(raw)
        if rec:
            records.append(rec)

    relevant = sum(1 for r in records if r["ai_relevant"])
    logger.info("Parsed %d announcements (%d AI-relevant)", len(records), relevant)

    if args.dry_run:
        logger.info("Dry run — skipping database write.")
        return 0

    conn = get_connection()
    init_schema(conn)
    inserted = upsert_announcements(records, conn, ai_relevant_only=not args.keep_all)
    total = conn.execute("SELECT COUNT(*) FROM gov_announcements").fetchone()[0]
    conn.close()

    logger.info("Done. Inserted %d new announcements. Silver total: %d.", inserted, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
