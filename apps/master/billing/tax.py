"""
Master DB — Tax Engine & Jurisdiction-Aware Resolution.
Phase 1 Layer 1 — Commercial Billing & Subscription Lifecycle.

Architectural Principles:
- Provider/configuration-driven: No hardcoded tax rates in models or views.
- 18% GST is INITIAL BUSINESS CONFIGURATION for India B2B SaaS, NOT an immutable engine rule.
- Decimal-only arithmetic with ROUND_HALF_UP to the nearest cent/paisa.
- Line-item based calculation: Total invoice tax is the sum of line-item taxes.
- Immutability: Once an invoice is ISSUED, tax calculations are locked and never recalculated dynamically.
"""

from abc import ABC, abstractmethod
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class TaxCalculationRequest:
    subtotal: Decimal
    currency: str = 'INR'
    country_code: str = 'IN'
    state_code: Optional[str] = None  # e.g., 'KA', 'MH', 'DL'
    tax_identifier: Optional[str] = None  # e.g., GSTIN
    is_tax_exempt: bool = False
    customer_jurisdiction: Optional[str] = None
    customer_tax_id: Optional[str] = None
    item_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.customer_jurisdiction and not self.country_code:
            self.country_code = self.customer_jurisdiction
        if self.customer_tax_id and not self.tax_identifier:
            self.tax_identifier = self.customer_tax_id


@dataclass
class TaxCalculationResult:
    tax_rate: Decimal  # e.g. Decimal('18.00')
    tax_amount: Decimal  # e.g. Decimal('1799.82')
    tax_breakdown: Dict[str, Any]  # {'CGST': '9.0%', 'SGST': '9.0%'}
    jurisdiction_code: str  # 'IN-KA' or 'INTL'
    tax_description: str  # 'GST 18% (9% CGST + 9% SGST)'
    rule_version: str  # 'IN_GST_2026_V1'
    total_amount: Decimal = Decimal('0.00')

    @property
    def breakdown(self):
        return self.tax_breakdown

    @property
    def tax_rate_percent(self):
        return self.tax_rate


class BaseTaxResolver(ABC):
    """Abstract interface for tax resolution across jurisdictions."""

    @abstractmethod
    def calculate_tax(self, request: TaxCalculationRequest) -> TaxCalculationResult:
        pass


class DefaultIndiaTaxResolver(BaseTaxResolver):
    """
    Default tax resolver implementing the current business configuration for Indian B2B SaaS.
    Platform default: Karnataka ('KA'), Standard SAC 998313 at 18.00% GST.
    """

    PLATFORM_STATE_CODE = 'KA'
    DEFAULT_GST_RATE = Decimal('18.00')

    def __init__(self, platform_state: str = 'KA', default_rate: Optional[Decimal] = None):
        self.platform_state = platform_state
        self.default_rate = default_rate or self.DEFAULT_GST_RATE

    def calculate_tax(self, request: TaxCalculationRequest) -> TaxCalculationResult:
        if request.is_tax_exempt:
            return TaxCalculationResult(
                tax_rate=Decimal('0.00'),
                tax_amount=Decimal('0.00'),
                tax_breakdown={'EXEMPT': Decimal('0.00')},
                jurisdiction_code=request.country_code or 'EXEMPT',
                tax_description='Tax Exempt',
                rule_version='EXEMPT_V1',
                total_amount=request.subtotal
            )

        # Non-India export: 0% zero-rated supply with LUT/Bond
        country = request.country_code or request.customer_jurisdiction or 'IN'
        if country.upper() != 'IN':
            return TaxCalculationResult(
                tax_rate=Decimal('0.00'),
                tax_amount=Decimal('0.00'),
                tax_breakdown={'EXPORT_ZERO_RATED': Decimal('0.00')},
                jurisdiction_code=country.upper(),
                tax_description='Zero-Rated Export (LUT)',
                rule_version='INTL_EXPORT_V1',
                total_amount=request.subtotal
            )

        # India GST calculation
        subtotal = request.subtotal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rate = self.default_rate
        tax_amount = (subtotal * rate / Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        dest_state = (request.state_code or self.platform_state).upper()
        is_intra_state = (dest_state == self.platform_state)

        if is_intra_state:
            half_rate = (rate / Decimal('2.00')).quantize(Decimal('0.01'))
            cgst = (tax_amount / Decimal('2.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            sgst = tax_amount - cgst  # Prevent 1 paisa rounding mismatch
            breakdown = {
                'CGST': f"{float(half_rate):.1f}%",
                'SGST': f"{float(half_rate):.1f}%",
                'cgst_amount': float(cgst),
                'sgst_amount': float(sgst)
            }
            desc = f"GST {rate}% ({half_rate}% CGST + {half_rate}% SGST)"
        else:
            breakdown = {'IGST': f"{float(rate):.1f}%", 'igst_amount': float(tax_amount)}
            desc = f"IGST {rate}%"

        return TaxCalculationResult(
            tax_rate=rate,
            tax_amount=tax_amount,
            tax_breakdown=breakdown,
            jurisdiction_code=f"IN-{dest_state}",
            tax_description=desc,
            rule_version='IN_GST_2026_V1',
            total_amount=subtotal + tax_amount
        )


# Global resolver instance
_active_tax_resolver: BaseTaxResolver = DefaultIndiaTaxResolver()


def get_tax_resolver() -> BaseTaxResolver:
    return _active_tax_resolver


def set_tax_resolver(resolver: BaseTaxResolver) -> None:
    global _active_tax_resolver
    _active_tax_resolver = resolver
