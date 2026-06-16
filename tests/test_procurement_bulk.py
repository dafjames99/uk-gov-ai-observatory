"""Tests for the bulk OCDS backfill loader."""

import gzip
import json
from pathlib import Path

import pytest

from src.common.db import get_connection, init_schema
from src.ingest.procurement_bulk import (
    archive_url,
    iter_releases_from_jsonl,
    load_archive,
    _normalise_to_releases,
)


def _ai_release(ocid: str, release_id: str = "r1") -> dict:
    """A minimal AI-relevant release (keyword in title)."""
    return {
        "ocid": ocid,
        "id": release_id,
        "tender": {
            "title": "Machine learning platform for fraud detection",
            "description": "Supply of an ML system.",
        },
        "buyer": {"name": "Test Department"},
        "awards": [{"value": {"amount": 100000, "currency": "GBP"}}],
        "date": "2023-05-01T00:00:00Z",
    }


def _plain_release(ocid: str) -> dict:
    """A non-AI release (no keyword, no AI CPV)."""
    return {
        "ocid": ocid,
        "id": "r1",
        "tender": {"title": "Office cleaning services", "description": "Daily cleaning."},
        "buyer": {"name": "Test Department"},
        "date": "2023-05-01T00:00:00Z",
    }


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_schema(c)
    yield c
    c.close()


# ── archive_url ────────────────────────────────────────────────────────────


def test_archive_url_year():
    url = archive_url("contracts_finder", 2023)
    assert "publication/128/download" in url
    assert "name=2023.jsonl.gz" in url


def test_archive_url_all_time():
    assert "name=full.jsonl.gz" in archive_url("find_a_tender", None)
    assert "publication/41/" in archive_url("find_a_tender", None)


def test_archive_url_unknown_source():
    with pytest.raises(KeyError):
        archive_url("nope", 2023)


# ── _normalise_to_releases — every supported line shape ─────────────────────


def test_normalise_release_package():
    pkg = {"uri": "x", "releases": [_ai_release("ocds-a"), _ai_release("ocds-b")]}
    assert len(_normalise_to_releases(pkg)) == 2


def test_normalise_record_package_prefers_compiled():
    rec = {"ocid": "ocds-a", "compiledRelease": {"ocid": "ocds-a", "tender": {"title": "t"}}}
    out = _normalise_to_releases({"records": [rec]})
    assert len(out) == 1
    # Compiled release without an id gets a synthesised, stable one.
    assert out[0]["id"] == "ocds-a-compiled"


def test_normalise_record_falls_back_to_releases():
    rec = {"ocid": "ocds-a", "releases": [_ai_release("ocds-a")]}
    out = _normalise_to_releases({"records": [rec]})
    assert len(out) == 1
    assert out[0]["id"] == "r1"


def test_normalise_bare_release():
    out = _normalise_to_releases(_ai_release("ocds-a"))
    assert len(out) == 1 and out[0]["ocid"] == "ocds-a"


def test_normalise_unrecognised_returns_empty():
    assert _normalise_to_releases({"foo": "bar"}) == []


# ── iter_releases_from_jsonl — plain and gzipped ───────────────────────────


def _write_jsonl(path: Path, lines: list[dict], gz: bool) -> Path:
    payload = "\n".join(json.dumps(o) for o in lines) + "\n"
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def test_iter_reads_plain_jsonl(tmp_path):
    p = _write_jsonl(tmp_path / "a.jsonl", [_ai_release("ocds-a"), _ai_release("ocds-b")], gz=False)
    assert len(list(iter_releases_from_jsonl(p))) == 2


def test_iter_reads_gzipped_jsonl(tmp_path):
    p = _write_jsonl(tmp_path / "a.jsonl.gz", [_ai_release("ocds-a")], gz=True)
    assert len(list(iter_releases_from_jsonl(p))) == 1


def test_iter_skips_blank_and_malformed_lines(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text(
        json.dumps(_ai_release("ocds-a")) + "\n\n" + "{not json}\n" + json.dumps(_ai_release("ocds-b")) + "\n",
        encoding="utf-8",
    )
    assert len(list(iter_releases_from_jsonl(p))) == 2


# ── load_archive — filtering, batching, idempotency ────────────────────────


def test_load_archive_ai_relevant_only(tmp_path, conn):
    p = _write_jsonl(
        tmp_path / "a.jsonl",
        [_ai_release("ocds-a"), _plain_release("ocds-b"), _ai_release("ocds-c")],
        gz=False,
    )
    inserted = load_archive(p, conn)
    assert inserted == 2  # the plain release is filtered out
    assert conn.execute("SELECT COUNT(*) FROM procurement_notices").fetchone()[0] == 2


def test_load_archive_idempotent(tmp_path, conn):
    p = _write_jsonl(tmp_path / "a.jsonl", [_ai_release("ocds-a"), _ai_release("ocds-b")], gz=False)
    assert load_archive(p, conn) == 2
    assert load_archive(p, conn) == 0  # re-run inserts nothing


def test_load_archive_batches_across_boundary(tmp_path, conn):
    releases = [_ai_release(f"ocds-{i}") for i in range(7)]
    p = _write_jsonl(tmp_path / "a.jsonl", releases, gz=False)
    # batch_size smaller than input forces multiple upserts; dedup must hold across them.
    inserted = load_archive(p, conn, batch_size=3)
    assert inserted == 7
    assert conn.execute("SELECT COUNT(*) FROM procurement_notices").fetchone()[0] == 7
