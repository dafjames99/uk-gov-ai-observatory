"""Export Silver tables and Gold views to Parquet for dashboard consumption.

Run after each ingest cycle. The exported files are committed to the repo
so the Streamlit dashboard can read them without needing a local DuckDB file.

Usage:
    uv run python scripts/export_gold.py
"""

import logging
import sys
from pathlib import Path

from src.common.db import get_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GOLD_DIR = Path(__file__).parents[1] / "data" / "gold"


def main() -> int:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_connection()

    exports = {
        "atrs_records.parquet": "SELECT * FROM atrs_records",
        "procurement_notices.parquet": "SELECT * FROM procurement_notices WHERE ai_relevant = TRUE",
        "v_reporting_gap.parquet": "SELECT * FROM v_reporting_gap",
        "v_spend_by_month.parquet": "SELECT * FROM v_spend_by_month",
    }

    for filename, query in exports.items():
        out_path = GOLD_DIR / filename
        conn.execute(f"COPY ({query}) TO '{out_path}' (FORMAT PARQUET)")
        row_count = conn.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
        logger.info("Exported %s — %d rows", filename, row_count)

    conn.close()
    logger.info("Gold export complete → %s", GOLD_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
