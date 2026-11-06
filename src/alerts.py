"""
Automated Property Deal & Distressed Vendor Alert Engine (`src/alerts.py`).

Evaluates newly ingested or price-adjusted Melbourne listings against customizable
investment / First-Home Buyer criteria:
1. Within target budget (default: $950,000 Melbourne FHG cap)
2. Low underquoting risk or verified vendor price cuts (distressed seller signal)
3. Quoted below or near the AVM True Fair Value with full Cash-to-Close breakdown

Dispatches structured JSON notifications to a configured webhook (Slack, Discord,
Teams, or custom HTTP endpoint) and falls back cleanly to structured local logging.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import requests

from config.settings import settings
from src.etl_pipeline import generate_melbourne_market_datasets
from src.financial_calculator import VictorianPropertyFinance
from src.train_avm import MelbourneAVMEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PropertyAlertCriteria:
    """Filtering rules for high-value, low-risk Melbourne property alerts."""

    max_budget_aud: float = 950_000.0
    max_underquoting_gap_pct: float = 4.5
    min_bedrooms: int = 2
    target_suburbs: Optional[List[str]] = None
    is_first_home_buyer: bool = True
    available_savings_aud: float = 95_000.0
    prioritize_price_cuts: bool = True


@dataclass(frozen=True)
class TriggeredPropertyAlert:
    """Structured alert payload for a qualifying high-value, low-risk listing."""

    listing_id: str
    normalized_address: str
    suburb_name: str
    postcode: str
    property_type: str
    bedrooms: int
    bathrooms: int
    land_size_sqm: float
    price_guide_raw: str
    price_guide_high: float
    avm_fair_value: float
    underquoting_gap_pct: float
    underquoting_risk_level: str
    price_revisions_count: int
    last_price_cut_pct: float
    fhg_5pct_eligible: bool
    payable_stamp_duty: float
    total_cash_to_close: float
    alert_reason: str
    triggered_at_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MelbournePropertyAlertEngine:
    """
    Automated alerting service that scans scored listings for high-value,
    low-underquoting-risk opportunities and dispatches webhook/logger notifications.
    """

    def __init__(
        self,
        criteria: Optional[PropertyAlertCriteria] = None,
        webhook_url: Optional[str] = None,
    ) -> None:
        self.criteria = criteria or PropertyAlertCriteria(
            max_budget_aud=settings.ALERT_MAX_BUDGET,
        )
        self.webhook_url = (
            webhook_url if webhook_url is not None else settings.ALERT_WEBHOOK_URL
        )

    def evaluate_listings(
        self, scored_listings_df: pd.DataFrame
    ) -> List[TriggeredPropertyAlert]:
        """
        Filter scored active listings against the alert criteria and enrich with
        Victorian Stamp Duty, FHG eligibility, and Cash-to-Close metrics.
        """
        alerts: List[TriggeredPropertyAlert] = []
        crit = self.criteria

        for _, row in scored_listings_df.iterrows():
            guide_high = float(row["price_guide_high"])
            avm_val = float(row["avm_estimated_value"])
            gap_pct = float(row["underquoting_gap_pct"])
            beds = int(row["bedrooms"])
            suburb = str(row["suburb_name"])
            revisions = int(row.get("price_revisions_count", 0))
            last_cut = float(row.get("last_price_cut_pct", 0.0))

            if guide_high > crit.max_budget_aud:
                continue
            if beds < crit.min_bedrooms:
                continue
            if crit.target_suburbs and suburb not in crit.target_suburbs:
                continue
            if gap_pct > crit.max_underquoting_gap_pct:
                continue

            cash_summary = VictorianPropertyFinance.calculate_cash_to_close(
                property_price=guide_high,
                available_savings=crit.available_savings_aud,
                is_first_home_buyer=crit.is_first_home_buyer,
            )

            reasons: List[str] = []
            if revisions > 0 and last_cut < 0:
                reasons.append(
                    f"Distressed Vendor Signal ({revisions}x price cut, {last_cut:.1f}%)"
                )
            if guide_high <= avm_val:
                discount_aud = avm_val - guide_high
                reasons.append(f"Quoted ${discount_aud:,.0f} below AVM Fair Value")
            else:
                reasons.append("Accurate Agent Guide (Low Underquoting Risk)")
            if cash_summary.fhg_scheme_applied:
                reasons.append("Eligible for 5% Deposit FHG ($0 LMI)")

            alerts.append(
                TriggeredPropertyAlert(
                    listing_id=str(row.get("listing_id", "")),
                    normalized_address=str(row["normalized_address"]),
                    suburb_name=suburb,
                    postcode=str(row["postcode"]),
                    property_type=str(row["property_type"]),
                    bedrooms=beds,
                    bathrooms=int(row["bathrooms"]),
                    land_size_sqm=float(row["land_size_sqm"]),
                    price_guide_raw=str(row["price_guide_raw"]),
                    price_guide_high=guide_high,
                    avm_fair_value=avm_val,
                    underquoting_gap_pct=gap_pct,
                    underquoting_risk_level=str(row["underquoting_risk_level"]),
                    price_revisions_count=revisions,
                    last_price_cut_pct=last_cut,
                    fhg_5pct_eligible=cash_summary.fhg_scheme_applied,
                    payable_stamp_duty=cash_summary.payable_stamp_duty,
                    total_cash_to_close=cash_summary.total_cash_to_close,
                    alert_reason=" | ".join(reasons),
                    triggered_at_utc=datetime.now(timezone.utc).isoformat(),
                )
            )

        # Sort by distressed price cuts first, then by lowest underquoting gap
        alerts.sort(
            key=lambda a: (-a.price_revisions_count, a.underquoting_gap_pct)
        )
        return alerts

    def dispatch_alerts(
        self, alerts: List[TriggeredPropertyAlert]
    ) -> Dict[str, Any]:
        """
        Send triggered property alerts to the configured webhook URL (if provided)
        and always emit structured alert logs to the local logger.
        """
        dispatched_count = 0
        webhook_status = "local_logger_only"

        for alert in alerts:
            log_msg = (
                f"[HIGH-VALUE LISTING ALERT] {alert.normalized_address} ({alert.property_type}, "
                f"{alert.bedrooms}BR) | Guide: {alert.price_guide_raw} vs AVM: ${alert.avm_fair_value:,.0f} "
                f"| Cash-to-Close: ${alert.total_cash_to_close:,.0f} | Reason: {alert.alert_reason}"
            )
            logger.info(log_msg)
            dispatched_count += 1

        if self.webhook_url and alerts:
            payload = {
                "platform": "Melbourne Real Estate Intelligence Platform",
                "alert_count": len(alerts),
                "listings": [a.to_dict() for a in alerts[:10]],
            }
            try:
                response = requests.post(
                    self.webhook_url,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                    timeout=8,
                )
                response.raise_for_status()
                webhook_status = f"delivered_http_{response.status_code}"
            except Exception as exc:
                logger.warning("Webhook delivery failed (%s); logged locally.", exc)
                webhook_status = "webhook_failed_logged_locally"

        return {
            "alerts_triggered": dispatched_count,
            "delivery_mode": webhook_status,
            "top_alert": alerts[0].to_dict() if alerts else None,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    _, active_df, sales_df = generate_melbourne_market_datasets()
    avm_engine = MelbourneAVMEngine()
    avm_engine.train(sales_df)
    scored = avm_engine.score_active_listings(active_df)

    alert_engine = MelbournePropertyAlertEngine()
    matched_alerts = alert_engine.evaluate_listings(scored)
    summary = alert_engine.dispatch_alerts(matched_alerts)
    print(f"Alert Scan Summary: {summary['alerts_triggered']} qualifying listings found.")

