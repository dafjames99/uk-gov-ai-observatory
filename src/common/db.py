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

    # v2 — Axis B (intent & capacity)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gov_announcements (
            announcement_id      VARCHAR PRIMARY KEY,
            title                VARCHAR,
            document_type        VARCHAR,
            organisations        JSON,
            public_timestamp     TIMESTAMPTZ,
            updated_timestamp    TIMESTAMPTZ,
            summary              VARCHAR,
            body_excerpt         VARCHAR,
            ai_relevant          BOOLEAN,
            ai_confidence        VARCHAR,
            ai_relevance_version VARCHAR,
            topic_tags           JSON,
            enrichment_version   VARCHAR,
            source_url           VARCHAR,
            ingested_at          TIMESTAMPTZ
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_growth_zones (
            zone_id          VARCHAR PRIMARY KEY,
            zone_name        VARCHAR,
            site             VARCHAR,
            region           VARCHAR,
            status           VARCHAR,
            investment_gbp   DOUBLE,
            compute_capacity VARCHAR,
            announced_date   DATE,
            lead_org         VARCHAR,
            source_url       VARCHAR,
            notes            VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS datacentre_planning (
            application_id   VARCHAR PRIMARY KEY,
            site_name        VARCHAR,
            local_authority  VARCHAR,
            description      VARCHAR,
            status           VARCHAR,
            decision_date    DATE,
            latitude         DOUBLE,
            longitude        DOUBLE,
            dc_relevant      BOOLEAN,
            source_url       VARCHAR,
            ingested_at      TIMESTAMPTZ
        )
    """)

    migrate(conn)
    _init_gold_views(conn)


# Additive column migrations. Keyed by table; each entry is (column, type).
# DuckDB's CREATE TABLE IF NOT EXISTS never alters an existing table, so columns
# added after a table first shipped are applied here with ADD COLUMN IF NOT EXISTS.
# Idempotent — safe to run on a fresh DB or an existing one.
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "procurement_notices": [
        ("documents", "JSON"),
        ("awards", "JSON"),
        ("framework_id", "VARCHAR"),
        ("procurement_method", "VARCHAR"),
        ("ai_confidence", "VARCHAR"),
        ("notice_summary", "VARCHAR"),
        ("enrichment_version", "VARCHAR"),
    ],
    "written_questions": [
        ("enrichment_version", "VARCHAR"),
    ],
}


def migrate(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply additive column migrations to existing Silver tables.

    Adds any columns introduced after a table first shipped, using
    ADD COLUMN IF NOT EXISTS so it is idempotent on both fresh and existing
    databases. New tables belong in init_schema(); only column additions to
    already-shipped tables belong here.

    Args:
        conn: An open DuckDB connection.
    """
    for table, columns in _COLUMN_MIGRATIONS.items():
        for name, col_type in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {col_type}"
            )


def _init_gold_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Create Gold layer views over Silver tables.

    Args:
        conn: An open DuckDB connection.
    """
    # Canonical de-duplicated procurement. The same contract is published many
    # times — across stages (tender vs award), across publication dates, and
    # across both Contracts Finder and Find a Tender (different ocid namespaces,
    # so no shared key). Collapse on buyer + value + title.
    #
    # Key design notes:
    #   - NO supplier in the key. For frameworks the value is the shared ceiling
    #     and the suppliers live inside one notice's awards[] array; the same
    #     framework appears as separate notices only because of stage/source, not
    #     because of distinct suppliers. Keying on supplier wrongly kept the
    #     tender stage (supplier_unknown) apart from the award stage, double-
    #     counting the framework value.
    #   - Title is normalised to alphanumerics and truncated to 50 chars, so
    #     spacing/punctuation variants ("COMIT4" vs "ComIT 4") and Find a Tender's
    #     title truncation both collapse.
    #   - Representative = the richest row: prefer one with awards (the full
    #     supplier list), then a named supplier, then a value, then most recent.
    # This is the analytical source of truth — spend/gap views and the Gold
    # export all read it.
    conn.execute(r"""
        CREATE OR REPLACE VIEW v_procurement_dedup AS
        SELECT * EXCLUDE (_rn) FROM (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        regexp_replace(lower(trim(buyer_name)), '[^a-z0-9]', '', 'g'),
                        COALESCE(CAST(value_amount AS VARCHAR), 'na'),
                        LEFT(regexp_replace(lower(trim(title)), '[^a-z0-9]', '', 'g'), 50)
                    ORDER BY
                        (CASE WHEN awards IS NOT NULL THEN 0 ELSE 1 END),
                        (CASE WHEN supplier_name IS NOT NULL
                              AND supplier_name <> 'supplier_unknown' THEN 0 ELSE 1 END),
                        (CASE WHEN value_amount IS NOT NULL THEN 0 ELSE 1 END),
                        published_date DESC NULLS LAST,
                        source
                ) AS _rn
            FROM procurement_notices
            WHERE ai_relevant = TRUE
        ) WHERE _rn = 1
    """)

    conn.execute("""
        CREATE OR REPLACE VIEW v_reporting_gap AS
        WITH procurement_canonical AS (
            SELECT oa.canonical_name, pn.notice_id
            FROM v_procurement_dedup pn
            JOIN org_aliases oa
                ON lower(trim(pn.buyer_name)) = lower(trim(oa.raw_name))
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
        FROM v_procurement_dedup pn
        JOIN org_aliases oa
            ON lower(trim(pn.buyer_name)) = lower(trim(oa.raw_name))
        GROUP BY oa.canonical_name, month, pn.currency
        ORDER BY month DESC, total_value DESC
    """)

    # WPQ scrutiny trends (Axis B). The Questions & Statements API already
    # returns a clean answering-body name, so group on it directly rather than
    # depending on org_aliases coverage.
    conn.execute("""
        CREATE OR REPLACE VIEW v_wpq_trends AS
        SELECT
            department,
            date_trunc('month', date_tabled) AS month,
            COUNT(*)                          AS question_count
        FROM written_questions
        WHERE ai_relevance_flag = TRUE
        GROUP BY department, month
        ORDER BY month DESC, question_count DESC
    """)

    # Announcement (intent) trends — AI announcements by month and document type.
    conn.execute("""
        CREATE OR REPLACE VIEW v_announcement_trends AS
        SELECT
            date_trunc('month', public_timestamp) AS month,
            document_type,
            COUNT(*)                              AS announcement_count
        FROM gov_announcements
        WHERE ai_relevant = TRUE
        GROUP BY month, document_type
        ORDER BY month DESC, announcement_count DESC
    """)

    # Capacity overview — the curated AI Growth Zones register, ordered by
    # announcement date for the Capacity lens.
    conn.execute("""
        CREATE OR REPLACE VIEW v_capacity_overview AS
        SELECT
            zone_id,
            zone_name,
            region,
            status,
            investment_gbp,
            compute_capacity,
            announced_date,
            lead_org,
            source_url
        FROM ai_growth_zones
        ORDER BY announced_date
    """)
