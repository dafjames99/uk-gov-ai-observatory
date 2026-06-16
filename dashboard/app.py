"""UK Gov AI Observatory — Streamlit dashboard (v2, three-lens).

USE — what AI government uses and buys (ATRS + procurement).
INTENT — what it plans and is scrutinised on (announcements + WPQs).
CAPACITY — the economic/physical expansion (AI Growth Zones).
"""

from glob import glob
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

GOLD_DIR = Path(__file__).parent.parent / "data" / "gold"

BLUE = "#1d70b8"
ORANGE = "#f47738"

st.set_page_config(
    page_title="UK Gov AI Observatory",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@st.cache_data(ttl=3600)
def load_atrs() -> pd.DataFrame:
    path = GOLD_DIR / "atrs_records.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["standard_version"] = (
        df["standard_version"].fillna("unknown").str.lstrip("v").apply(
            lambda x: f"v{x}" if x != "unknown" else x
        )
    )
    df["date_published"] = pd.to_datetime(df["date_published"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_procurement() -> pd.DataFrame:
    """Concatenate the year-partitioned procurement Parquet files."""
    parts = sorted(glob(str(GOLD_DIR / "procurement_notices" / "*.parquet")))
    if not parts:
        return pd.DataFrame()
    df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce")
    df["value_amount"] = pd.to_numeric(df["value_amount"], errors="coerce")
    df["ai_confidence"] = df["ai_confidence"].fillna("none")
    return df


def _load_simple(name: str) -> pd.DataFrame:
    path = GOLD_DIR / name
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


@st.cache_data(ttl=3600)
def load_reporting_gap() -> pd.DataFrame:
    return _load_simple("v_reporting_gap.parquet")


@st.cache_data(ttl=3600)
def load_announcements() -> pd.DataFrame:
    df = _load_simple("gov_announcements.parquet")
    if not df.empty:
        df["public_timestamp"] = pd.to_datetime(df["public_timestamp"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_wpq() -> pd.DataFrame:
    df = _load_simple("written_questions.parquet")
    if not df.empty:
        df["date_tabled"] = pd.to_datetime(df["date_tabled"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_growth_zones() -> pd.DataFrame:
    df = _load_simple("ai_growth_zones.parquet")
    if not df.empty:
        df["announced_date"] = pd.to_datetime(df["announced_date"], errors="coerce")
        df["investment_gbp"] = pd.to_numeric(df["investment_gbp"], errors="coerce")
    return df


atrs = load_atrs()
procurement = load_procurement()
gap = load_reporting_gap()
announcements = load_announcements()
wpq = load_wpq()
zones = load_growth_zones()

# ---------------------------------------------------------------------------
# Header + global controls
# ---------------------------------------------------------------------------

st.title("UK Gov AI Observatory")
st.caption(
    "Tracking how UK central government **uses**, **plans for**, and **builds capacity for** AI — "
    "across procurement, algorithmic transparency, announcements, parliamentary scrutiny and infrastructure. "
    "Sources: [ATRS Hub](https://www.gov.uk/algorithmic-transparency-records) · "
    "[Contracts Finder](https://www.contractsfinder.service.gov.uk) · "
    "[Find a Tender](https://www.find-tender.service.gov.uk) · "
    "[GOV.UK](https://www.gov.uk) · [UK Parliament](https://questions-statements.parliament.uk)"
)

confidence_mode = st.radio(
    "AI-relevance confidence for procurement",
    options=["Strong only", "Strong + weak"],
    horizontal=True,
    help="Strong = explicit AI terms. Weak = ambiguous/soft signals (generic IT, 'algorithm'). "
    "Headline figures default to strong for precision.",
)
if not procurement.empty:
    if confidence_mode == "Strong only":
        proc = procurement[procurement["ai_confidence"] == "strong"].copy()
    else:
        proc = procurement[procurement["ai_confidence"].isin(["strong", "weak"])].copy()
else:
    proc = procurement

st.divider()

# ---------------------------------------------------------------------------
# Top-line metrics
# ---------------------------------------------------------------------------

spend = proc["value_amount"].sum() if not proc.empty else 0
median_val = proc["value_amount"].median() if not proc.empty else None

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("ATRS records", len(atrs) if not atrs.empty else "—")
m2.metric(f"AI contracts ({confidence_mode.split()[0].lower()})", f"{len(proc):,}" if not proc.empty else 0)
m3.metric("Median AI contract", f"£{median_val:,.0f}" if median_val is not None and pd.notna(median_val) else "—")
m4.metric("AI announcements", len(announcements) if not announcements.empty else 0)
m5.metric("AI written questions", len(wpq) if not wpq.empty else 0)
pledged = zones["investment_gbp"].sum() if not zones.empty else 0
m6.metric("Growth Zone £ pledged", f"£{pledged/1e9:.1f}bn" if pledged else "—")

if not proc.empty:
    st.caption(
        f"Contracts are de-duplicated across stages, dates and both procurement sources "
        f"(Contracts Finder + Find a Tender). Total AI-flagged contract value is "
        f"**~£{spend/1e9:.0f}bn**, but that sums notice values and is inflated by framework ceiling "
        f"figures (multi-billion ICT frameworks that merely mention AI) — treat it as an upper bound, "
        f"not actual AI outlay. The **median** contract above is the reliable central figure."
    )

st.divider()

# ---------------------------------------------------------------------------
# Lenses
# ---------------------------------------------------------------------------

lens_use, lens_intent, lens_capacity = st.tabs(
    ["🔧 USE — deployed & bought", "🗣 INTENT — planned & scrutinised", "🏗 CAPACITY — expansion"]
)

# ── USE ────────────────────────────────────────────────────────────────────

with lens_use:
    st.subheader("AI-flagged procurement")
    if proc.empty:
        st.info("No procurement data loaded. Run the backfill and `scripts/export_gold.py`.")
    else:
        by_source = proc["source"].value_counts().to_dict()
        st.caption(
            f"{len(proc):,} notices · "
            f"Contracts Finder: {by_source.get('contracts_finder', 0):,} · "
            f"Find a Tender: {by_source.get('find_a_tender', 0):,}"
        )

        monthly = (
            proc.dropna(subset=["published_date"])
            .assign(month=lambda d: d["published_date"].dt.to_period("M").dt.to_timestamp())
            .groupby(["month", "source"])
            .agg(value=("value_amount", "sum"), notices=("notice_id", "count"))
            .reset_index()
        )
        fig = px.bar(
            monthly,
            x="month",
            y="value",
            color="source",
            title="AI-flagged contract value by month",
            labels={"month": "", "value": "£ value", "source": ""},
            color_discrete_map={"contracts_finder": BLUE, "find_a_tender": ORANGE},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=1.05))
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("**Largest AI-flagged contracts**")
        top = proc.nlargest(20, "value_amount")[
            ["buyer_name", "title", "value_amount", "source", "ai_confidence", "published_date", "source_url"]
        ].copy()
        top["value_amount"] = top["value_amount"].apply(lambda x: f"£{x:,.0f}" if pd.notna(x) else "—")
        top["published_date"] = top["published_date"].dt.strftime("%Y-%m-%d")
        top.columns = ["Buyer", "Title", "Value", "Source", "Confidence", "Published", "Link"]
        st.dataframe(
            top,
            use_container_width=True,
            hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="↗")},
        )

    st.divider()
    st.subheader("Algorithmic Transparency (ATRS) records")
    if atrs.empty:
        st.info("No ATRS data loaded.")
    else:
        col_f, col_c = st.columns([1, 2])
        with col_f:
            search = st.text_input("Search organisation or description", placeholder="e.g. DWP, fraud")
            phases = st.multiselect("Phase", sorted(atrs["phase"].dropna().unique()))
        with col_c:
            phase_counts = atrs.groupby("phase").size().reset_index(name="count").sort_values("count")
            fig = px.bar(
                phase_counts, x="count", y="phase", orientation="h",
                title="ATRS records by phase", labels={"count": "Records", "phase": ""},
                color="count", color_continuous_scale="Blues",
            )
            fig.update_layout(showlegend=False, coloraxis_showscale=False, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        filtered = atrs.copy()
        if search:
            mask = (
                filtered["one_sentence_desc"].fillna("").str.contains(search, case=False)
                | filtered["organisation_name"].fillna("").str.contains(search, case=False)
            )
            filtered = filtered[mask]
        if phases:
            filtered = filtered[filtered["phase"].isin(phases)]

        show = filtered[["organisation_name", "phase", "standard_version", "date_published", "one_sentence_desc", "source_url"]].copy()
        show["date_published"] = show["date_published"].dt.strftime("%Y-%m-%d")
        show.columns = ["Organisation", "Phase", "Version", "Published", "Description", "Link"]
        st.caption(f"Showing {len(show)} of {len(atrs)} records")
        st.dataframe(
            show, use_container_width=True, hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="GOV.UK ↗")},
        )

    st.divider()
    st.subheader("Reporting gap — procurement vs. ATRS registration")
    if gap.empty:
        st.info("No reporting-gap data.")
    else:
        active = gap[(gap["ai_procurement_count"] > 0) | (gap["atrs_record_count"] > 0)].copy()
        active = active.sort_values("ai_procurement_count", ascending=False).head(25)
        melted = active.melt(
            id_vars="canonical_name",
            value_vars=["ai_procurement_count", "atrs_record_count"],
            var_name="metric", value_name="count",
        )
        melted["metric"] = melted["metric"].map(
            {"ai_procurement_count": "AI-flagged contracts", "atrs_record_count": "ATRS registrations"}
        )
        fig = px.bar(
            melted, x="count", y="canonical_name", color="metric", orientation="h", barmode="group",
            title="AI procurement vs. ATRS registrations by department (top 25)",
            labels={"count": "Count", "canonical_name": "", "metric": ""},
            color_discrete_map={"AI-flagged contracts": BLUE, "ATRS registrations": ORANGE},
            height=max(400, len(active) * 26),
        )
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=1.02))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Reporting gap counts use all AI-relevant procurement (strong + weak) regardless of the "
            "toggle above, since it measures presence of activity, not spend."
        )

# ── INTENT ─────────────────────────────────────────────────────────────────

with lens_intent:
    st.subheader("Government AI announcements")
    if announcements.empty:
        st.info("No announcements loaded. Run `scripts/announcements_ingest.py`.")
    else:
        ann = announcements.dropna(subset=["public_timestamp"]).copy()
        ann["month"] = ann["public_timestamp"].dt.to_period("M").dt.to_timestamp()
        trend = ann.groupby(["month", "document_type"]).size().reset_index(name="count")
        fig = px.bar(
            trend, x="month", y="count", color="document_type",
            title="AI announcements by month and type",
            labels={"month": "", "count": "Announcements", "document_type": ""},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=1.05))
        st.plotly_chart(fig, use_container_width=True)

        types = st.multiselect("Document type", sorted(ann["document_type"].dropna().unique()))
        q = st.text_input("Search announcement title", placeholder="e.g. growth zone, compute, sovereign")
        view = ann.sort_values("public_timestamp", ascending=False)
        if types:
            view = view[view["document_type"].isin(types)]
        if q:
            view = view[view["title"].fillna("").str.contains(q, case=False)]
        show = view[["public_timestamp", "document_type", "title", "summary", "source_url"]].copy()
        show["public_timestamp"] = show["public_timestamp"].dt.strftime("%Y-%m-%d")
        show.columns = ["Published", "Type", "Title", "Summary", "Link"]
        st.caption(f"Showing {len(show)} of {len(ann)} announcements")
        st.dataframe(
            show, use_container_width=True, hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="GOV.UK ↗")},
        )

    st.divider()
    st.subheader("Parliamentary scrutiny — written questions on AI")
    if wpq.empty:
        st.info("No written questions loaded. Run `scripts/wpq_ingest.py`.")
    else:
        wq = wpq.dropna(subset=["date_tabled"]).copy()
        col_a, col_b = st.columns(2)
        with col_a:
            monthly = wq.assign(month=lambda d: d["date_tabled"].dt.to_period("M").dt.to_timestamp()).groupby("month").size().reset_index(name="count")
            fig = px.bar(
                monthly, x="month", y="count", title="AI written questions by month",
                labels={"month": "", "count": "Questions"}, color_discrete_sequence=[BLUE],
            )
            fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            dept = wq["department"].value_counts().head(10).reset_index()
            dept.columns = ["department", "count"]
            fig = px.bar(
                dept.sort_values("count"), x="count", y="department", orientation="h",
                title="Most-questioned departments", labels={"count": "Questions", "department": ""},
                color_discrete_sequence=[ORANGE],
            )
            fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        show = wq.sort_values("date_tabled", ascending=False)[
            ["date_tabled", "house", "department", "member_name", "question_text", "source_url"]
        ].copy()
        show["date_tabled"] = show["date_tabled"].dt.strftime("%Y-%m-%d")
        show["question_text"] = show["question_text"].fillna("").str[:160]
        show.columns = ["Tabled", "House", "Department", "Member", "Question", "Link"]
        st.dataframe(
            show, use_container_width=True, hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="↗")},
        )

# ── CAPACITY ───────────────────────────────────────────────────────────────

with lens_capacity:
    st.subheader("AI Growth Zones")
    if zones.empty:
        st.info("No Growth Zones loaded. Run `scripts/init_db.py`.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Zones tracked", len(zones))
        c2.metric("Confirmed", int((zones["status"] == "confirmed").sum()))
        pledged = zones["investment_gbp"].sum()
        c3.metric("Disclosed £ pledged", f"£{pledged/1e9:.1f}bn" if pledged else "—")

        show = zones.sort_values("announced_date")[
            ["zone_name", "region", "status", "investment_gbp", "compute_capacity", "announced_date", "source_url", "notes"]
        ].copy()
        show["investment_gbp"] = show["investment_gbp"].apply(
            lambda x: f"£{x/1e9:.1f}bn" if pd.notna(x) else "—"
        )
        show["announced_date"] = show["announced_date"].dt.strftime("%Y-%m-%d")
        show.columns = ["Zone", "Region", "Status", "£ pledged", "Compute", "Announced", "Source", "Notes"]
        st.dataframe(
            show, use_container_width=True, hide_index=True,
            column_config={"Source": st.column_config.LinkColumn("Source", display_text="GOV.UK ↗")},
        )
        st.caption(
            "Curated from GOV.UK announcements; figures are as-reported (only well-sourced £ shown). "
            "**Data-centre planning data is not yet integrated** — planning.data.gov.uk is England-only and "
            "the North Wales and Lanarkshire zones are devolved, so it is deferred as a supplementary source."
        )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Contains public sector information licensed under the "
    "[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). "
    "Parliamentary data licensed under the "
    "[Open Parliament Licence](https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/). "
    "AI-relevance is a versioned heuristic, not an official classification. "
    "This tool is for research and reference — not a legal compliance audit."
)
