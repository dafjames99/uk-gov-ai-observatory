"""Fetch AI-related Written Parliamentary Questions and load into Silver.

Usage:
    uv run python -m scripts.wpq_ingest
    uv run python -m scripts.wpq_ingest --dry-run
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.http import build_session
from src.ingest.written_questions import (
    fetch_questions,
    parse_question,
    save_bronze,
    upsert_questions,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BRONZE_ROOT = Path(__file__).parents[1] / "data" / "bronze"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest AI-related Written Parliamentary Questions.")
    p.add_argument("--keep-all", action="store_true", help="Persist all results, not just AI-relevant.")
    p.add_argument("--dry-run", action="store_true", help="Fetch and parse but do not write to the DB.")
    p.add_argument("--skip-bronze", action="store_true", help="Do not write raw responses to Bronze.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    session = build_session(rate_limit_delay=1.0)
    values, raw_pages = fetch_questions(session)

    if not args.skip_bronze and raw_pages:
        save_bronze(raw_pages, run_date=date.today(), bronze_root=BRONZE_ROOT)

    records = [r for v in values if (r := parse_question(v))]
    relevant = sum(1 for r in records if r["ai_relevance_flag"])
    logger.info("Parsed %d questions (%d AI-relevant)", len(records), relevant)

    if args.dry_run:
        logger.info("Dry run — skipping database write.")
        return 0

    conn = get_connection()
    init_schema(conn)
    inserted = upsert_questions(records, conn, ai_relevant_only=not args.keep_all)
    total = conn.execute("SELECT COUNT(*) FROM written_questions").fetchone()[0]
    conn.close()

    logger.info("Done. Inserted %d new questions. Silver total: %d.", inserted, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
