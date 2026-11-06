"""
Victorian Financial & First-Home Buyer Calculation Engine (`src/financial_calculator.py`).

Implements State Revenue Office (SRO) Victoria Land Transfer Duty (Stamp Duty) progressive
scales, First-Home Buyer (FHB) exemptions/concessions, Federal First Home Guarantee (FHG)
deposit structures, and full Cash-to-Close modeling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class StampDutyBreakdown:
    """Detailed result of Victorian Land Transfer Duty calculation."""

    dutiable_value: float
    base_stamp_duty: float
    fhb_exemption_applied: bool
    fhb_concession_applied: bool
    concession_savings: float
    payable_stamp_duty: float
    effective_duty_rate_pct: float


@dataclass(frozen=True)
class FHGEligibilityResult:
    """Federal First Home Guarantee (FHG) deposit & LMI assessment."""

    property_price: float
    melbourne_metro_cap: float
    is_fhg_eligible: bool
    required_deposit_pct: float
    required_deposit_amount: float
    estimated_lmi_waived: float
    loan_amount: float
    lvr_pct: float


@dataclass(frozen=True)
class CashToCloseSummary:
    """Complete upfront capital requirement and shortfall analysis."""

    property_price: float
    is_first_home_buyer: bool
    available_savings: float
    minimum_deposit: float
    required_deposit_pct: float
    fhg_scheme_applied: bool
    payable_stamp_duty: float
    fhb_duty_savings: float
    conveyancing_legal_fee: float
    building_pest_inspection_fee: float
    title_transfer_registration_fee: float
    total_upfront_costs_excl_deposit: float
    total_cash_to_close: float
    savings_surplus_or_shortfall: float
    has_sufficient_funds: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VictorianPropertyFinance:
    """
    Enterprise calculation engine for Victorian residential property acquisitions.

    Covers:
    1. Victorian Land Transfer Duty (Stamp Duty) general progressive rates:
       - $0 to $25,000: 1.4% of dutiable value
       - $25,001 to $130,000: $350 + 2.4% of amount over $25,000
       - $130,001 to $960,000: $2,870 + 6.0% of amount over $130,000
       - $960,001 to $2,000,000: 5.5% of total dutiable value
       - Over $2,000,000: $110,000 + 6.5% of amount over $2,000,000
    2. First-Home Buyer (FHB) Duty Exemption & Concession:
       - Full exemption (zero duty) for dutiable values <= $600,000
       - Sliding-scale concession for dutiable values between $600,001 and $750,000
    3. Federal First Home Guarantee (FHG):
       - Melbourne Metropolitan price cap of $950,000 allows a 5% deposit with $0 LMI.
       - Above $950,000 (or non-FHB) defaults to a standard 20% deposit to avoid LMI.
    4. Total Cash-to-Close:
       - Deposit + Stamp Duty + Conveyancing ($2,000) + Building & Pest ($600) + Savings Shortfall.
    """

    FHB_EXEMPTION_CAP: float = 600_000.0
    FHB_CONCESSION_CAP: float = 750_000.0
    FHG_MELBOURNE_METRO_CAP: float = 950_000.0

    FHG_LOW_DEPOSIT_RATE: float = 0.05
    STANDARD_DEPOSIT_RATE: float = 0.20

    DEFAULT_CONVEYANCING_FEE: float = 2_000.0
    DEFAULT_BUILDING_PEST_FEE: float = 600.0

    @classmethod
    def calculate_general_stamp_duty(cls, dutiable_value: float) -> float:
        """
        Calculate standard Victorian general land transfer duty (prior to concessions).

        Parameters
        ----------
        dutiable_value : float
            Purchase price or market value of the property in AUD.

        Returns
        -------
        float
            Standard Victorian stamp duty rounded to 2 decimal places.
        """
        if dutiable_value <= 0:
            return 0.0

        if dutiable_value <= 25_000:
            duty = dutiable_value * 0.014
        elif dutiable_value <= 130_000:
            duty = 350.0 + (dutiable_value - 25_000.0) * 0.024
        elif dutiable_value <= 960_000:
            duty = 2_870.0 + (dutiable_value - 130_000.0) * 0.06
        elif dutiable_value <= 2_000_000:
            duty = dutiable_value * 0.055
        else:
            duty = 110_000.0 + (dutiable_value - 2_000_000.0) * 0.065

        return round(duty, 2)

    @classmethod
    def calculate_stamp_duty(
        cls,
        property_price: float,
        is_first_home_buyer: bool = True,
    ) -> StampDutyBreakdown:
        """
        Compute Victorian Land Transfer Duty including First-Home Buyer exemptions
        and sliding-scale concessions ($600,000 to $750,000).
        """
        if property_price < 0:
            raise ValueError("Property price cannot be negative.")

        base_duty = cls.calculate_general_stamp_duty(property_price)
        fhb_exemption = False
        fhb_concession = False
        payable_duty = base_duty

        if is_first_home_buyer:
            if property_price <= cls.FHB_EXEMPTION_CAP:
                fhb_exemption = True
                payable_duty = 0.0
            elif property_price < cls.FHB_CONCESSION_CAP:
                fhb_concession = True
                # Linear phase-out between $600,000 (0% of base duty) and $750,000 (100% of base duty)
                concession_proportion = (property_price - cls.FHB_EXEMPTION_CAP) / (
                    cls.FHB_CONCESSION_CAP - cls.FHB_EXEMPTION_CAP
                )
                payable_duty = round(base_duty * concession_proportion, 2)

        concession_savings = round(base_duty - payable_duty, 2)
        effective_rate = round((payable_duty / property_price) * 100.0, 2) if property_price > 0 else 0.0

        return StampDutyBreakdown(
            dutiable_value=round(property_price, 2),
            base_stamp_duty=base_duty,
            fhb_exemption_applied=fhb_exemption,
            fhb_concession_applied=fhb_concession,
            concession_savings=concession_savings,
            payable_stamp_duty=payable_duty,
            effective_duty_rate_pct=effective_rate,
        )

    @classmethod
    def evaluate_fhg_eligibility(
        cls,
        property_price: float,
        is_first_home_buyer: bool = True,
    ) -> FHGEligibilityResult:
        """
        Assess eligibility for the Federal First Home Guarantee (FHG) under the
        Melbourne metropolitan cap ($950,000).
        """
        if property_price < 0:
            raise ValueError("Property price cannot be negative.")

        is_eligible = bool(is_first_home_buyer and 0 < property_price <= cls.FHG_MELBOURNE_METRO_CAP)
        deposit_rate = cls.FHG_LOW_DEPOSIT_RATE if is_eligible else cls.STANDARD_DEPOSIT_RATE
        deposit_amount = round(property_price * deposit_rate, 2)
        loan_amount = round(property_price - deposit_amount, 2)
        lvr_pct = round((1.0 - deposit_rate) * 100.0, 2) if property_price > 0 else 0.0

        # Approximate commercial LMI saved by FHG guarantee (~3.1% of 95% LVR loan)
        estimated_lmi_waived = round(loan_amount * 0.031, 2) if is_eligible else 0.0

        return FHGEligibilityResult(
            property_price=round(property_price, 2),
            melbourne_metro_cap=cls.FHG_MELBOURNE_METRO_CAP,
            is_fhg_eligible=is_eligible,
            required_deposit_pct=round(deposit_rate * 100.0, 2),
            required_deposit_amount=deposit_amount,
            estimated_lmi_waived=estimated_lmi_waived,
            loan_amount=loan_amount,
            lvr_pct=lvr_pct,
        )

    @classmethod
    def calculate_cash_to_close(
        cls,
        property_price: float,
        available_savings: float = 0.0,
        is_first_home_buyer: bool = True,
        conveyancing_fee: float = DEFAULT_CONVEYANCING_FEE,
        building_pest_fee: float = DEFAULT_BUILDING_PEST_FEE,
        title_registration_fee: float = 0.0,
    ) -> CashToCloseSummary:
        """
        Compute complete Total Cash-to-Close including deposit structure, Victorian
        stamp duty, legal/conveyancing ($2,000), building & pest inspection ($600),
        and upfront savings surplus/shortfall.
        """
        duty_result = cls.calculate_stamp_duty(
            property_price=property_price,
            is_first_home_buyer=is_first_home_buyer,
        )
        fhg_result = cls.evaluate_fhg_eligibility(
            property_price=property_price,
            is_first_home_buyer=is_first_home_buyer,
        )

        ancillary_costs = round(
            duty_result.payable_stamp_duty
            + conveyancing_fee
            + building_pest_fee
            + title_registration_fee,
            2,
        )
        total_cash_required = round(fhg_result.required_deposit_amount + ancillary_costs, 2)
        surplus_or_shortfall = round(available_savings - total_cash_required, 2)

        return CashToCloseSummary(
            property_price=round(property_price, 2),
            is_first_home_buyer=is_first_home_buyer,
            available_savings=round(available_savings, 2),
            minimum_deposit=fhg_result.required_deposit_amount,
            required_deposit_pct=fhg_result.required_deposit_pct,
            fhg_scheme_applied=fhg_result.is_fhg_eligible,
            payable_stamp_duty=duty_result.payable_stamp_duty,
            fhb_duty_savings=duty_result.concession_savings,
            conveyancing_legal_fee=round(conveyancing_fee, 2),
            building_pest_inspection_fee=round(building_pest_fee, 2),
            title_transfer_registration_fee=round(title_registration_fee, 2),
            total_upfront_costs_excl_deposit=ancillary_costs,
            total_cash_to_close=total_cash_required,
            savings_surplus_or_shortfall=surplus_or_shortfall,
            has_sufficient_funds=surplus_or_shortfall >= 0,
        )


if __name__ == "__main__":
    # Quick verification across Victorian FHB & General brackets
    sample_scenarios = [
        ("FHB Exempt ($580k)", 580_000, 45_000, True),
        ("FHB Sliding Concession ($680k)", 680_000, 65_000, True),
        ("FHB Under FHG Cap ($890k)", 890_000, 100_000, True),
        ("Above FHG Cap ($1.15M)", 1_150_000, 260_000, False),
    ]
    for label, price, savings, is_fhb in sample_scenarios:
        summary = VictorianPropertyFinance.calculate_cash_to_close(
            property_price=price,
            available_savings=savings,
            is_first_home_buyer=is_fhb,
        )
        print(
            f"[{label}] Price=${price:,.0f} | Deposit=${summary.minimum_deposit:,.0f} "
            f"({summary.required_deposit_pct:.0f}%) | Stamp Duty=${summary.payable_stamp_duty:,.0f} "
            f"| Total Cash-to-Close=${summary.total_cash_to_close:,.0f} "
            f"| Surplus/Shortfall=${summary.savings_surplus_or_shortfall:+,.0f}"
        )

