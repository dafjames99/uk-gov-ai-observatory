"""DuckDB connection helper and Silver/Gold schema initialisation."""

import os
from pathlib import Path

import duckdb
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DB_PATH = Path(__file__).parents[2] / "data" / "observatory.duckdb"


def get_connection(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection, creating the DB file if necessary.

    Args:
        db_path: Path to the .duckdb file. Defaults to data/observatory.duckdb.

    Returns:
        An open DuckDB connection.
    """
    path = Path(db_path) if db_path else Path(os.getenv("DB_PATH", str(_DEFAULT_DB_PATH)))
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create Silver tables and Gold views if they don't already exist.

    Args:
        conn: An open DuckDB connection.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atrs_records (
            record_id            VARCHAR PRIMARY KEY,
            organisation_name    VARCHAR,
            phase                VARCHAR,
            one_sentence_desc    VARCHAR,
            model_architecture   VARCHAR,
            sensitive_attributes VARCHAR,
            dpi_assessment_url   VARCHAR,
            date_published       DATE,
            standard_version     VARCHAR,
            source_url           VARCHAR,
            ingested_at          TIMESTAMPTZ
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS procurement_notices (
            notice_id             VARCHAR PRIMARY KEY,
            source                VARCHAR,
            stage                 VARCHAR,
            title                 VARCHAR,
            description           VARCHAR,
            value_amount          DOUBLE,
            currency              VARCHAR,
            buyer_name            VARCHAR,
            buyer_org_id          VARCHAR,
            supplier_name         VARCHAR,
            supplier_id           VARCHAR,
            cpv_codes             JSON,
            published_date        DATE,
            contract_start        DATE,
            contract_end          DATE,
            ai_relevant           BOOLEAN,
            ai_relevance_version  VARCHAR,
            link_status           VARCHAR,
            source_url            VARCHAR,
            ingested_at           TIMESTAMPTZ
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS org_aliases (
            raw_name       VARCHAR PRIMARY KEY,
            canonical_name VARCHAR NOT NULL,
            org_type       VARCHAR
        )
    """)

    # Phase 2 — created now so the schema is forward-compatible
    conn.execute("""
        CREATE TABLE IF NOT EXISTS written_questions (
            question_id       VARCHAR PRIMARY KEY,
            house             VARCHAR,
            date_tabled       DATE,
            date_answered     DATE,
            member_name       VARCHAR,
            department        VARCHAR,
            question_text     VARCHAR,
            answer_text       VARCHAR,
            ai_relevance_flag BOOLEAN,
            topic_tags        JSON,
            source_url        VARCHAR,
            ingested_at       TIMESTAMPTZ
        )
    """)

    _init_gold_views(conn)


def _init_gold_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Create Gold layer views over Silver tables.

    Args:
        conn: An open DuckDB connection.
    """
    conn.execute("""
        CREATE OR REPLACE VIEW v_reporting_gap AS
        WITH procurement_canonical AS (
            SELECT oa.canonical_name, pn.notice_id
            FROM procurement_notices pn
            JOIN org_aliases oa
                ON lower(trim(pn.buyer_name)) = lower(trim(oa.raw_name))
            WHERE pn.ai_relevant = TRUE
        ),
        atrs_canonical AS (
            SELECT oa.canonical_name, ar.record_id
            FROM atrs_records ar
            JOIN org_aliases oa
                ON lower(trim(ar.organisation_name)) = lower(trim(oa.raw_name))
        )
        SELECT
            oa.canonical_name,
            COUNT(DISTINCT pc.notice_id)  AS ai_procurement_count,
            COUNT(DISTINCT ac.record_id)  AS atrs_record_count
        FROM (SELECT DISTINCT canonical_name FROM org_aliases) oa
        LEFT JOIN procurement_canonical pc ON pc.canonical_name = oa.canonical_name
        LEFT JOIN atrs_canonical        ac ON ac.canonical_name = oa.canonical_name
        GROUP BY oa.canonical_name
        ORDER BY ai_procurement_count DESC
    """)

    conn.execute("""
        CREATE OR REPLACE VIEW v_spend_by_month AS
        SELECT
            oa.canonical_name,
            date_trunc('month', pn.published_date) AS month,
            SUM(pn.value_amount)                   AS total_value,
            pn.currency,
            COUNT(*)                               AS notice_count
        FROM procurement_notices pn
        JOIN org_aliases oa
            ON lower(trim(pn.buyer_name)) = lower(trim(oa.raw_name))
        WHERE pn.ai_relevant = TRUE
        GROUP BY oa.canonical_name, month, pn.currency
        ORDER BY month DESC, total_value DESC
    """)

    # Phase 2 view — safe to create now as written_questions table exists
    conn.execute("""
        CREATE OR REPLACE VIEW v_wpq_trends AS
        SELECT
            oa.canonical_name,
            date_trunc('month', wq.date_tabled) AS month,
            COUNT(*)                            AS question_count
        FROM written_questions wq
        JOIN org_aliases oa
            ON lower(trim(wq.department)) = lower(trim(oa.raw_name))
        WHERE wq.ai_relevance_flag = TRUE
        GROUP BY oa.canonical_name, month
        ORDER BY month DESC, question_count DESC
    """)
