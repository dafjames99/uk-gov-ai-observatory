"""Initialise the DuckDB warehouse and seed the org_aliases table.

Safe to re-run — all operations are idempotent.
"""

import logging
from pathlib import Path

from src.common.db import get_connection, init_schema
from src.common.org_aliases import seed_from_csv
from src.common.seeds import seed_table_from_csv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SEEDS_DIR = Path(__file__).parents[1] / "data" / "seeds"


def main() -> None:
    logger.info("Connecting to DuckDB...")
    conn = get_connection()

    logger.info("Initialising schema...")
    init_schema(conn)

    aliases_csv = SEEDS_DIR / "org_aliases.csv"
    logger.info("Seeding org_aliases from %s...", aliases_csv)
    inserted = seed_from_csv(str(aliases_csv), conn)
    logger.info("Done — %d new rows inserted into org_aliases.", inserted)

    total = conn.execute("SELECT COUNT(*) FROM org_aliases").fetchone()[0]
    logger.info("org_aliases total rows: %d", total)

    zones_csv = SEEDS_DIR / "ai_growth_zones.csv"
    logger.info("Seeding ai_growth_zones from %s...", zones_csv)
    zones_inserted = seed_table_from_csv(str(zones_csv), "ai_growth_zones", "zone_id", conn)
    zones_total = conn.execute("SELECT COUNT(*) FROM ai_growth_zones").fetchone()[0]
    logger.info("Done — %d new rows inserted; ai_growth_zones total: %d.", zones_inserted, zones_total)

    conn.close()
    logger.info("Database initialised successfully.")


if __name__ == "__main__":
    main()
