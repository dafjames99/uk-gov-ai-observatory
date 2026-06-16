"""Backfill historic procurement notices from OCP bulk OCDS archives.

Downloads per-year (or all-time) bulk archives for Contracts Finder and/or
Find a Tender and loads them into Silver. Use this for history; use
procurement_ingest.py for the incremental weekly tail.

Usage:
    # Default: both sources, 2021..current year (per design decision §10.1)
    uv run python -m scripts.procurement_backfill

    # One source, a year range
    uv run python -m scripts.procurement_backfill --source find_a_tender \
        --from-year 2023 --to-year 2024

    # All-time archive in a single file
    uv run python -m scripts.procurement_backfill --source contracts_finder --all-time

    # Load a previously downloaded archive without re-downloading
    uv run python -m scripts.procurement_backfill --archive data/bronze/cf_2023.jsonl.gz \
        --source contracts_finder
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.http import build_session
from src.ingest.procurement import SOURCES
from src.ingest.procurement_bulk import download_archive, load_archive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BRONZE_ROOT = Path(__file__).parents[1] / "data" / "bronze"
DEFAULT_FROM_YEAR = 2021  # FTS launch — consistent two-feed denominator (design §10.1)
SOURCE_NAMES = ("contracts_finder", "find_a_tender")  # SOURCES (the dict) is imported


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill procurement notices from bulk OCDS archives.")
    p.add_argument(
        "--source",
        choices=(*SOURCE_NAMES, "both"),
        default="both",
        help="Procurement source to backfill. Default: both.",
    )
    p.add_argument(
        "--from-year",
        type=int,
        default=DEFAULT_FROM_YEAR,
        help=f"First calendar year to load (inclusive). Default: {DEFAULT_FROM_YEAR}.",
    )
    p.add_argument(
        "--to-year",
        type=int,
        default=date.today().year,
        help="Last calendar year to load (inclusive). Default: current year.",
    )
    p.add_argument(
        "--all-time",
        action="store_true",
        help="Load the single all-time archive instead of per-year files.",
    )
    p.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Path to a local .jsonl[.gz] archive to load (skips download). "
        "Requires --source to be a single source.",
    )
    p.add_argument(
        "--keep-all",
        action="store_true",
        help="Persist all notices, not just AI-relevant ones.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Download/locate archives but do not write to the database.",
    )
    return p.parse_args()


def _sources(arg: str) -> tuple[str, ...]:
    return SOURCE_NAMES if arg == "both" else (arg,)


def main() -> int:
    args = parse_args()

    if args.archive and args.source == "both":
        logger.error("--archive requires a single --source, not 'both'.")
        return 2

    conn = None if args.dry_run else get_connection()
    if conn is not None:
        init_schema(conn)

    session = build_session(rate_limit_delay=1.0)
    ai_relevant_only = not args.keep_all
    total_inserted = 0

    # A local archive short-circuits download and year iteration.
    if args.archive:
        if args.dry_run:
            logger.info("Dry run — would load %s", args.archive)
            return 0
        total_inserted += load_archive(
            args.archive, conn, SOURCES[args.source], ai_relevant_only
        )
        logger.info("Backfill complete. Inserted %d new notices.", total_inserted)
        conn.close()
        return 0

    years: list[int | None] = [None] if args.all_time else list(range(args.from_year, args.to_year + 1))

    for source in _sources(args.source):
        bronze_dir = BRONZE_ROOT / f"{source}_bulk"
        for year in years:
            try:
                archive = download_archive(session, source, year, bronze_dir)
            except Exception:
                logger.exception("Failed to download %s %s — skipping", source, year or "full")
                continue

            if args.dry_run:
                logger.info("Dry run — downloaded but not loading %s", archive.name)
                continue

            total_inserted += load_archive(archive, conn, SOURCES[source], ai_relevant_only)

    if conn is not None:
        silver_total = conn.execute("SELECT COUNT(*) FROM procurement_notices").fetchone()[0]
        conn.close()
        logger.info(
            "Backfill complete. Inserted %d new notices. Silver total: %d.",
            total_inserted,
            silver_total,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
