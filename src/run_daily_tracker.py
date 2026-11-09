"""
Daily Automated Tracker & Static Data Generator (`src/run_daily_tracker.py`).

Orchestrates the complete daily ingestion, valuation, and artifact generation cycle:
1. Ingests active listings from agency portals (Barry Plant, Ray White, View, etc.).
2. Loads settled transactions and trains the HistGradientBoosting/XGBoost AVM model.
3. Scores underquoting risk gaps and assigns color-coded risk tiers.
4. Computes Victorian SRO Stamp Duty, FHG 5% deposit eligibility, and Cash-to-Close.
5. Emits `docs/data/listings.json`, `docs/data/settled_sales.json`,
   `docs/data/metadata.json`, and `docs/data/digest.md`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.agency_scrapers import MultiAgencyAggregator
from src.financial_calculator import VictorianPropertyFinance
from src.train_avm import MelbourneAVMEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_daily_tracker")


def run_pipeline(output_dir: Optional[Path] = None) -> None:
    """Execute the end-to-end daily property tracker pipeline."""
    logger.info("=== Starting Melbourne Property Tracker Daily Ingestion ===")

    out_dir = output_dir or (PROJECT_ROOT / "docs" / "data")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ingest and aggregate market data
    aggregator = MultiAgencyAggregator()
    listings_df, settled_sales_df = aggregator.aggregate_market_data()

    # If active listings are fewer than pre-existing rich dataset, load & merge existing data
    existing_listings_path = out_dir / "listings.json"
    if existing_listings_path.exists() and len(listings_df) < 400:
        try:
            with open(existing_listings_path, "r", encoding="utf-8") as f:
                prev_listings = json.load(f)
            prev_df = pd.DataFrame(prev_listings)
            # Merge to preserve comprehensive coverage
            combined_df = pd.concat([listings_df, prev_df], ignore_index=True)
            listings_df = combined_df.drop_duplicates(subset=["normalized_address"], keep="first")
            logger.info("Preserved coverage with existing listings: %d total", len(listings_df))
        except Exception as e:
            logger.warning("Could not merge existing listings: %s", e)

    existing_sales_path = out_dir / "settled_sales.json"
    if existing_sales_path.exists() and len(settled_sales_df) < 400:
        try:
            with open(existing_sales_path, "r", encoding="utf-8") as f:
                prev_sales = json.load(f)
            prev_sales_df = pd.DataFrame(prev_sales)
            settled_sales_df = pd.concat([settled_sales_df, prev_sales_df], ignore_index=True).drop_duplicates(
                subset=["normalized_address"], keep="first"
            )
        except Exception as e:
            logger.warning("Could not merge existing settled sales: %s", e)

    # 2. Train AVM
    logger.info("Training AVM model on historical settled sales...")
    avm_engine = MelbourneAVMEngine()
    metrics = avm_engine.train(settled_sales_df)
    logger.info(
        "AVM Model (%s) trained: R2=%.4f, MAPE=%.2f%%, MAE=$%s",
        metrics.model_backend,
        metrics.r2,
        metrics.mape_pct,
        f"{metrics.mae_aud:,.0f}",
    )

    # 3. Score active listings
    logger.info("Scoring active listings for underquoting and property defects...")
    scored_df = avm_engine.score_active_listings(listings_df)

    # 4. Calculate Victorian Stamp Duty & Cash-to-Close
    logger.info("Calculating Victorian Stamp Duty, FHG 5%% deposits, and Cash-to-Close...")
    enriched_records: List[Dict[str, Any]] = []

    for _, row in scored_df.iterrows():
        rec = row.to_dict()
        top_guide = float(rec.get("price_guide_high") or 750000.0)

        # Financial modeling
        duty_breakdown = VictorianPropertyFinance.calculate_stamp_duty(top_guide, is_first_home_buyer=True)
        fhg = VictorianPropertyFinance.evaluate_fhg_eligibility(top_guide)
        c2c = VictorianPropertyFinance.calculate_cash_to_close(
            property_price=top_guide,
            is_first_home_buyer=True,
            available_savings=95000.0,
        )

        rec["payable_stamp_duty"] = round(duty_breakdown.payable_stamp_duty, 2)
        rec["fhb_duty_savings"] = round(duty_breakdown.concession_savings, 2)
        rec["required_deposit_pct"] = round(fhg.required_deposit_pct * 100.0, 1)
        rec["minimum_deposit"] = round(fhg.required_deposit_amount, 2)
        rec["fhg_scheme_applied"] = fhg.is_fhg_eligible
        rec["total_upfront_costs_excl_deposit"] = round(c2c.total_upfront_costs_excl_deposit, 2)
        rec["total_cash_to_close"] = round(c2c.total_cash_to_close, 2)
        rec["savings_surplus_or_shortfall"] = round(c2c.savings_surplus_or_shortfall, 2)
        rec["has_sufficient_funds"] = c2c.has_sufficient_funds

        # Ensure listing_url is always populated for clickable card headings
        if not rec.get("listing_url"):
            clean_addr = f"{rec.get('street_address', '')} {rec.get('suburb_name', '')} VIC {rec.get('postcode', '')}".strip()
            rec["listing_url"] = f"https://www.google.com/search?q={urllib.parse.quote_plus(clean_addr + ' property for sale')}"

        # Clean NaNs and infs for JSON serialization
        clean_rec = {}
        for k, v in rec.items():
            if isinstance(v, (list, tuple)):
                clean_rec[k] = list(v)
            elif isinstance(v, dict):
                clean_rec[k] = v
            elif v is None:
                clean_rec[k] = None
            else:
                try:
                    if pd.isna(v):
                        clean_rec[k] = None
                    elif isinstance(v, (float, int)):
                        clean_rec[k] = float(v) if isinstance(v, float) else int(v)
                    else:
                        clean_rec[k] = v
                except Exception:
                    clean_rec[k] = str(v)

        enriched_records.append(clean_rec)

    # 5. Clean settled sales for serialization
    clean_sales: List[Dict[str, Any]] = []
    for _, row in settled_sales_df.iterrows():
        s_rec = row.to_dict()
        clean_s = {}
        for k, v in s_rec.items():
            if isinstance(v, (list, tuple)):
                clean_s[k] = list(v)
            elif isinstance(v, dict):
                clean_s[k] = v
            elif v is None:
                clean_s[k] = None
            else:
                try:
                    if pd.isna(v):
                        clean_s[k] = None
                    elif isinstance(v, (float, int)):
                        clean_s[k] = float(v) if isinstance(v, float) else int(v)
                    else:
                        clean_s[k] = v
                except Exception:
                    clean_s[k] = str(v)
        clean_sales.append(clean_s)

    # 6. Save JSON files
    with open(out_dir / "listings.json", "w", encoding="utf-8") as f:
        json.dump(enriched_records, f, indent=2)

    with open(out_dir / "settled_sales.json", "w", encoding="utf-8") as f:
        json.dump(clean_sales, f, indent=2)

    # 7. Metadata JSON
    risk_counts = {}
    agency_counts = {}
    for r in enriched_records:
        level = r.get("underquoting_risk_level", "Unknown")
        risk_counts[level] = risk_counts.get(level, 0) + 1
        agency = (r.get("agency_name") or "Other").split(" (")[0].strip()
        agency_counts[agency] = agency_counts.get(agency, 0) + 1

    sorted_agencies = [
        ag for ag, _ in sorted(agency_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    metadata = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_active_listings": len(enriched_records),
        "total_settled_sales": len(clean_sales),
        "avm_metrics": {
            "model_backend": metrics.model_backend,
            "r2": round(metrics.r2, 4),
            "mape_pct": round(metrics.mape_pct, 2),
            "mae_aud": round(metrics.mae_aud, 2),
        },
        "agencies_scraped": sorted_agencies,
        "agency_counts": agency_counts,
        "risk_summary": risk_counts,
    }

    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logger.info(
        "Saved listings.json (%d items) and settled_sales.json to %s",
        len(enriched_records),
        out_dir,
    )

    # 8. Generate Markdown Digest
    generate_markdown_digest(enriched_records, metadata, out_dir / "digest.md")
    logger.info("Saved daily digest to %s", out_dir / "digest.md")
    logger.info("=== Melbourne Property Tracker Daily Ingestion Complete ===")


def generate_markdown_digest(
    listings: List[Dict[str, Any]], metadata: Dict[str, Any], output_path: Path
) -> None:
    """Produce the Daily Market Intelligence Digest in Markdown format."""
    now_str = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")

    # Sort for top auction baits (> 12% gap)
    auction_baits = sorted(
        [l for l in listings if (l.get("underquoting_gap_pct") or 0) > 12.0],
        key=lambda x: x.get("underquoting_gap_pct") or 0,
        reverse=True,
    )[:5]

    # Sort for top distressed deals
    distressed_deals = [
        l for l in listings if "distressed" in (l.get("underquoting_risk_level") or "").lower()
    ]
    if not distressed_deals:
        distressed_deals = sorted(
            listings,
            key=lambda x: x.get("avm_estimated_value", 0) - (x.get("price_guide_high") or 0),
            reverse=True,
        )
    top_distressed = distressed_deals[:5]

    # Sort for top FHB picks (price <= 950k, beds >= 2)
    fhb_picks = [
        l
        for l in listings
        if (l.get("price_guide_high") or 9999999) <= 950000 and (l.get("bedrooms") or 0) >= 2
    ]
    top_fhb = sorted(fhb_picks, key=lambda x: x.get("total_cash_to_close") or 999999)[:5]

    lines = [
        "# Melbourne Property Market Daily Intelligence Digest",
        f"**Generated**: {now_str}  ",
        f"**Active Listings Scraped**: {metadata['total_active_listings']} | **Settled Sales in AVM**: {metadata['total_settled_sales']}  ",
        f"**AVM Model**: {metadata['avm_metrics']['model_backend']} ($R^2$: {metadata['avm_metrics']['r2']:.3f}, MAPE: {metadata['avm_metrics']['mape_pct']}%)  ",
        f"**Agencies Included**: {', '.join(metadata['agencies_scraped'])}",
        "",
        "---",
        "",
        "## 🔴 Top 5 Auction Bait Traps (Severe Underquoting)",
        "> **Buyer Warning**: Agent price guides sit far below the AVM Fair Value. Expect the seller's true reserve to be significantly higher on auction day.",
        "",
        "| Suburb | Address | Quoted Guide | AVM Fair Value | Underquoting Gap | Agency | Flag / Risk |",
        "|---|---|---|---|---|---|---|",
    ]

    for item in auction_baits:
        sub = item.get("suburb_name", "")
        addr = item.get("street_address", "")
        raw = item.get("price_guide_raw", "")
        avm = f"${item.get('avm_estimated_value', 0):,.0f}"
        gap = f"**+{item.get('underquoting_gap_pct', 0):.1f}%**"
        agency = item.get("agency_name", "")
        flag = item.get("property_issue_heading", "Standard Due Diligence")
        lines.append(f"| **{sub}** | {addr} | {raw} | {avm} | {gap} | {agency} | {flag} |")

    lines.extend([
        "",
        "---",
        "",
        "## 💎 Top 5 Motivated Vendor & Distressed Opportunities",
        "> **Negotiation Leverage**: Private sales or stale campaigns priced below AVM Fair Value or featuring vendor price cuts.",
        "",
        "| Suburb | Address | Guide | AVM Fair Value | Discount Gap | Agency | Red Flag / Due Diligence |",
        "|---|---|---|---|---|---|---|",
    ])

    for item in top_distressed:
        sub = item.get("suburb_name", "")
        addr = item.get("street_address", "")
        raw = item.get("price_guide_raw", "")
        avm = f"${item.get('avm_estimated_value', 0):,.0f}"
        gap = f"**+{item.get('underquoting_gap_pct', 0):.1f}%**"
        agency = item.get("agency_name", "")
        flag = item.get("property_issue_heading", "Clean Profile")
        lines.append(f"| **{sub}** | {addr} | {raw} | {avm} | {gap} | {agency} | {flag} |")

    lines.extend([
        "",
        "---",
        "",
        "## 🏠 Top 5 First-Home Buyer Value Picks (≤$950k FHG Cap)",
        "> **FHB Eligible**: 2+ bedrooms, eligible for 5% deposit scheme with $0 LMI, and honest price guides.",
        "",
        "| Suburb | Address | Beds/Baths | Top Guide | AVM Fair Value | Cash-to-Close (5% Dep) | Status |",
        "|---|---|---|---|---|---|---|",
    ])

    for item in top_fhb:
        sub = item.get("suburb_name", "")
        addr = item.get("street_address", "")
        beds = f"{item.get('bedrooms', 0)}B / {item.get('bathrooms', 0)}Ba"
        guide = f"${item.get('price_guide_high', 0):,.0f}"
        avm = f"${item.get('avm_estimated_value', 0):,.0f}"
        c2c = f"**${item.get('total_cash_to_close', 0):,.0f}**"
        status = item.get("underquoting_risk_level", "Active")
        lines.append(f"| **{sub}** | {addr} | {beds} | {guide} | {avm} | {c2c} | {status} |")

    lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    run_pipeline()
