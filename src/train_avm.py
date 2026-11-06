"""
Machine Learning Automated Valuation Model (AVM) & Underquoting Detector (`src/train_avm.py`).

Trains a gradient-boosted valuation model (XGBoost with automatic fallback to
Scikit-Learn HistGradientBoostingRegressor) on Valuer-General Victoria settled
sales features:
- Land size (sqm), bedrooms, bathrooms, car spaces, property type
- Spatial coordinates (latitude, longitude) and Haversine distance to Melbourne CBD

Computes "True Fair Value" for active listings and applies the Victorian
Underquoting Risk Flag algorithm:
- Compares Agent Price Guide (Low/Mid/High) against AVM Predicted Fair Value
- Calculates the Underquoting Gap (%) and assigns risk tiers:
  * Severe Underquoting Risk (Red): AVM exceeds agent guide high by >12% (or mid by >15%)
  * Moderate Underquoting Risk (Amber): AVM exceeds agent guide high by 5% to 12%
  * Fair / Accurate Guide (Green): Agent guide aligns within 5% of AVM
  * High-Value / Distressed Opportunity (Cyan): Priced below AVM with verified vendor price cuts
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, r2_score
from sklearn.model_selection import train_test_split

try:
    from xgboost import XGBRegressor

    HAS_XGBOOST = True
except ImportError:  # pragma: no cover
    from sklearn.ensemble import HistGradientBoostingRegressor

    HAS_XGBOOST = False

from src.etl_pipeline import generate_melbourne_market_datasets

logger = logging.getLogger(__name__)

# Flinders Street Station / Melbourne CBD reference coordinates
MELBOURNE_CBD_LAT: float = -37.8183
MELBOURNE_CBD_LON: float = 144.9671

# Known Melbourne high-density towers with historical cladding/rectification/high-OC or pre-2017 inboard layouts
HIGH_DENSITY_TOWER_WATCHLIST: Dict[str, str] = {
    "673 LA TROBE ST": "Windowless Room / Cladding History (Lacrosse)",
    "677 LA TROBE ST": "Windowless Room / Cladding History (Lacrosse)",
    "888 COLLINS ST": "High-Density Tower / High OC Fees",
    "889 COLLINS ST": "High-Density Tower / Borrowed-Light Layout",
    "883 COLLINS ST": "High-Density Tower / High OC Fees",
    "628 FLINDERS ST": "Older High-Density / Potential Cladding & High OC",
    "198 HARBOUR ESP": "Older Docklands Tower / Compact Layout",
    "8 PEARL RIVER RD": "High-Density Investor Tower / Compact Internal",
    "8 WATERVIEW WALK": "Older High-Density / Check OC Levies",
    "915 COLLINS ST": "High-Density Tower / Check Inboard Bedroom",
}

ARTERIAL_MAIN_ROADS: Tuple[str, ...] = (
    "SYDNEY RD",
    "BARKLY ST",
    "HIGH ST",
    "BELL ST",
    "NICHOLSON ST",
    "BALLARAT RD",
    "SPRINGVALE RD",
    "WARRIGAL RD",
    "MAROONDAH HWY",
    "NEPEAN HWY",
    "PUNT RD",
    "HODDLE ST",
    "DANDENONG RD",
    "ST KILDA RD",
    "QUEENS RD",
    "LA TROBE ST",
    "FLINDERS ST",
    "COLLINS ST",
    "BOURKE ST",
    "LONSDALE ST",
    "SPENCER ST",
    "KING ST",
)

PROPERTY_TYPE_ENCODING: Dict[str, int] = {
    "House": 3,
    "Townhouse": 2,
    "Unit": 1,
    "Apartment": 0,
    "Vacant Land": 2,
    "Other": 1,
}


@dataclass(frozen=True)
class AVMTrainingMetrics:
    """Evaluation metrics from AVM model validation."""

    model_backend: str
    train_samples: int
    test_samples: int
    mae_aud: float
    mape_pct: float
    r2: float


@dataclass(frozen=True)
class UnderquotingAssessment:
    """Result of comparing an agent's price guide against the AVM True Fair Value."""

    avm_fair_value: float
    agent_guide_low: float
    agent_guide_high: float
    agent_guide_mid: float
    underquoting_gap_aud: float
    underquoting_gap_pct: float
    risk_score: float
    risk_level: str
    risk_color_hex: str
    risk_rgb: List[int]
    explanation: str


def haversine_km(
    lat1: float,
    lon1: float,
    lat2: float = MELBOURNE_CBD_LAT,
    lon2: float = MELBOURNE_CBD_LON,
) -> float:
    """Compute great-circle distance in kilometers between two WGS84 coordinates."""
    r_earth = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class MelbourneAVMEngine:
    """
    Gradient-Boosted Automated Valuation Model (AVM) and Underquoting Risk Engine
    for Melbourne residential real estate.
    """

    FEATURE_COLUMNS: List[str] = [
        "land_size_sqm",
        "building_area_sqm",
        "bedrooms",
        "bathrooms",
        "car_spaces",
        "property_type_code",
        "latitude",
        "longitude",
        "cbd_distance_km",
    ]

    def __init__(self, random_state: int = 42) -> None:
        self.random_state = random_state
        self.model: Any = None
        self.metrics: Optional[AVMTrainingMetrics] = None

    @classmethod
    def engineer_features(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Transform raw property attributes into spatial & structural ML features."""
        feat = pd.DataFrame(index=df.index)
        feat["land_size_sqm"] = pd.to_numeric(df["land_size_sqm"], errors="coerce").fillna(250.0)
        feat["building_area_sqm"] = pd.to_numeric(
            df.get("building_area_sqm", feat["land_size_sqm"] * 0.48), errors="coerce"
        ).fillna(feat["land_size_sqm"] * 0.48)
        feat["bedrooms"] = pd.to_numeric(df["bedrooms"], errors="coerce").fillna(3).astype(int)
        feat["bathrooms"] = pd.to_numeric(df["bathrooms"], errors="coerce").fillna(2).astype(int)
        feat["car_spaces"] = pd.to_numeric(df.get("car_spaces", 1), errors="coerce").fillna(1).astype(int)
        feat["property_type_code"] = (
            df["property_type"].map(PROPERTY_TYPE_ENCODING).fillna(2).astype(int)
        )
        feat["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
        feat["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
        feat["cbd_distance_km"] = [
            round(haversine_km(float(lat), float(lon)), 3)
            for lat, lon in zip(feat["latitude"], feat["longitude"])
        ]
        return feat[cls.FEATURE_COLUMNS]

    def train(self, settled_sales_df: pd.DataFrame) -> AVMTrainingMetrics:
        """
        Train the XGBoost regressor on historical settled sales and compute
        hold-out validation accuracy metrics (MAE, MAPE, R²).
        """
        if settled_sales_df.empty:
            raise ValueError("Settled sales training DataFrame cannot be empty.")

        X = self.engineer_features(settled_sales_df)
        y = pd.to_numeric(settled_sales_df["sold_price"], errors="coerce").values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.20, random_state=self.random_state
        )

        if HAS_XGBOOST:
            self.model = XGBRegressor(
                n_estimators=260,
                max_depth=5,
                learning_rate=0.055,
                subsample=0.88,
                colsample_bytree=0.90,
                reg_alpha=1.0,
                reg_lambda=2.0,
                random_state=self.random_state,
                n_jobs=1,
            )
            backend_name = "XGBoost Regressor"
        else:
            self.model = HistGradientBoostingRegressor(
                max_iter=250,
                max_depth=6,
                learning_rate=0.06,
                random_state=self.random_state,
            )
            backend_name = "HistGradientBoostingRegressor"

        self.model.fit(X_train, y_train)
        y_pred = self.model.predict(X_test)

        mae = float(mean_absolute_error(y_test, y_pred))
        mape = float(mean_absolute_percentage_error(y_test, y_pred) * 100.0)
        r2 = float(r2_score(y_test, y_pred))

        self.metrics = AVMTrainingMetrics(
            model_backend=backend_name,
            train_samples=len(X_train),
            test_samples=len(X_test),
            mae_aud=round(mae, 2),
            mape_pct=round(mape, 2),
            r2=round(r2, 4),
        )
        return self.metrics

    def predict_fair_value(self, properties_df: pd.DataFrame) -> np.ndarray:
        """Predict 'True Fair Value' in AUD for a DataFrame of properties."""
        if self.model is None:
            raise RuntimeError("AVM model has not been trained yet. Call `train()` first.")
        X = self.engineer_features(properties_df)
        raw_preds = self.model.predict(X)
        # Round to nearest $1,000 for clean valuation output
        return np.round(raw_preds / 1_000.0) * 1_000.0

    @staticmethod
    def evaluate_underquoting_risk(
        avm_fair_value: float,
        guide_low: float,
        guide_high: float,
        sale_method: str = "Auction",
        price_revisions_count: int = 0,
        days_on_market: int = 18,
    ) -> UnderquotingAssessment:
        """
        Evaluate a listing's Underquoting Risk Flag by comparing the Agent Price Guide
        against the AVM True Fair Value, while distinguishing Auction Baiting from
        Motivated / Urgent Private Sellers.

        Formula
        -------
        underquoting_gap_pct = ((avm_fair_value - guide_high) / guide_high) * 100
        """
        guide_mid = (guide_low + guide_high) / 2.0 if guide_high > 0 else guide_low
        reference_guide = guide_high if guide_high > 0 else max(guide_low, 1.0)

        gap_aud = round(avm_fair_value - reference_guide, 2)
        gap_pct = round((gap_aud / reference_guide) * 100.0, 2)

        is_auction = "auction" in str(sale_method).lower()

        # Normalize a 0-100 Underquoting Risk Score
        risk_score = float(np.clip(50.0 + gap_pct * 3.2, 0.0, 100.0))
        if is_auction and gap_pct > 5.0:
            risk_score = float(np.clip(risk_score + 8.0, 0.0, 100.0))

        # Case 1: Priced significantly below AVM OR has verified price cuts in a Private Sale / Stale campaign
        if (
            (not is_auction and gap_pct >= 6.0)
            or (price_revisions_count > 0 and gap_pct >= -2.0)
            or (days_on_market >= 38 and gap_pct >= 5.0)
        ):
            risk_level = "Distressed / Urgent Seller Deal"
            color_hex = "#06B6D4"
            rgb = [6, 182, 212, 230]
            cut_note = (
                f"Vendor has cut price {price_revisions_count}x "
                if price_revisions_count > 0
                else f"Listed via {sale_method} ({days_on_market} days on market) "
            )
            explanation = (
                f"💎 Motivated Seller Signal: {cut_note}with top guide (${guide_high:,.0f}) "
                f"sitting {gap_pct:+.1f}% below AVM Fair Value (${avm_fair_value:,.0f}). "
                f"Likely an owner seeking a fast, clean sale rather than auction baiting."
            )
        elif gap_pct >= 11.0:
            risk_level = "High Underquoting Risk"
            color_hex = "#EF4444"
            rgb = [239, 68, 68, 220]
            explanation = (
                f"🔴 Auction Bait Warning: AVM Fair Value (${avm_fair_value:,.0f}) exceeds top of "
                f"agent guide (${guide_high:,.0f}) by +{gap_pct:.1f}%. High risk of hidden reserve above guide."
            )
        elif gap_pct >= 4.5:
            risk_level = "Moderate Underquoting Risk"
            color_hex = "#F59E0B"
            rgb = [245, 158, 11, 220]
            explanation = (
                f"🟠 Competitive Guide: AVM Fair Value exceeds agent guide high by +{gap_pct:.1f}%. "
                f"Expect bidding competition 5%–10% above the quoted range."
            )
        else:
            risk_level = "Low Risk (Accurate Guide)"
            color_hex = "#10B981"
            rgb = [16, 185, 129, 220]
            explanation = (
                f"🟢 Honest Guide: Agent price guide (${guide_low:,.0f}–${guide_high:,.0f}) is aligned "
                f"with AVM Fair Value (${avm_fair_value:,.0f})."
            )

        return UnderquotingAssessment(
            avm_fair_value=round(avm_fair_value, 2),
            agent_guide_low=round(guide_low, 2),
            agent_guide_high=round(guide_high, 2),
            agent_guide_mid=round(guide_mid, 2),
            underquoting_gap_aud=gap_aud,
            underquoting_gap_pct=gap_pct,
            risk_score=round(risk_score, 1),
            risk_level=risk_level,
            risk_color_hex=color_hex,
            risk_rgb=rgb,
            explanation=explanation,
        )

    @staticmethod
    def detect_property_red_flags(row: pd.Series) -> Tuple[str, str]:
        """
        Inspect structural, spatial, pricing, and building-level signals to identify
        hidden property compromises (e.g., Windowless Inboard Bedroom, Cladding/OC Risk,
        Compact Student/Bank-Restricted Size, Main Road Noise, Stale/Price-Cut Listing).

        Returns
        -------
        Tuple[str, str]
            `(short_issue_heading, detailed_due_diligence_notes)`
        """
        headings: List[str] = []
        details: List[str] = []

        norm_addr = str(row.get("normalized_address", "")).upper()
        suburb = str(row.get("suburb_name", "")).upper()
        ptype = str(row.get("property_type", "House"))
        beds = int(row.get("bedrooms", 2) or 2)
        cars = int(row.get("car_spaces", 1) or 0)
        land_sqm = float(row.get("land_size_sqm", 0) or 0)
        bldg_sqm = float(row.get("building_area_sqm", 0) or 0)
        guide_high = float(row.get("price_guide_high", 0) or 0)
        avm_val = float(row.get("avm_estimated_value", 0) or 0)
        dom = int(row.get("days_on_market", 18) or 18)
        revisions = int(row.get("price_revisions_count", 0) or 0)

        # 1. Check Known Melbourne High-Density Building Watchlist
        matched_building_flag = False
        for street_key, watch_label in HIGH_DENSITY_TOWER_WATCHLIST.items():
            if street_key in norm_addr:
                headings.append(watch_label)
                details.append(
                    f"Building Watchlist ({street_key}): Pre-2017 high-density tower known for "
                    f"inboard/windowless 2nd bedrooms, high Owners Corp (OC) fees, or cladding history. "
                    f"Inspect floorplan for external bedroom windows and review Section 32 OC minutes."
                )
                matched_building_flag = True
                break

        # 2. Detect Windowless / Inboard Bedroom & Pre-BADS Compact Layout Risk
        if ptype in ("Apartment", "Unit") and not matched_building_flag:
            if beds >= 2 and (
                (0 < bldg_sqm <= 64 and guide_high < 680_000)
                or (suburb in ("DOCKLANDS", "SOUTHBANK", "MELBOURNE", "SOUTH YARRA") and guide_high < 560_000)
            ):
                headings.append("Windowless Room / Compact Layout Risk")
                details.append(
                    f"2+ BR apartment priced at ${guide_high:,.0f} with compact footprint: High probability "
                    f"of pre-2017 'inboard' (windowless or frosted-glass) second bedroom. Verify external "
                    f"window on floorplan."
                )
            elif beds == 1 and (guide_high < 395_000 or (0 < bldg_sqm < 48)):
                headings.append("Undersized (<50sqm) / Bank LVR Restriction")
                details.append(
                    "Internal area likely under 50 sqm: Major banks often restrict LVR to <=80% "
                    "(ineligible for 5% FHG deposit) and resale liquidity is lower."
                )

        # 3. High Owners Corp (OC) / Lift & Pool Levy Risk in High-Rise Suburbs
        if ptype == "Apartment" and suburb in (
            "DOCKLANDS",
            "SOUTH YARRA",
            "SOUTH MELBOURNE",
            "PORT MELBOURNE",
            "ST KILDA",
        ):
            if "High OC" not in " ".join(headings):
                headings.append("High Owners Corp / Cladding Check")
                details.append(
                    f"High-rise precinct ({suburb.title()}): Check Section 32 for annual OC levies "
                    f"(>$6,000/yr common with lifts/concierge/pool) and combustible cladding notices."
                )

        # 4. No Off-Street Parking
        if cars == 0:
            headings.append("No Car Space")
            details.append(
                "0 registered car spaces on title: Significantly narrows future buyer pool and resale value."
            )

        # 5. Arterial Main Road / Tram Corridor Noise
        for road in ARTERIAL_MAIN_ROADS:
            if f" {road}," in norm_addr or norm_addr.endswith(road):
                if ptype in ("House", "Townhouse", "Unit"):
                    headings.append("Main Road / Traffic Noise")
                    details.append(
                        f"Located on arterial corridor ({road}): Expect traffic/tram noise and council parking overlays."
                    )
                break

        # 6. Older Unrenovated / Structural Defect Discount Signal
        if ptype in ("House", "Townhouse") and avm_val > 0 and guide_high < avm_val * 0.86:
            if revisions > 0 or dom >= 35:
                headings.append("Old Building / Reno or Easements Needed")
                details.append(
                    f"Priced >14% below suburb AVM with {dom} days on market: Likely original unrenovated "
                    f"condition, weatherboard stump/roof wear, or restrictive land covenant."
                )

        # 7. Stale Campaign / Repeated Price Cuts
        if revisions >= 2 or dom >= 50:
            headings.append("Stale Listing / Multiple Price Cuts")
            details.append(
                f"On market {dom} days with {revisions} price revision(s): Previous buyers passed after "
                f"inspection or contract review—strong leverage to negotiate."
            )

        if not headings:
            return "✅ Clean Profile (Standard Due Diligence)", (
                "No obvious structural, windowless-room, or main-road red flags detected. "
                "Perform standard Section 32 and Building & Pest inspection."
            )

        # Keep top 2 most critical headings for compact table view
        summary_heading = " | ".join(headings[:2])
        return summary_heading, " • ".join(details)

    def score_active_listings(self, listings_df: pd.DataFrame) -> pd.DataFrame:
        """
        Enrich active listings with AVM True Fair Value predictions,
        Underquoting Risk Flag assessments, and Property Red-Flag Issue Headings.
        """
        scored = listings_df.copy()
        avm_preds = self.predict_fair_value(scored)
        scored["avm_estimated_value"] = avm_preds

        assessments: List[UnderquotingAssessment] = []
        for idx, row in scored.iterrows():
            assessment = self.evaluate_underquoting_risk(
                avm_fair_value=float(row["avm_estimated_value"]),
                guide_low=float(row["price_guide_low"]),
                guide_high=float(row["price_guide_high"]),
                sale_method=str(row.get("sale_method", "Auction")),
                price_revisions_count=int(row.get("price_revisions_count", 0)),
                days_on_market=int(row.get("days_on_market", 18)),
            )
            assessments.append(assessment)

        scored["underquoting_gap_aud"] = [a.underquoting_gap_aud for a in assessments]
        scored["underquoting_gap_pct"] = [a.underquoting_gap_pct for a in assessments]
        scored["underquoting_risk_score"] = [a.risk_score for a in assessments]
        scored["underquoting_risk_level"] = [a.risk_level for a in assessments]
        scored["risk_color_hex"] = [a.risk_color_hex for a in assessments]
        scored["risk_rgb"] = [a.risk_rgb for a in assessments]
        scored["underquoting_explanation"] = [a.explanation for a in assessments]
        scored["cbd_distance_km"] = [
            round(haversine_km(float(lat), float(lon)), 2)
            for lat, lon in zip(scored["latitude"], scored["longitude"])
        ]

        issue_headings: List[str] = []
        issue_details: List[str] = []
        for _, row in scored.iterrows():
            heading, detail = self.detect_property_red_flags(row)
            issue_headings.append(heading)
            issue_details.append(detail)

        scored["property_issue_heading"] = issue_headings
        scored["property_issue_details"] = issue_details
        return scored

    @staticmethod
    def find_comparable_settled_sales(
        target_listing: pd.Series,
        settled_sales_df: pd.DataFrame,
        top_k: int = 5,
    ) -> pd.DataFrame:
        """
        Retrieve the nearest comparable historical settled sales for a given listing,
        weighted by spatial proximity (km), property type match, and bedroom similarity.
        """
        comps = settled_sales_df.copy()
        t_lat = float(target_listing["latitude"])
        t_lon = float(target_listing["longitude"])
        t_beds = int(target_listing["bedrooms"])
        t_type = str(target_listing["property_type"])
        t_land = float(target_listing.get("land_size_sqm", 300.0))

        comps["distance_km"] = [
            round(haversine_km(t_lat, t_lon, float(lat), float(lon)), 2)
            for lat, lon in zip(comps["latitude"], comps["longitude"])
        ]
        comps["type_penalty"] = np.where(comps["property_type"] == t_type, 0.0, 2.0)
        comps["bed_penalty"] = (comps["bedrooms"] - t_beds).abs() * 0.8
        comps["land_penalty"] = ((comps["land_size_sqm"] - t_land).abs() / 200.0).clip(0, 2.5)
        comps["similarity_rank_score"] = (
            comps["distance_km"] + comps["type_penalty"] + comps["bed_penalty"] + comps["land_penalty"]
        )

        nearest = comps.sort_values("similarity_rank_score", ascending=True).head(top_k).copy()
        nearest["price_per_sqm"] = (
            nearest["sold_price"] / nearest["land_size_sqm"].replace(0, np.nan)
        ).round(0)
        return nearest[
            [
                "sale_id",
                "street_address",
                "suburb_name",
                "property_type",
                "bedrooms",
                "bathrooms",
                "land_size_sqm",
                "distance_km",
                "settlement_date",
                "sale_method",
                "sold_price",
                "price_per_sqm",
            ]
        ]


if __name__ == "__main__":
    _, active_df, sales_df = generate_melbourne_market_datasets()
    avm = MelbourneAVMEngine()
    metrics = avm.train(sales_df)
    scored_df = avm.score_active_listings(active_df)

    print(
        f"AVM Training Complete ({metrics.model_backend}) | "
        f"MAE=${metrics.mae_aud:,.0f} | MAPE={metrics.mape_pct:.2f}% | R2={metrics.r2:.4f}"
    )
    risk_counts = scored_df["underquoting_risk_level"].value_counts().to_dict()
    print("Underquoting Risk Distribution:", risk_counts)

