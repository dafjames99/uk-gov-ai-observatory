"""Smoke tests for schema initialisation and org_aliases seeding."""

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema, migrate
from src.common.org_aliases import resolve, seed_from_csv, unmatched_names


def _columns(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchall()
    }

SEEDS_CSV = Path(__file__).parents[1] / "data" / "seeds" / "org_aliases.csv"


@pytest.fixture
def conn():
    """In-memory DuckDB connection, fully initialised."""
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_silver_tables_exist(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert {"atrs_records", "procurement_notices", "org_aliases", "written_questions"} <= tables


def test_v2_axis_b_tables_exist(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert {"gov_announcements", "ai_growth_zones", "datacentre_planning"} <= tables


def test_procurement_notices_has_v2_columns(conn):
    cols = _columns(conn, "procurement_notices")
    assert {
        "documents",
        "awards",
        "framework_id",
        "procurement_method",
        "ai_confidence",
        "notice_summary",
        "enrichment_version",
    } <= cols


def test_migrate_adds_columns_to_legacy_table():
    """migrate() backfills columns onto an existing pre-v2 table shape."""
    c = get_connection(":memory:")
    # Simulate a v1 table that predates the additive columns.
    c.execute("""
        CREATE TABLE procurement_notices (
            notice_id VARCHAR PRIMARY KEY,
            title     VARCHAR
        )
    """)
    c.execute("CREATE TABLE written_questions (question_id VARCHAR PRIMARY KEY)")

    migrate(c)
    assert {"documents", "awards", "ai_confidence"} <= _columns(c, "procurement_notices")
    assert "enrichment_version" in _columns(c, "written_questions")

    # Idempotent — a second run is a no-op and does not raise.
    migrate(c)
    assert "ai_confidence" in _columns(c, "procurement_notices")
    c.close()


def test_init_schema_is_idempotent():
    """Re-running init_schema on the same DB does not raise or duplicate."""
    c = get_connection(":memory:")
    init_schema(c)
    init_schema(c)
    assert {"documents", "ai_confidence"} <= _columns(c, "procurement_notices")
    c.close()


def test_gold_views_exist(conn):
    views = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert {
        "v_reporting_gap",
        "v_spend_by_month",
        "v_wpq_trends",
        "v_announcement_trends",
        "v_capacity_overview",
    } <= views


def test_org_aliases_seed(conn):
    inserted = seed_from_csv(str(SEEDS_CSV), conn)
    assert inserted > 0

    # Idempotency — second seed inserts nothing
    inserted_again = seed_from_csv(str(SEEDS_CSV), conn)
    assert inserted_again == 0


def test_resolve_known_alias(conn):
    seed_from_csv(str(SEEDS_CSV), conn)
    assert resolve("DWP", conn) == "Department for Work and Pensions"
    assert resolve("dwp", conn) == "Department for Work and Pensions"  # case-insensitive
    assert resolve("HMRC", conn) == "HM Revenue & Customs"
    assert resolve("NHS England", conn) == "NHS England"


def test_resolve_unknown_returns_none(conn):
    seed_from_csv(str(SEEDS_CSV), conn)
    assert resolve("Totally Unknown Body", conn) is None


def test_unmatched_names(conn):
    seed_from_csv(str(SEEDS_CSV), conn)
    # Insert a notice with a buyer name not in org_aliases
    conn.execute("""
        INSERT INTO procurement_notices (notice_id, buyer_name, ai_relevant, ai_relevance_version)
        VALUES ('test-001', 'Unknown Agency XYZ', TRUE, '1.0')
    """)
    unmatched = unmatched_names("procurement_notices", "buyer_name", conn)
    assert "unknown agency xyz" in unmatched
