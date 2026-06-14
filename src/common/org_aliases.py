"""org_aliases lookup — resolves raw organisation name variants to canonical names."""

import logging

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)


def resolve(raw_name: str, conn: duckdb.DuckDBPyConnection) -> str | None:
    """Look up a raw organisation name and return its canonical form.

    Matching is case-insensitive and strips leading/trailing whitespace.

    Args:
        raw_name: The raw name as it appears in a source record.
        conn: An open DuckDB connection.

    Returns:
        The canonical name, or None if no alias is found.
    """
    result = conn.execute(
        "SELECT canonical_name FROM org_aliases WHERE lower(trim(raw_name)) = lower(trim(?))",
        [raw_name],
    ).fetchone()
    if result is None:
        logger.debug("No alias found for %r", raw_name)
        return None
    return result[0]


def seed_from_csv(csv_path: str, conn: duckdb.DuckDBPyConnection) -> int:
    """Load org_aliases from a CSV file, inserting rows that don't already exist.

    The CSV must have columns: raw_name, canonical_name, org_type.

    Args:
        csv_path: Absolute or relative path to the CSV seed file.
        conn: An open DuckDB connection.

    Returns:
        Number of rows inserted.
    """
    df = pd.read_csv(csv_path)
    existing = {r[0] for r in conn.execute("SELECT raw_name FROM org_aliases").fetchall()}
    new_rows = df[~df["raw_name"].isin(existing)]
    if not new_rows.empty:
        conn.register("_seed_tmp", new_rows)
        conn.execute("INSERT INTO org_aliases SELECT * FROM _seed_tmp")
        conn.unregister("_seed_tmp")
    inserted = len(new_rows)
    logger.info("Seeded %d org_aliases rows from %s", inserted, csv_path)
    return inserted


def unmatched_names(
    table: str,
    name_col: str,
    conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """Return distinct raw names in a Silver table that have no alias entry.

    Useful for iteratively expanding the alias seed.

    Args:
        table: Silver table name (e.g. 'procurement_notices').
        name_col: Column containing organisation names (e.g. 'buyer_name').
        conn: An open DuckDB connection.

    Returns:
        Sorted list of unmatched raw name strings.
    """
    rows = conn.execute(f"""
        SELECT DISTINCT lower(trim({name_col})) AS raw
        FROM {table}
        WHERE lower(trim({name_col})) NOT IN (
            SELECT lower(trim(raw_name)) FROM org_aliases
        )
        ORDER BY raw
    """).fetchall()
    return [r[0] for r in rows]
