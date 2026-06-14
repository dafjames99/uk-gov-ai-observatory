"""UK Gov AI Observatory — Streamlit dashboard."""

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

GOLD_DIR = Path(__file__).parent.parent / "data" / "gold"

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
    # Normalise standard_version to always carry the 'v' prefix
    df["standard_version"] = df["standard_version"].fillna("unknown").str.lstrip("v").apply(
        lambda x: f"v{x}" if x != "unknown" else x
    )
    df["date_published"] = pd.to_datetime(df["date_published"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_procurement() -> pd.DataFrame:
    path = GOLD_DIR / "procurement_notices.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce")
    df["value_amount"] = pd.to_numeric(df["value_amount"], errors="coerce")
    return df


@st.cache_data(ttl=3600)
def load_reporting_gap() -> pd.DataFrame:
    path = GOLD_DIR / "v_reporting_gap.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


@st.cache_data(ttl=3600)
def load_spend_by_month() -> pd.DataFrame:
    path = GOLD_DIR / "v_spend_by_month.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["month"] = pd.to_datetime(df["month"], errors="coerce")
    df["total_value"] = pd.to_numeric(df["total_value"], errors="coerce")
    return df


atrs = load_atrs()
procurement = load_procurement()
gap = load_reporting_gap()
spend = load_spend_by_month()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("UK Gov AI Observatory")
st.caption(
    "Tracking algorithmic transparency registrations and AI-related procurement "
    "across UK central government and arm's-length bodies. "
    "Data: [ATRS Hub](https://www.gov.uk/algorithmic-transparency-records) · "
    "[Contracts Finder](https://www.contractsfinder.service.gov.uk) · "
    "Licences: [OGL v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)"
)

st.divider()

# ---------------------------------------------------------------------------
# Top-level metrics
# ---------------------------------------------------------------------------

c1, c2, c3, c4 = st.columns(4)
c1.metric("ATRS Records", len(atrs) if not atrs.empty else "—")
c2.metric("AI-relevant Contracts", len(procurement) if not procurement.empty else "—")

prod_count = len(atrs[atrs["phase"] == "production"]) if not atrs.empty else 0
c3.metric("In Production", prod_count)

if not procurement.empty and "value_amount" in procurement.columns:
    total_val = procurement["value_amount"].sum()
    c4.metric("Total AI-flagged Spend", f"£{total_val/1e6:.1f}m" if total_val > 0 else "£0")
else:
    c4.metric("Total AI-flagged Spend", "—")

st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_atrs, tab_spend, tab_gap = st.tabs(
    ["ATRS Records", "Procurement Spend", "Reporting Gap"]
)

# ── Tab 1: ATRS Records ────────────────────────────────────────────────────

with tab_atrs:
    if atrs.empty:
        st.info("No ATRS data loaded. Run `scripts/atrs_ingest.py` then `scripts/export_gold.py`.")
    else:
        col_filter, col_chart = st.columns([1, 2])

        with col_filter:
            search = st.text_input("Search title or description", placeholder="e.g. fraud, DWP, chatbot")
            phases = st.multiselect(
                "Phase",
                options=sorted(atrs["phase"].dropna().unique()),
                default=[],
            )
            versions = st.multiselect(
                "ATRS version",
                options=sorted(atrs["standard_version"].dropna().unique()),
                default=[],
            )

        filtered = atrs.copy()
        if search:
            mask = (
                filtered["one_sentence_desc"].fillna("").str.contains(search, case=False)
                | filtered["organisation_name"].fillna("").str.contains(search, case=False)
                | filtered["record_id"].fillna("").str.contains(search, case=False)
            )
            filtered = filtered[mask]
        if phases:
            filtered = filtered[filtered["phase"].isin(phases)]
        if versions:
            filtered = filtered[filtered["standard_version"].isin(versions)]

        with col_chart:
            phase_counts = (
                atrs.groupby("phase").size().reset_index(name="count")
                .sort_values("count", ascending=True)
            )
            fig = px.bar(
                phase_counts,
                x="count",
                y="phase",
                orientation="h",
                title="Records by Phase (all data)",
                labels={"count": "Records", "phase": ""},
                color="count",
                color_continuous_scale="Blues",
            )
            fig.update_layout(showlegend=False, coloraxis_showscale=False, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        st.caption(f"Showing {len(filtered)} of {len(atrs)} records")

        display_cols = ["organisation_name", "phase", "standard_version", "date_published", "one_sentence_desc", "source_url"]
        display_df = filtered[display_cols].copy()
        display_df["date_published"] = display_df["date_published"].dt.strftime("%Y-%m-%d")
        display_df["one_sentence_desc"] = display_df["one_sentence_desc"].fillna("").str[:120]
        display_df.columns = ["Organisation", "Phase", "Version", "Published", "Description", "Source"]

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Source": st.column_config.LinkColumn("Source", display_text="GOV.UK ↗"),
            },
        )

# ── Tab 2: Procurement Spend ───────────────────────────────────────────────

with tab_spend:
    if procurement.empty or spend.empty:
        st.info(
            "No procurement data loaded yet. "
            "Run `scripts/procurement_ingest.py` then `scripts/export_gold.py`."
        )
        st.markdown(
            "Once loaded, this tab will show AI-flagged contract spend trends over time, "
            "filterable by department, with a supplier breakdown."
        )
    else:
        dept_options = ["All departments"] + sorted(spend["canonical_name"].dropna().unique())
        dept_filter = st.selectbox("Filter by department", dept_options)

        spend_filtered = spend if dept_filter == "All departments" else spend[spend["canonical_name"] == dept_filter]
        monthly = (
            spend_filtered.groupby("month")
            .agg(total_value=("total_value", "sum"), notices=("notice_count", "sum"))
            .reset_index()
        )

        fig = px.bar(
            monthly,
            x="month",
            y="total_value",
            title="AI-flagged Contract Value by Month",
            labels={"month": "", "total_value": "£ value"},
            color_discrete_sequence=["#1d70b8"],
        )
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Top contracts")
        top = procurement.nlargest(20, "value_amount")[
            ["buyer_name", "title", "value_amount", "published_date", "stage", "source_url"]
        ].copy()
        top["value_amount"] = top["value_amount"].apply(lambda x: f"£{x:,.0f}" if pd.notna(x) else "—")
        top["published_date"] = top["published_date"].dt.strftime("%Y-%m-%d")
        top.columns = ["Buyer", "Title", "Value", "Published", "Stage", "Source"]
        st.dataframe(
            top,
            use_container_width=True,
            hide_index=True,
            column_config={"Source": st.column_config.LinkColumn("Source", display_text="↗")},
        )

# ── Tab 3: Reporting Gap ───────────────────────────────────────────────────

with tab_gap:
    if gap.empty:
        st.info("No data available.")
    else:
        st.markdown(
            "**Reporting gap:** comparison of AI-flagged procurement activity against ATRS registrations, "
            "joined via the `org_aliases` lookup table. Departments with procurement spend but no ATRS "
            "registration may represent under-reporting."
        )

        # Only show orgs with at least one signal
        gap_active = gap[(gap["ai_procurement_count"] > 0) | (gap["atrs_record_count"] > 0)].copy()
        gap_active = gap_active.sort_values("atrs_record_count", ascending=False)

        # Melt for grouped bar chart
        gap_melted = gap_active.melt(
            id_vars="canonical_name",
            value_vars=["ai_procurement_count", "atrs_record_count"],
            var_name="metric",
            value_name="count",
        )
        gap_melted["metric"] = gap_melted["metric"].map(
            {"ai_procurement_count": "AI-flagged Contracts", "atrs_record_count": "ATRS Registrations"}
        )

        fig = px.bar(
            gap_melted,
            x="count",
            y="canonical_name",
            color="metric",
            orientation="h",
            barmode="group",
            title="AI Procurement vs. ATRS Registrations by Department",
            labels={"count": "Count", "canonical_name": "", "metric": ""},
            color_discrete_map={
                "AI-flagged Contracts": "#1d70b8",
                "ATRS Registrations": "#f47738",
            },
            height=max(400, len(gap_active) * 28),
        )
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), legend=dict(orientation="h", y=1.05))
        st.plotly_chart(fig, use_container_width=True)

        unmatched_orgs = gap[(gap["ai_procurement_count"] == 0) & (gap["atrs_record_count"] == 0)]
        if not unmatched_orgs.empty:
            st.caption(
                f"⚠ {len(unmatched_orgs)} organisations in the alias table have no activity in either dataset yet."
            )

        with st.expander("Full gap table"):
            display_gap = gap_active.copy()
            display_gap.columns = ["Department", "AI-flagged Contracts", "ATRS Registrations"]
            st.dataframe(display_gap, use_container_width=True, hide_index=True)

        with st.expander("Methodology — AI-relevance definition"):
            st.markdown(
                """
                A procurement notice is flagged as AI-relevant if **any** of the following match:

                **Keywords** (applied to title + description, case-insensitive)
                `artificial intelligence`, `machine learning`, `deep learning`, `neural network`,
                `large language model`, `generative ai`, `llm`, `natural language processing`,
                `nlp`, `computer vision`, `automated decision`, `predictive analytics`,
                `predictive model`, `algorithm`, `algorithmic`

                **CPV code prefixes**
                `7221`, `7222`, `7223`, `7224`, `7226`, `7231`, `7232`, `4800`, `4815`, `4816`, `4817`

                This definition is versioned (`ai_relevance_version` field on each record).
                Changing the definition requires re-processing from Bronze snapshots.
                """
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
    "This tool is for research and reference — not a legal compliance audit."
)
