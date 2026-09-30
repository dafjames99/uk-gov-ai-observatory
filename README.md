# UK Gov AI Observatory

Tracks UK central government AI activity along two axes:

- **Axis A — usage:** AI-relevant procurement notices (Contracts Finder, Find a
  Tender) and Algorithmic Transparency Recording Standard (ATRS) records.
- **Axis B — intent & capacity:** GOV.UK announcements, Written Parliamentary
  Questions and AI Growth Zones.

Design docs live in [`docs/`](docs/). The Streamlit dashboard is in
[`dashboard/app.py`](dashboard/app.py).

## Quick start

```bash
uv sync
uv run streamlit run dashboard/app.py
```

The dashboard reads only the committed Gold Parquet files in `data/gold/`, so it
runs from a fresh clone with no ingestion step.

## Data layout

| Path | Layer | In git? | Notes |
| --- | --- | --- | --- |
| `data/seeds/` | Reference | Yes | `org_aliases.csv`, `ai_growth_zones.csv` |
| `data/gold/` | Gold | Yes | Parquet exports of Silver tables and Gold views |
| `data/observatory.duckdb` | Silver + Gold views | No | ~150 MB, rebuilt by the scripts below |
| `data/bronze/` | Bronze (raw responses) | No | ~425 MB, re-downloaded by the scripts below |

The committed Gold snapshot was last exported on **2026-06-16**. Every source is
public and keyless (see `.env.example`), so the local layers can be rebuilt from
scratch. Re-running the live-API ingests today returns newer data than that
snapshot. The bulk procurement archives are versioned per year upstream, so
they are the most stable part of the rebuild.

## Rebuilding local data

Run from the repo root, in this order. Every step is idempotent (upserts), and
each ingest writes its raw responses to `data/bronze/` before touching Silver.

```bash
# 1. Create the DuckDB schema and load seeds (org aliases, AI Growth Zones)
uv run python scripts/init_db.py

# 2. Procurement history: OCP bulk OCDS archives, both sources, 2021..current year
#    (~410 MB download into data/bronze/{contracts_finder,find_a_tender}_bulk/)
uv run python -m scripts.procurement_backfill

# 3. Procurement incremental tail (default: last 7 days; widen with --from-date)
uv run python -m scripts.procurement_ingest --source contracts_finder
uv run python -m scripts.procurement_ingest --source find_a_tender

# 4. ATRS records (GOV.UK Search + Content APIs)
uv run python scripts/atrs_ingest.py

# 5. Axis B: Written Parliamentary Questions and GOV.UK announcements
uv run python -m scripts.wpq_ingest
uv run python -m scripts.announcements_ingest

# 6. Export Silver tables and Gold views to data/gold/ for the dashboard
uv run python scripts/export_gold.py
```

Upstream sources:

| Script | Source |
| --- | --- |
| `procurement_backfill` | `data.open-contracting.org` bulk downloads (Contracts Finder, Find a Tender) |
| `procurement_ingest` | Contracts Finder OCDS Search API; Find a Tender `ocdsReleasePackages` API |
| `atrs_ingest` | `www.gov.uk/api/search.json` and `www.gov.uk/api/content` |
| `wpq_ingest` | `questions-statements-api.parliament.uk` |
| `announcements_ingest` | `www.gov.uk/api/search.json` (filters in `config/announcements.yaml`) |

AI-relevance keyword rules (procurement, WPQs, announcements) are in
`config/ai_relevance.yaml`. ATRS records are kept wholesale.
To reload a bulk archive you already have locally, skip the download with
`--archive <path> --source <contracts_finder|find_a_tender>`.

## Tests

```bash
uv run pytest
```
