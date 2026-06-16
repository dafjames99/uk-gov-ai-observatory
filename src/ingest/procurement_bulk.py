"""Bulk OCDS backfill for procurement notices.

Loads historic procurement data from the Open Contracting Partnership Data
Registry bulk archives, rather than paginating the rate-limited live search
APIs. Each archive is newline-delimited JSON (optionally gzipped), one
contracting process per line. Lines are normalised to OCDS *releases* and run
through the same parse_release / upsert_notices path as the live ingest, so the
Silver result is identical and idempotent on notice_id.

Sources (OCP Data Registry publication ids):
    contracts_finder -> 128
    find_a_tender    -> 41

Download URL pattern:
    https://data.open-contracting.org/en/publication/{pub}/download?name={year}.jsonl.gz
    (name="full" for the all-time archive)
"""

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Iterator

import duckdb

from src.common.http import RateLimitedSession
from src.ingest.procurement import parse_release, upsert_notices

logger = logging.getLogger(__name__)

_OCP_DOWNLOAD = "https://data.open-contracting.org/en/publication/{pub}/download"
_PUBLICATION_IDS: dict[str, int] = {
    "contracts_finder": 128,
    "find_a_tender": 41,
}
_DEFAULT_BATCH_SIZE = 5000
_DOWNLOAD_CHUNK = 1 << 16  # 64 KiB


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def archive_url(source: str, year: int | None) -> str:
    """Build the OCP Data Registry bulk download URL.

    Args:
        source: One of the keys in _PUBLICATION_IDS.
        year: Calendar year, or None for the all-time ("full") archive.

    Returns:
        Fully qualified download URL for the gzipped JSONL archive.

    Raises:
        KeyError: If source is unknown.
    """
    pub = _PUBLICATION_IDS[source]
    name = "full" if year is None else str(year)
    return f"{_OCP_DOWNLOAD.format(pub=pub)}?name={name}.jsonl.gz"


def download_archive(
    session: RateLimitedSession,
    source: str,
    year: int | None,
    dest_dir: Path,
) -> Path:
    """Download a bulk archive to the Bronze layer, streaming to disk.

    Args:
        session: A configured RateLimitedSession.
        source: Procurement source key.
        year: Calendar year, or None for the all-time archive.
        dest_dir: Directory to write the archive into (created if absent).

    Returns:
        Path to the downloaded .jsonl.gz file.
    """
    url = archive_url(source, year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{source}_{year or 'full'}.jsonl.gz"

    logger.info("Downloading %s → %s", url, dest)
    resp = session.get(url, stream=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
            if chunk:
                f.write(chunk)

    logger.info("Downloaded %s (%d bytes)", dest.name, dest.stat().st_size)
    return dest


# ---------------------------------------------------------------------------
# Parsing the bulk format
# ---------------------------------------------------------------------------


def _releases_from_record(record: dict) -> list[dict]:
    """Extract release dicts from an OCDS record.

    Prefers the compiledRelease (the merged view of a contracting process),
    falling back to the raw releases array. Compiled releases lack a release
    'id', so one is synthesised from the ocid to keep notice_id stable.
    """
    compiled = record.get("compiledRelease")
    if compiled:
        if not compiled.get("id"):
            compiled = {**compiled, "id": f"{compiled.get('ocid', '')}-compiled"}
        return [compiled]
    return record.get("releases", [])


def _normalise_to_releases(obj: dict) -> list[dict]:
    """Normalise one bulk line into a list of OCDS release dicts.

    Tolerant of the shapes the registry emits: record packages, release
    packages, bare records, and bare releases.

    Args:
        obj: One parsed JSON line.

    Returns:
        Zero or more release dicts ready for parse_release().
    """
    if "records" in obj:  # record package
        out: list[dict] = []
        for record in obj["records"]:
            out.extend(_releases_from_record(record))
        return out
    if "releases" in obj:  # release package, or a record carrying releases
        return obj["releases"]
    if "compiledRelease" in obj:  # a bare record
        return _releases_from_record(obj)
    if obj.get("ocid"):  # a bare release
        return [obj]
    return []


def iter_releases_from_jsonl(path: Path) -> Iterator[dict]:
    """Yield OCDS release dicts from a (optionally gzipped) JSONL archive.

    Args:
        path: Path to a .jsonl or .jsonl.gz file.

    Yields:
        Release dicts, one at a time, suitable for parse_release().
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON on line %d of %s", line_no, path.name)
                continue
            yield from _normalise_to_releases(obj)


# ---------------------------------------------------------------------------
# Load into Silver
# ---------------------------------------------------------------------------


def load_archive(
    path: Path,
    conn: duckdb.DuckDBPyConnection,
    ai_relevant_only: bool = True,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> int:
    """Stream a bulk archive into the Silver procurement_notices table.

    Parses each release and upserts in batches. Dedup against existing
    notice_ids (including rows inserted by earlier batches in this run) is
    handled by upsert_notices, so the load is idempotent and safe to re-run.

    Args:
        path: Path to the .jsonl / .jsonl.gz archive.
        conn: An open DuckDB connection.
        ai_relevant_only: If True, persist only AI-relevant notices.
        batch_size: Number of parsed notices to accumulate before each upsert.

    Returns:
        Total number of new rows inserted.
    """
    batch: list[dict[str, Any]] = []
    parsed = inserted = errors = 0

    for release in iter_releases_from_jsonl(path):
        try:
            notice = parse_release(release)
        except Exception:
            errors += 1
            logger.debug("Failed to parse release %s", release.get("ocid"), exc_info=True)
            continue
        if notice:
            batch.append(notice)
            parsed += 1
            if len(batch) >= batch_size:
                inserted += upsert_notices(batch, conn, ai_relevant_only)
                batch.clear()

    if batch:
        inserted += upsert_notices(batch, conn, ai_relevant_only)

    logger.info(
        "Loaded %s: parsed %d notices, inserted %d new (%d parse errors)",
        path.name,
        parsed,
        inserted,
        errors,
    )
    return inserted
