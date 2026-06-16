"""Tests for the generic CSV seeder and the AI Growth Zones seed."""

from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.common.seeds import seed_table_from_csv

ZONES_CSV = Path(__file__).parents[1] / "data" / "seeds" / "ai_growth_zones.csv"


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_seed_growth_zones(conn):
    inserted = seed_table_from_csv(str(ZONES_CSV), "ai_growth_zones", "zone_id", conn)
    assert inserted > 0
    total = conn.execute("SELECT COUNT(*) FROM ai_growth_zones").fetchone()[0]
    assert total == inserted


def test_seed_growth_zones_idempotent(conn):
    seed_table_from_csv(str(ZONES_CSV), "ai_growth_zones", "zone_id", conn)
    assert seed_table_from_csv(str(ZONES_CSV), "ai_growth_zones", "zone_id", conn) == 0


def test_seed_growth_zones_typed_values(conn):
    seed_table_from_csv(str(ZONES_CSV), "ai_growth_zones", "zone_id", conn)
    row = conn.execute(
        "SELECT investment_gbp, announced_date, region FROM ai_growth_zones WHERE zone_id = 'lanarkshire'"
    ).fetchone()
    assert row[0] == 8_200_000_000  # parsed as a number
    assert str(row[1]) == "2026-01-29"  # parsed as a DATE
    assert row[2] == "Scotland"
    # Zones without a stated figure keep NULL rather than an imputed value.
    null_inv = conn.execute(
        "SELECT investment_gbp FROM ai_growth_zones WHERE zone_id = 'culham'"
    ).fetchone()[0]
    assert null_inv is None
