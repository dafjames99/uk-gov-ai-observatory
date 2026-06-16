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


def _export_procurement_by_year(conn) -> None:
    """Write AI-relevant procurement notices to data/gold/procurement_notices/.

    One Parquet per publication year (plus 'unknown.parquet' for rows with no
    published_date), so the dashboard can glob and concatenate without loading
    a single large file. The directory is cleared first so dropped years don't
    linger.
    """
    out_dir = GOLD_DIR / "procurement_notices"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.parquet"):
        stale.unlink()

    # v_procurement_dedup is already AI-relevant and de-duplicated.
    years = conn.execute(
        """
        SELECT DISTINCT CAST(EXTRACT(year FROM published_date) AS INT) AS yr
        FROM v_procurement_dedup
        WHERE published_date IS NOT NULL
        ORDER BY yr
        """
    ).fetchall()

    for (yr,) in years:
        out = out_dir / f"{yr}.parquet"
        conn.execute(
            f"""
            COPY (
                SELECT * FROM v_procurement_dedup
                WHERE EXTRACT(year FROM published_date) = {yr}
            ) TO '{out}' (FORMAT PARQUET)
            """
        )
        rows = conn.execute(
            f"SELECT COUNT(*) FROM v_procurement_dedup WHERE EXTRACT(year FROM published_date) = {yr}"
        ).fetchone()[0]
        logger.info("Exported procurement_notices/%d.parquet — %d rows", yr, rows)

    # Rows with no usable date still belong somewhere.
    unknown = out_dir / "unknown.parquet"
    n_unknown = conn.execute(
        "SELECT COUNT(*) FROM v_procurement_dedup WHERE published_date IS NULL"
    ).fetchone()[0]
    if n_unknown:
        conn.execute(
            f"""
            COPY (
                SELECT * FROM v_procurement_dedup WHERE published_date IS NULL
            ) TO '{unknown}' (FORMAT PARQUET)
            """
        )
        logger.info("Exported procurement_notices/unknown.parquet — %d rows", n_unknown)


def main() -> int:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_connection()

    # Procurement is the largest table; partition it by year so each Parquet
    # stays small and the dashboard can load years independently.
    _export_procurement_by_year(conn)

    exports = {
        # Axis A — usage
        "atrs_records.parquet": "SELECT * FROM atrs_records",
        "v_reporting_gap.parquet": "SELECT * FROM v_reporting_gap",
        "v_spend_by_month.parquet": "SELECT * FROM v_spend_by_month",
        # Axis B — intent & capacity
        "gov_announcements.parquet": "SELECT * FROM gov_announcements WHERE ai_relevant = TRUE",
        "written_questions.parquet": "SELECT * FROM written_questions WHERE ai_relevance_flag = TRUE",
        "ai_growth_zones.parquet": "SELECT * FROM ai_growth_zones",
        "v_announcement_trends.parquet": "SELECT * FROM v_announcement_trends",
        "v_wpq_trends.parquet": "SELECT * FROM v_wpq_trends",
        "v_capacity_overview.parquet": "SELECT * FROM v_capacity_overview",
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
