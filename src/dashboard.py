"""
Interactive Melbourne Real Estate Intelligence & Valuation Dashboard (`src/dashboard.py`).

Built with Streamlit + PyDeck/Mapbox for spatial visualization:
- Renders an interactive map of Melbourne active listings color-coded by their
  Underquoting Risk Score (Red = Severe Underquoting Risk, Amber = Moderate Risk,
  Green = Accurate Guide, Cyan = Distressed Vendor / Price Cut Deal).
- Sidebar filters for Suburb, Property Type, Maximum Budget, First-Home Buyer status,
  and Available Upfront Savings.
- Displays a live Comparable Sales Matrix pulling the nearest Valuer-General
  historical settled properties for any selected listing.
- Integrates the Victorian Financial & First-Home Buyer Calculation Engine
  (Land Transfer Duty, $950k FHG 5% deposit check, and Cash-to-Close breakdown).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

# Ensure project root is on sys.path when invoked via `streamlit run src/dashboard.py`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import importlib
import pandas as pd
import pydeck as pdk
import streamlit as st

import src.etl_pipeline as etl_module
import src.train_avm as avm_module

importlib.reload(etl_module)
importlib.reload(avm_module)

from src.alerts import MelbournePropertyAlertEngine, PropertyAlertCriteria
from src.etl_pipeline import (
    DomainAPIClient,
    PublicSuburbPortalClient,
    RealWorldCSVConnector,
    generate_melbourne_market_datasets,
)
from src.financial_calculator import VictorianPropertyFinance
from src.train_avm import AVMTrainingMetrics, MelbourneAVMEngine


@st.cache_data(show_spinner=False)
def load_and_score_market_data(
    schema_version: int = 4,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, AVMTrainingMetrics]:
    """Load Melbourne suburbs, settled sales, and active listings scored by the XGBoost AVM."""
    suburbs_df, listings_df, settled_sales_df = generate_melbourne_market_datasets()
    avm_engine = MelbourneAVMEngine()
    metrics = avm_engine.train(settled_sales_df)
    scored_listings_df = avm_engine.score_active_listings(listings_df)
    return suburbs_df, scored_listings_df, settled_sales_df, metrics


def ensure_issue_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee `property_issue_heading` and `property_issue_details` exist even on cached DataFrames."""
    if "property_issue_heading" in df.columns and "property_issue_details" in df.columns:
        return df
    out = df.copy()
    headings, details = [], []
    for _, row in out.iterrows():
        h, d = MelbourneAVMEngine.detect_property_red_flags(row)
        headings.append(h)
        details.append(d)
    out["property_issue_heading"] = headings
    out["property_issue_details"] = details
    return out


def render_dashboard() -> None:
    """Main entry point for the Streamlit spatial valuation & underquoting dashboard."""
    st.set_page_config(
        page_title="Melbourne Real Estate Intelligence & AVM Platform",
        page_icon="🏙️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("🏙️ Melbourne Real Estate Intelligence & Property Valuation Platform")
    st.caption(
        "PostGIS Spatial Architecture • XGBoost Automated Valuation Model (AVM) • "
        "Underquoting Detector • SRO Victoria Stamp Duty & First-Home Buyer Engine"
    )

    suburbs_df, scored_listings_df, settled_sales_df, avm_metrics = load_and_score_market_data()

    # -------------------------------------------------------------------------
    # Sidebar: Real-World Data Connector (Zero-Key Public Suburb Fetch, CSV & API)
    # -------------------------------------------------------------------------
    with st.sidebar.expander("📡 Live Melbourne Data Sources", expanded=True):
        data_mode = st.radio(
            "Select Ingestion Source:",
            options=[
                "Built-in Multi-Suburb Dataset",
                "🌐 Live Suburb Fetch (No API Key / No ABN)",
                "📂 Upload Custom CSVs (VGVR / Listings)",
                "🔑 Domain Developer API (With Key)",
            ],
        )

        if data_mode == "🌐 Live Suburb Fetch (No API Key / No ABN)":
            st.caption(
                "Pulls real-time active listings, price guides, coordinates, and recent "
                "settled sales directly from public Victorian suburb feeds."
            )
            preset_suburbs = {
                "Parkville (3052)": ("Parkville", "3052"),
                "Carlton (3053)": ("Carlton", "3053"),
                "Clayton (3168)": ("Clayton", "3168"),
                "Hawthorn (3122)": ("Hawthorn", "3122"),
                "South Melbourne (3205)": ("South Melbourne", "3205"),
                "St Kilda (3182)": ("St Kilda", "3182"),
                "Brighton (3186)": ("Brighton", "3186"),
                "Port Melbourne (3207)": ("Port Melbourne", "3207"),
                "Docklands (3008)": ("Docklands", "3008"),
                "South Yarra (3141)": ("South Yarra", "3141"),
                "Richmond (3121)": ("Richmond", "3121"),
                "Brunswick (3056)": ("Brunswick", "3056"),
                "Preston (3072)": ("Preston", "3072"),
                "Reservoir (3073)": ("Reservoir", "3073"),
                "Footscray (3011)": ("Footscray", "3011"),
                "Coburg (3058)": ("Coburg", "3058"),
                "Yarraville (3013)": ("Yarraville", "3013"),
                "Bentleigh East (3165)": ("Bentleigh East", "3165"),
                "Ringwood (3134)": ("Ringwood", "3134"),
                "Frankston (3199)": ("Frankston", "3199"),
                "Sunshine West (3020)": ("Sunshine West", "3020"),
                "Custom Melbourne Suburb...": ("Custom", ""),
            }
            chosen_preset = st.selectbox("Target Melbourne Suburb:", list(preset_suburbs.keys()))
            if chosen_preset == "Custom Melbourne Suburb...":
                col_sub, col_pc = st.columns(2)
                raw_sub = col_sub.text_input("Suburb Name", value="Northcote")
                raw_pc = col_pc.text_input(
                    "Postcode (optional if known)",
                    value="3070",
                    help="Leave blank to auto-detect for 70+ Melbourne suburbs",
                )
                live_sub, live_pc = PublicSuburbPortalClient.resolve_suburb_postcode(
                    raw_sub, raw_pc
                )
                st.caption(f"Resolved Target: **{live_sub} VIC {live_pc}**")
            else:
                live_sub, live_pc = preset_suburbs[chosen_preset]

            pages_to_fetch = st.slider("Pages to Fetch (~25 listings/page)", 1, 6, 3)

            if st.button("🚀 Fetch Live Suburb Listings & Sold Comps", width="stretch"):
                with st.spinner(f"Fetching live market data for {live_sub} VIC {live_pc}..."):
                    try:
                        live_active_df, live_sold_df = PublicSuburbPortalClient.fetch_live_suburb_market(
                            suburb=live_sub, postcode=live_pc, max_pages=pages_to_fetch
                        )
                        if not live_sold_df.empty and len(live_sold_df) >= 10:
                            combined_sales_df = pd.concat(
                                [live_sold_df, settled_sales_df], ignore_index=True
                            )
                        else:
                            combined_sales_df = settled_sales_df

                        if not live_active_df.empty:
                            avm_live = MelbourneAVMEngine()
                            avm_metrics = avm_live.train(combined_sales_df)
                            scored_listings_df = avm_live.score_active_listings(live_active_df)
                            if not live_sold_df.empty:
                                settled_sales_df = pd.concat(
                                    [live_sold_df, settled_sales_df], ignore_index=True
                                )
                            st.session_state["live_scored_df"] = scored_listings_df
                            st.session_state["live_sales_df"] = settled_sales_df
                            st.session_state["live_metrics"] = avm_metrics
                            # Reset or update the multiselect filter so the newly fetched custom suburb is automatically selected!
                            fetched_suburbs = sorted(scored_listings_df["suburb_name"].unique().tolist())
                            st.session_state["suburb_multiselect"] = fetched_suburbs
                            st.success(
                                f"Loaded {len(scored_listings_df)} live active listings and "
                                f"{len(live_sold_df)} real sold comps in {live_sub} ({live_pc})!"
                            )
                        else:
                            st.warning(
                                f"No active listings found for '{live_sub} ({live_pc})'. "
                                "Check the suburb spelling and 4-digit Victorian postcode."
                            )
                    except Exception as exc:
                        st.error(f"Live suburb fetch failed: {exc}")

            if "live_scored_df" in st.session_state:
                scored_listings_df = st.session_state["live_scored_df"]
                settled_sales_df = st.session_state["live_sales_df"]
                avm_metrics = st.session_state["live_metrics"]

        elif data_mode == "📂 Upload Custom CSVs (VGVR / Listings)":
            uploaded_sales = st.file_uploader(
                "1. Settled Sales CSV (VGVR / Data.Vic)", type=["csv"]
            )
            uploaded_listings = st.file_uploader(
                "2. Active Listings CSV", type=["csv"]
            )
            if uploaded_sales is not None:
                settled_sales_df = RealWorldCSVConnector.load_settled_sales_csv(uploaded_sales)
                st.success(f"Loaded {len(settled_sales_df)} settled sales records.")
            if uploaded_listings is not None:
                raw_active_df = RealWorldCSVConnector.load_active_listings_csv(uploaded_listings)
                avm_live = MelbourneAVMEngine()
                avm_metrics = avm_live.train(settled_sales_df)
                scored_listings_df = avm_live.score_active_listings(raw_active_df)
                st.success(f"Scored {len(scored_listings_df)} uploaded listings with AVM!")

        elif data_mode == "🔑 Domain Developer API (With Key)":
            domain_key_input = st.text_input(
                "Domain Developer API Key",
                type="password",
                help="Create an API key at developer.domain.com.au",
            )
            col_sub, col_pc = st.columns(2)
            target_suburb = col_sub.text_input("Suburb", value="Brunswick")
            target_pcode = col_pc.text_input("Postcode", value="3056")
            if st.button("Fetch Live Domain Listings", width="stretch"):
                try:
                    client = DomainAPIClient(api_key=domain_key_input)
                    live_df = client.fetch_suburb_listings(
                        suburb=target_suburb, postcode=target_pcode
                    )
                    if not live_df.empty:
                        avm_live = MelbourneAVMEngine()
                        avm_metrics = avm_live.train(settled_sales_df)
                        scored_listings_df = avm_live.score_active_listings(live_df)
                        st.success(
                            f"Fetched & scored {len(scored_listings_df)} live listings in {target_suburb}!"
                        )
                    else:
                        st.warning("No priced active listings returned for that suburb.")
                except Exception as exc:
                    st.error(f"Domain API error: {exc}")

    # -------------------------------------------------------------------------
    # Sidebar: Filters & First-Home Buyer Financial Parameters
    # -------------------------------------------------------------------------
    st.sidebar.header("🔍 Spatial & Budget Filters")

    all_suburbs = sorted(scored_listings_df["suburb_name"].unique().tolist())
    # Ensure session_state multiselect only contains valid current options
    if "suburb_multiselect" in st.session_state:
        valid_sel = [s for s in st.session_state["suburb_multiselect"] if s in all_suburbs]
        st.session_state["suburb_multiselect"] = valid_sel or all_suburbs
    selected_suburbs = st.sidebar.multiselect(
        "Filter by Melbourne Suburb(s)",
        options=all_suburbs,
        default=all_suburbs,
        key="suburb_multiselect",
    )

    all_types = sorted(scored_listings_df["property_type"].unique().tolist())
    selected_types = st.sidebar.multiselect(
        "Property Type",
        options=all_types,
        default=all_types,
    )

    max_budget = st.sidebar.slider(
        "Maximum Target Budget ($ AUD)",
        min_value=450_000,
        max_value=4_500_000,
        value=2_500_000,
        step=50_000,
        format="$%d",
    )

    risk_filter = st.sidebar.multiselect(
        "Underquoting & Seller Motivation Classification",
        options=[
            "Distressed / Urgent Seller Deal",
            "Low Risk (Accurate Guide)",
            "Moderate Underquoting Risk",
            "High Underquoting Risk",
        ],
        default=[
            "Distressed / Urgent Seller Deal",
            "Low Risk (Accurate Guide)",
            "Moderate Underquoting Risk",
            "High Underquoting Risk",
        ],
    )

    st.sidebar.markdown("---")
    st.sidebar.header("🏦 Buyer Financial Profile (VIC)")
    is_fhb = st.sidebar.checkbox("Eligible First-Home Buyer (VIC)", value=True)
    available_savings = st.sidebar.number_input(
        "Available Upfront Savings ($ AUD)",
        min_value=10_000,
        max_value=1_500_000,
        value=110_000,
        step=5_000,
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("🤖 AVM Model Diagnostics")
    st.sidebar.write(f"**Engine:** {avm_metrics.model_backend}")
    st.sidebar.write(f"**Validation R²:** `{avm_metrics.r2:.3f}`")
    st.sidebar.write(f"**Validation MAPE:** `{avm_metrics.mape_pct:.2f}%`")
    st.sidebar.write(f"**Validation MAE:** `${avm_metrics.mae_aud:,.0f}`")

    # Apply filters
    scored_listings_df = ensure_issue_columns(scored_listings_df)
    filtered_df = scored_listings_df[
        (scored_listings_df["suburb_name"].isin(selected_suburbs))
        & (scored_listings_df["property_type"].isin(selected_types))
        & (scored_listings_df["price_guide_high"] <= max_budget)
        & (scored_listings_df["underquoting_risk_level"].isin(risk_filter))
    ].copy()

    # -------------------------------------------------------------------------
    # Top KPI Ribbon
    # -------------------------------------------------------------------------
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Active Filtered Listings", f"{len(filtered_df)}")
    high_risk_count = int(
        (filtered_df["underquoting_risk_level"] == "High Underquoting Risk").sum()
    )
    kpi2.metric("Flagged High Underquoting Risk", f"{high_risk_count}")
    distressed_count = int(
        (filtered_df["underquoting_risk_level"] == "Distressed / Urgent Seller Deal").sum()
    )
    kpi3.metric("💎 Urgent Seller / Price-Cut Deals", f"{distressed_count}")
    fhg_eligible_count = int((filtered_df["price_guide_high"] <= 950_000).sum())
    kpi4.metric("FHG 5% Deposit Eligible (≤$950k)", f"{fhg_eligible_count}")

    if filtered_df.empty:
        st.warning("No active listings match your current suburb and budget filters. Try widening the criteria.")
        return

    # -------------------------------------------------------------------------
    # Interactive Melbourne Spatial Map (PyDeck) & Legend
    # -------------------------------------------------------------------------
    st.subheader("🗺️ Live Melbourne Spatial Valuation & Underquoting Risk Map")
    st.markdown(
        "**Legend:** "
        "🔴 **High Underquoting Risk** (>11% below AVM) &nbsp;|&nbsp; "
        "🟠 **Moderate Underquoting Risk** (4.5%–11% below AVM) &nbsp;|&nbsp; "
        "🟢 **Low Risk (Accurate Guide)** &nbsp;|&nbsp; "
        "🔵 **Distressed / Price-Cut Opportunity**"
    )

    mid_lat = float(filtered_df["latitude"].mean())
    mid_lon = float(filtered_df["longitude"].mean())

    scatter_layer = pdk.Layer(
        "ScatterplotLayer",
        data=filtered_df,
        get_position=["longitude", "latitude"],
        get_fill_color="risk_rgb",
        get_radius=190,
        radius_min_pixels=7,
        radius_max_pixels=22,
        pickable=True,
        stroked=True,
        get_line_color=[255, 255, 255, 200],
        line_width_min_pixels=1.5,
    )

    tooltip = {
        "html": (
            "<b>{normalized_address}</b><br/>"
            "Type: {property_type} ({bedrooms}BR / {bathrooms}BA / {land_size_sqm} sqm)<br/>"
            "Agent Guide: <b>{price_guide_raw}</b><br/>"
            "AVM Fair Value: <b>${avm_estimated_value}</b><br/>"
            "Underquoting Gap: <b>{underquoting_gap_pct}%</b> ({underquoting_risk_level})<br/>"
            "Issue Flag: <b>{property_issue_heading}</b><br/>"
            "Agency: {agency_name} ({agent_name})"
        ),
        "style": {
            "backgroundColor": "#0F172A",
            "color": "#F8FAFC",
            "fontSize": "12px",
            "padding": "10px",
            "borderRadius": "6px",
        },
    }

    deck = pdk.Deck(
        layers=[scatter_layer],
        initial_view_state=pdk.ViewState(
            latitude=mid_lat,
            longitude=mid_lon,
            zoom=10.8,
            pitch=35,
        ),
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip=tooltip,
    )
    st.pydeck_chart(deck, width="stretch")

    # -------------------------------------------------------------------------
    # Active Listings Table with AVM & Underquoting Risk Scores
    # -------------------------------------------------------------------------
    st.subheader("📋 Scored Active Listings & Underquoting Audit")
    display_cols = [
        "listing_id",
        "normalized_address",
        "suburb_name",
        "property_type",
        "sale_method",
        "bedrooms",
        "bathrooms",
        "land_size_sqm",
        "price_guide_raw",
        "avm_estimated_value",
        "underquoting_gap_pct",
        "underquoting_risk_level",
        "property_issue_heading",
        "days_on_market",
        "price_revisions_count",
        "agency_name",
    ]
    st.dataframe(
        filtered_df.sort_values("underquoting_risk_score", ascending=False)[display_cols],
        width="stretch",
        hide_index=True,
    )

    # -------------------------------------------------------------------------
    # Deep-Dive Inspector: Comparable Sales Matrix & Cash-to-Close Calculator
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("🔎 Property Deep-Dive: Comparable Settled Sales Matrix & Cash-to-Close")

    listing_options = {
        f"{row['listing_id']} — {row['normalized_address']} ({row['price_guide_raw']})": row["listing_id"]
        for _, row in filtered_df.iterrows()
    }
    selected_label = st.selectbox(
        "Select an Active Listing to Inspect Comparable Sales & Victorian Stamp Duty:",
        options=list(listing_options.keys()),
    )
    selected_id = listing_options[selected_label]
    selected_row = filtered_df[filtered_df["listing_id"] == selected_id].iloc[0]

    col_left, col_right = st.columns([1.05, 1.25])

    with col_left:
        st.markdown(f"#### 🏛️ Victorian Cash-to-Close & Stamp Duty Analysis")
        st.write(
            f"**Selected Property:** `{selected_row['normalized_address']}` "
            f"({selected_row['property_type']}, {int(selected_row['bedrooms'])} Bed / "
            f"{int(selected_row['bathrooms'])} Bath / {float(selected_row['land_size_sqm']):.0f} sqm)"
        )
        st.info(selected_row["underquoting_explanation"])
        if "✅" in str(selected_row.get("property_issue_heading", "")):
            st.success(
                f"**Property Due-Diligence Check ({selected_row['property_issue_heading']}):** "
                f"{selected_row.get('property_issue_details', '')}"
            )
        else:
            st.warning(
                f"**⚠️ Potential Property Issue Detected — `{selected_row['property_issue_heading']}`**\n\n"
                f"{selected_row.get('property_issue_details', '')}"
            )

        eval_price_mode = st.radio(
            "Evaluate Acquisition Price At:",
            options=[
                f"AVM True Fair Value (${selected_row['avm_estimated_value']:,.0f})",
                f"Agent Guide High (${selected_row['price_guide_high']:,.0f})",
            ],
            horizontal=True,
        )
        target_purchase_price = (
            float(selected_row["avm_estimated_value"])
            if "AVM" in eval_price_mode
            else float(selected_row["price_guide_high"])
        )

        cash_summary = VictorianPropertyFinance.calculate_cash_to_close(
            property_price=target_purchase_price,
            available_savings=float(available_savings),
            is_first_home_buyer=is_fhb,
        )

        fin_df = pd.DataFrame(
            [
                ("Target Purchase Price", f"${cash_summary.property_price:,.2f}"),
                (
                    "Federal First Home Guarantee (FHG ≤ $950k)",
                    "✅ Eligible (5% Deposit, $0 LMI)"
                    if cash_summary.fhg_scheme_applied
                    else "❌ Standard 20% Deposit Required",
                ),
                (
                    f"Minimum Required Deposit ({cash_summary.required_deposit_pct:.0f}%)",
                    f"${cash_summary.minimum_deposit:,.2f}",
                ),
                ("Victorian Land Transfer Duty (Stamp Duty)", f"${cash_summary.payable_stamp_duty:,.2f}"),
                ("First-Home Buyer Stamp Duty Concession Saved", f"${cash_summary.fhb_duty_savings:,.2f}"),
                ("Legal & Conveyancing Fees", f"${cash_summary.conveyancing_legal_fee:,.2f}"),
                ("Building & Pest Inspection", f"${cash_summary.building_pest_inspection_fee:,.2f}"),
                ("Total Cash-to-Close Required", f"${cash_summary.total_cash_to_close:,.2f}"),
                ("Your Available Upfront Savings", f"${cash_summary.available_savings:,.2f}"),
                (
                    "Upfront Savings Surplus / (Shortfall)",
                    f"${cash_summary.savings_surplus_or_shortfall:+,.2f}",
                ),
            ],
            columns=["Financial Component", "Amount / Status"],
        )
        st.table(fin_df)

    with col_right:
        st.markdown("#### 📐 Nearest Comparable Settled Sales (Valuer-General Victoria)")
        comps_df = MelbourneAVMEngine.find_comparable_settled_sales(
            target_listing=selected_row,
            settled_sales_df=settled_sales_df,
            top_k=6,
        )
        st.dataframe(comps_df, width="stretch", hide_index=True)

        avg_comp_price = float(comps_df["sold_price"].mean())
        avg_comp_sqm = float(comps_df["price_per_sqm"].mean())
        st.caption(
            f"**Comparable Matrix Summary:** Nearest 6 settled sales average "
            f"**${avg_comp_price:,.0f}** (mean land rate **${avg_comp_sqm:,.0f}/sqm**) "
            f"vs AVM Fair Value of **${selected_row['avm_estimated_value']:,.0f}**."
        )

        st.markdown("#### 🔔 Automated High-Value / Low-Risk Alert Feed")
        alert_engine = MelbournePropertyAlertEngine(
            criteria=PropertyAlertCriteria(
                max_budget_aud=float(max_budget),
                is_first_home_buyer=is_fhb,
                available_savings_aud=float(available_savings),
            )
        )
        matching_alerts = alert_engine.evaluate_listings(filtered_df)
        if matching_alerts:
            alert_rows = [
                {
                    "Listing ID": a.listing_id,
                    "Address": a.normalized_address,
                    "Guide High": f"${a.price_guide_high:,.0f}",
                    "AVM Value": f"${a.avm_fair_value:,.0f}",
                    "Cash-to-Close": f"${a.total_cash_to_close:,.0f}",
                    "Signal": a.alert_reason,
                }
                for a in matching_alerts[:5]
            ]
            st.dataframe(pd.DataFrame(alert_rows), width="stretch", hide_index=True)
        else:
            st.write("No active listings in the current filter trigger low-risk deal alerts.")


if __name__ == "__main__":
    render_dashboard()

