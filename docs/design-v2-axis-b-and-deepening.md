# Design v2 — Closing the Axis B gap & deepening Axis A

**Status:** Draft for review
**Date:** 16 June 2026
**Supersedes:** Phase 2/3 ordering in the PRD (`uk-gov-ai-observatory-prd.md` §4)
**Decisions locked:** Both axes, phased (backfill first). Deterministic baseline now; LLM enrichment deferred behind a clean seam. Scope decisions resolved in §10.

---

## 1. Why this exists

The mission has two axes:

- **Axis A — utilisation:** what AI government *uses and buys*. (ATRS + procurement)
- **Axis B — intent & economic expansion:** what government *plans, funds, scrutinises, and builds* — AI Growth Zones, compute, sovereign AI, data centres, investment pledges.

v1 shipped 100% Axis A on the *lowest-value* procurement feed (Contracts Finder, ≥£12k sub-threshold) and 0% Axis B. This design:

1. Removes the backfill dead-end (bulk OCDS archives, not the rate-limited live API).
2. Deepens Axis A (Find a Tender high-value feed + richer OCDS fields + scored relevance).
3. Opens Axis B (announcements, Growth Zones register, WPQs, data-centre buildout).
4. Reframes the dashboard into **USE / INTENT / CAPACITY** lenses.

All new sources are keyless and automatable. £0/month preserved.

---

## 2. Architecture changes at a glance

```
data/bronze/<source>/<run_date>/        # unchanged pattern, new source dirs
  contracts_finder/   find_a_tender/   announcements/   wpq/   planning/

src/ingest/
  procurement.py        # generalised: live CF + FTS via one fetcher
  procurement_bulk.py   # NEW — stream bulk OCDS archives (.jsonl.gz / yearly)
  announcements.py      # NEW — GOV.UK Search API, document_type filter
  written_questions.py  # NEW — questions-statements-api.parliament.uk
  planning.py           # NEW — planning.data.gov.uk data-centre buildout
  ai_relevance.py       # rewritten: tiered/scored, multi-domain

src/enrich/             # NEW (empty seam) — future LLM stage, not built yet

src/common/
  db.py                 # + migrate() for additive ALTER TABLE columns
  http.py               # unchanged (already handles cursor + retry)

data/seeds/
  org_aliases.csv       # unchanged
  ai_growth_zones.csv   # NEW curated register
```

**Source-of-truth principle:** the deterministic classifier writes `ai_relevant` / `ai_confidence`. Any future LLM output lands in *separate, additive* columns (`*_summary`, `topic_tags`, `enrichment_version`) and never overwrites deterministic fields.

---

## 3. Schema migrations

DuckDB's `CREATE TABLE IF NOT EXISTS` does not alter existing tables, so additive changes go through a new idempotent `migrate()` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. `init_schema()` calls `migrate()` at the end so a fresh DB and an existing DB converge to the same shape.

### 3.1 `procurement_notices` — additive columns

| Column | Type | Purpose |
|---|---|---|
| `documents` | JSON | `tender.documents[]` — array of `{title, url, documentType}`. The route to real detail behind thin descriptions. |
| `awards` | JSON | Full `awards[]` array (supplier, value, dates) — not just the first. |
| `framework_id` | VARCHAR | Lot/framework identifier; distinguishes call-offs from standalone awards. |
| `procurement_method` | VARCHAR | `tender.procurementMethod` (open/selective/limited/direct). |
| `ai_confidence` | VARCHAR | `strong` / `weak` / `none` — replaces the binary signal as the trust dimension. |
| `notice_summary` | VARCHAR | LLM seam — nullable, populated later by `src/enrich/`. |
| `enrichment_version` | VARCHAR | LLM seam — null until enriched. |

`ai_relevant` (bool) is retained for backward compatibility but is now derived as `ai_confidence != 'none'`. `value_amount`/`currency`/`supplier_*` keep taking the *first* award for the headline figure; `awards` holds the full set.

### 3.2 `written_questions` — already exists, no change

The v1 schema (`scripts/init_db.py:84`) is correct. Add only the LLM seam columns: `topic_tags` already present; add `enrichment_version VARCHAR`.

### 3.3 New table — `gov_announcements`

```sql
CREATE TABLE IF NOT EXISTS gov_announcements (
    announcement_id      VARCHAR PRIMARY KEY,  -- GOV.UK content_id or base_path slug
    title                VARCHAR,
    document_type        VARCHAR,              -- press_release | news_story | policy_paper | consultation | guidance
    organisations        JSON,                 -- array of org slugs (filter_organisations values)
    public_timestamp     TIMESTAMPTZ,          -- first published
    updated_timestamp    TIMESTAMPTZ,
    summary              VARCHAR,              -- GOV.UK description field
    body_excerpt         VARCHAR,              -- first N chars of Content API body (provenance, not full text)
    ai_relevant          BOOLEAN,
    ai_confidence        VARCHAR,              -- strong | weak | none
    ai_relevance_version VARCHAR,
    topic_tags           JSON,                 -- LLM seam, null until enriched
    enrichment_version   VARCHAR,              -- LLM seam
    source_url           VARCHAR,
    ingested_at          TIMESTAMPTZ
);
```

This is the spine of Axis B intent. It reuses the exact GOV.UK Search + Content API pattern already proven in `src/ingest/atrs.py` — only the `filter_content_store_document_type` value changes.

### 3.4 New table — `ai_growth_zones` (curated, seeded from CSV)

```sql
CREATE TABLE IF NOT EXISTS ai_growth_zones (
    zone_id          VARCHAR PRIMARY KEY,   -- kebab slug, e.g. 'north-wales'
    zone_name        VARCHAR,
    site             VARCHAR,
    region           VARCHAR,
    status           VARCHAR,               -- announced | confirmed | construction | operational
    investment_gbp   DOUBLE,                -- pledged £, nullable
    compute_capacity VARCHAR,              -- free text e.g. '100MW→2GW' (no false precision)
    announced_date   DATE,
    lead_org         VARCHAR,
    source_url       VARCHAR,               -- the GOV.UK announcement it was sourced from
    notes            VARCHAR
);
```

Curated like `org_aliases` — low-churn, high-signal. Seeded from `data/seeds/ai_growth_zones.csv`, cross-linked to `gov_announcements` by `source_url`. Manual curation is acceptable here because there are ~single digits of zones and each is individually newsworthy.

### 3.5 New table — `datacentre_planning` (Capacity, automated)

```sql
CREATE TABLE IF NOT EXISTS datacentre_planning (
    application_id   VARCHAR PRIMARY KEY,   -- planning.data.gov.uk reference
    site_name        VARCHAR,
    local_authority  VARCHAR,
    description      VARCHAR,
    status           VARCHAR,
    decision_date    DATE,
    latitude         DOUBLE,
    longitude        DOUBLE,
    dc_relevant      BOOLEAN,               -- keyword-matched 'data centre' / use class
    source_url       VARCHAR,
    ingested_at      TIMESTAMPTZ
);
```

Bulk CSV/GeoJSON download, filtered locally for data-centre use. Connects the policy promises (Growth Zones) to physical reality (what's actually been applied for / approved).

**Supplementary, not core** (§10.4) — planning.data.gov.uk is England-only, and the marquee zones (North Wales, Lanarkshire) are devolved and *outside* it. The curated national `ai_growth_zones` register carries the Capacity lens; this feed adds England-only granularity with a loud UI caveat. Deprioritised below `ai_growth_zones` (B4 → after B2).

---

## 4. Procurement: backfill + Find a Tender

### 4.1 Bulk backfill (`src/ingest/procurement_bulk.py`)

- **Contracts Finder:** monthly OCDS snapshots on data.gov.uk as gzipped JSONL (one contracting process per line).
- **Find a Tender:** OCDS bulk download per-year or all-time (JSON).

**Depth: from 2021 onward** (§10.1) — Find a Tender launched Jan 2021, so anchoring both feeds at the same start gives a consistent two-feed denominator with a pre-LLM (2021–2022) baseline.

Flow: download archive → stream line-by-line → `parse_release()` (unchanged) → `upsert_notices()` (unchanged, idempotent on `notice_id`). No live API, no rate limit. Bronze stores the downloaded archive path + a manifest, not re-exploded pages.

### 4.2 Find a Tender live source (generalise `procurement.py`)

Add a `Source` config rather than forking the module:

| | Contracts Finder | Find a Tender |
|---|---|---|
| Base URL | `/Published/Notices/OCDS/Search` | `/api/1.0/ocdsReleasePackages` |
| Date params | `publishedFrom`/`publishedTo` | `updatedFrom`/`updatedTo` |
| Pagination | `links.next` cursor | `cursor` token |
| Rate limit | ~24 req/session, no header | 429 + `Retry-After` header (honour it) |
| `source` value | `contracts_finder` | `find_a_tender` |

`fetch_releases()` takes a `Source` dataclass; the rest of the module is shared. FTS gives the high-value contracts that are the whole point of tracking AI spend.

### 4.3 Richer parsing

`parse_release()` gains extraction of `documents`, full `awards`, `framework_id`, `procurement_method` (§3.1). Backward-compatible — existing fields unchanged. Re-running the backfill repopulates the new columns from Bronze.

---

## 5. AI-relevance v2 — tiered & scored

v1's `algorithm`/`algorithmic` keywords + CPV `72xx`/`48xx` flag most government IT. Replace the binary with scored tiers in `config/ai_relevance.yaml` (bump to `version: 2.0`):

```yaml
version: "2.0"
strong_keywords:    # high precision — alone sufficient for 'strong'
  - artificial intelligence
  - machine learning
  - large language model
  - generative ai
  - neural network
  - computer vision
  - automated decision-making
weak_keywords:      # only 'weak' unless paired with a strong CPV
  - algorithm
  - predictive
  - automation
  - data science
strong_cpv_prefixes: ["7222", "7223", "48000000"]   # narrowed
weak_cpv_prefixes:   ["7221", "7226", "7231"]
scoring:
  strong: any strong_keyword OR (weak_keyword AND strong_cpv)
  weak:   weak_keyword OR strong_cpv
  none:   otherwise
```

`is_ai_relevant()` returns `("strong"|"weak"|"none")`; callers map to `ai_confidence` and derive `ai_relevant = confidence != 'none'`. Dashboard defaults to `strong`, with a toggle to include `weak`. The same scorer, with a domain-specific keyword set, classifies `gov_announcements`. Methodology stays versioned, so the v1→v2 shift is auditable, not silent.

---

## 6. Gold views & exports

New/changed views in `_init_gold_views()`:

- `v_spend_by_month` — unchanged shape; now spans CF + FTS + backfilled history.
- `v_reporting_gap` — unchanged.
- `v_announcement_trends` — AI announcements per month by `document_type` and organisation (INTENT lens).
- `v_capacity_overview` — Growth Zones joined to `datacentre_planning` by region; pledged £ and status roll-up (CAPACITY lens).
- `v_wpq_trends` — already defined; activates once WPQs ingested.

`scripts/export_gold.py` gains the new Parquet exports. `gov_announcements`, `ai_growth_zones`, `datacentre_planning`, and the two new views are added to the `exports` dict.

---

## 7. Dashboard reframe — three lenses

Restructure `dashboard/app.py` from 3 procurement-centric tabs into three lenses:

- **USE** — ATRS browser, procurement spend (CF + FTS), reporting gap. (existing tabs, re-homed)
- **INTENT** — announcements timeline by department/type, WPQ trends, consultations.
- **CAPACITY** — Growth Zones map/table, pledged investment, data-centre planning pipeline.

Top-line metrics expand: existing four + "AI announcements (12mo)", "£ pledged (Growth Zones)", "Data-centre applications".

---

## 8. The LLM seam (deferred, not designed away)

Built now, populated later — no schema migration required to switch it on:

- Nullable columns on every enrichment-eligible table: `notice_summary`/`topic_tags` + `enrichment_version`.
- Empty `src/enrich/` package with the interface stubbed: `enrich(rows, version) -> rows`.
- Deterministic classifier remains the source of truth; LLM output is always additive.

When switched on, candidates are: summarise thin notices from their `documents[]`, topic-tag WPQs and announcements, suggest relevance for borderline `weak` cases (human-reviewed). Out of scope for this design.

---

## 9. Sequenced delivery

Each step is its own branch + PR (per workflow rule).

| # | Deliverable | Depends on |
|---|---|---|
| A1 | `db.migrate()` + new columns/tables (§3) | — |
| A2 | `procurement_bulk.py` backfill loader (§4.1) | A1 |
| A3 | Richer `parse_release()` (§4.3) | A1 |
| A4 | Find a Tender live source (§4.2) | A3 |
| A5 | AI-relevance v2 scorer (§5) | A1 |
| B1 | `announcements.py` + `gov_announcements` (§3.3) | A5 |
| B2 | `ai_growth_zones.csv` + seed loader (§3.4) | A1 |
| B3 | `written_questions.py` (§3.2) | A5 |
| B5 | Gold views + export updates (§6) | B1–B3 |
| B6 | Dashboard three-lens rebuild (§7) | B5 |
| B4 | `planning.py` data-centre feed (§3.5) — *supplementary, deferred* | A1 |

A1–A2 alone remove the dead-end. A3–A5 complete Axis A. B1–B3 + B5–B6 open Axis B. B4 is supplementary (§10.4) and does not block the dashboard.

---

## 10. Resolved scope decisions (16 June 2026)

1. **Backfill depth — from 2021.** Aligns Contracts Finder + Find a Tender (FTS launched Jan 2021) for a consistent two-feed denominator; retains a pre-LLM 2021–2022 baseline so the AI procurement inflection is visible. Rejected: all-time (noisy pre-2020 false positives), 2023 (no baseline).
2. **`weak` relevance — strong-only headline, weak behind a toggle.** Headline £/counts use `strong` matches only; `weak` is queryable with a confidence badge. Keeps headline numbers defensible without discarding soft-signal AI. Rejected: including weak in headline (recreates the current false-positive problem).
3. **Growth Zones curation — feed-flagged, hand-curated.** The announcements feed surfaces candidate zone announcements by keyword; the structured row (£, compute, status) is curated by hand. Detection automated, extraction human. Rejected: fully manual (misses announcements), fully automated (mangles stated figures).
4. **Planning data — supplementary, England-only, with caveat.** Curated national `ai_growth_zones` carries the Capacity lens; planning.data.gov.uk adds England-only detail with a UI caveat. Deprioritised (B4, post-dashboard). Rejected: core England-only source (misses devolved marquee zones), pursuing devolved portals now (heavy scraping, marginal coverage).
```
