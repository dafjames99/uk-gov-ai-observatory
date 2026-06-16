"""Generic CSV → DuckDB table seeder for curated reference tables."""

import logging

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)


def seed_table_from_csv(
    csv_path: str,
    table: str,
    pk_col: str,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Insert rows from a CSV that aren't already present (keyed on pk_col).

    Column-explicit insert, so the CSV column order need not match the table.
    Idempotent — re-running inserts only genuinely new rows.

    Args:
        csv_path: Path to the CSV seed file. Its columns must be a subset of
            the table's columns and include pk_col.
        table: Target table name.
        pk_col: Primary-key column used to detect existing rows.
        conn: An open DuckDB connection.

    Returns:
        Number of rows inserted.
    """
    df = pd.read_csv(csv_path).drop_duplicates(subset=[pk_col])
    existing = {r[0] for r in conn.execute(f"SELECT {pk_col} FROM {table}").fetchall()}
    new_rows = df[~df[pk_col].isin(existing)]

    if new_rows.empty:
        logger.info("No new rows to seed into %s from %s", table, csv_path)
        return 0

    cols = ", ".join(new_rows.columns)
    conn.register("_seed_tmp", new_rows)
    conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _seed_tmp")
    conn.unregister("_seed_tmp")

    logger.info("Seeded %d rows into %s from %s", len(new_rows), table, csv_path)
    return len(new_rows)
