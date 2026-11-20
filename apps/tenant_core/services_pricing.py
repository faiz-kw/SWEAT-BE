"""
apps/tenant_core/services_pricing.py — Unified Canonical Pricing & Quotation Service

Single Source of Truth for calculating quotations across the entire SaaS ecosystem:
- Admin Panel (Lead Conversion, Member Renewals, Upgrades, Package Purchases)
- Member Portal & Mobile Apps (Self-Service Renewals, Upgrades, New Plans)
- Public Website & Checkout

Enforces statutory GST compliance, zero-hardcoding database-driven catalog pricing,
and atomic discount validation.
"""

from decimal import Decimal
from typing import Optional, Dict, Any, List
import logging

from django.db import models
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from config.routers import get_tenant_db_alias
from .models_catalog import Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from .models_org import Branch
from .models_workforce import UserProfile
from .models_crm import Lead
from .models_memberships import Membership
from .services_discounts import DiscountCouponEngineService

logger = logging.getLogger(__name__)


class UnifiedPricingService:
    """
    Authoritative calculation engine for package quotations and discounts.
    Pure read-only calculations. Zero database mutation.
    """

    @classmethod
    def calculate_quote(
        cls,
        order_type: str = 'NEW_MEMBERSHIP',
        package_id: Optional[str] = None,
        package_version_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        user_profile_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        current_membership_id: Optional[str] = None,
        coupon_code: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Calculates canonical pricing breakdown:
        Catalog Price -> Promotional Discount -> Net Taxable Subtotal -> GST Tax (18%) -> Final Payable.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        order_type_clean = (order_type or 'NEW_MEMBERSHIP').strip().upper()

        # 1. Resolve Branch
        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()

        # 2. Resolve UserProfile / Lead
        user_profile = None
        lead = None
        if user_profile_id:
            user_profile = UserProfile.objects.using(alias).filter(id=user_profile_id).first()
        elif lead_id:
            lead = Lead.objects.using(alias).filter(id=lead_id).first()
            if lead and lead.organization:
                # Try matching existing user profile by phone
                if lead.phone_normalized:
                    from .models_users import TenantUser
                    t_user = TenantUser.objects.using(alias).filter(
                        organization=lead.organization,
                        phone=lead.phone_normalized
                    ).first()
                    if t_user:
                        user_profile = UserProfile.objects.using(alias).filter(user=t_user).first()

        # Fallback branch from profile if unassigned
        if not branch and user_profile:
            branch = user_profile.preferred_branch

        # 3. Resolve Target Package & Version
        package = None
        version = None
        if package_version_id:
            version = PackageVersion.objects.using(alias).select_related('package', 'package__program').filter(
                id=package_version_id
            ).first()
            if version:
                package = version.package
        elif package_id:
            package = Package.objects.using(alias).select_related('program').filter(id=package_id).first()
            if package:
                version = package.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first()
                if not version:
                    version = package.versions.using(alias).order_by('-version_number').first()

        if not package or not version:
            raise ValidationError("Target package or package version could not be resolved.")

        # 4. Resolve Catalog Price Object
        price_obj = None
        if branch:
            price_obj = version.prices.using(alias).filter(
                branch=branch, status='ACTIVE'
            ).first()
        if not price_obj:
            price_obj = version.prices.using(alias).filter(status='ACTIVE').first()

        catalog_price = Decimal(str(price_obj.base_price)) if price_obj else Decimal('15000.00')
        tax_pct = Decimal(str(price_obj.tax_percent)) if price_obj and price_obj.tax_percent is not None else Decimal('18.000')
        prices_include_tax = price_obj.prices_include_tax if price_obj else True

        # 5. Handle UPGRADE difference calculation if applicable
        current_membership = None
        current_plan_price = Decimal('0.00')
        raw_difference = catalog_price

        if order_type_clean == 'UPGRADE':
            if current_membership_id:
                current_membership = Membership.objects.using(alias).filter(id=current_membership_id).first()
            elif user_profile:
                current_membership = Membership.objects.using(alias).filter(
                    user_profile=user_profile, status='ACTIVE'
                ).order_by('-created_at').first()

            if current_membership and getattr(current_membership, 'contract_snapshot', None):
                snap = current_membership.contract_snapshot
                snap_price = getattr(snap, 'purchase_price', None) or getattr(snap, 'final_amount', None)
                if snap_price:
                    current_plan_price = Decimal(str(snap_price))

            raw_difference = max(Decimal('0.00'), catalog_price - current_plan_price)

        # Base subtotal subject to discount evaluation:
        # For upgrade, discount applies to the difference; for renewal/purchase, to catalog price
        subtotal_for_discount = raw_difference if order_type_clean == 'UPGRADE' else catalog_price

        # 6. Evaluate Dynamic Coupon / Promotional Code
        discount_amount = Decimal('0.00')
        coupon_result: Optional[Dict[str, Any]] = None

        if coupon_code and coupon_code.strip():
            coupon_clean = coupon_code.strip().upper()
            val = DiscountCouponEngineService.validate_coupon(
                code_str=coupon_clean,
                user_profile=user_profile,
                order_subtotal=subtotal_for_discount,
                branch=branch,
                package=package,
                order_type=order_type_clean,
                db_alias=alias,
            )
            coupon_result = val
            if val.get('is_valid'):
                discount_amount = Decimal(str(val.get('discount_amount') or '0.00'))

        # 7. Apply statutory GST calculation
        # Statute Invariant: Discounts are deducted from the gross amount prior to tax computation
        net_taxable_subtotal = max(Decimal('0.00'), subtotal_for_discount - discount_amount)

        if prices_include_tax:
            # Inclusive: final payable is the net discounted subtotal
            final_payable = net_taxable_subtotal
            if tax_pct > Decimal('0.00'):
                # Extract backward GST component: Tax = Total * (TaxRate / (100 + TaxRate))
                tax_fraction = tax_pct / (Decimal('100.00') + tax_pct)
                gst_tax_amount = (final_payable * tax_fraction).quantize(Decimal('0.01'))
                base_taxable_value = (final_payable - gst_tax_amount).quantize(Decimal('0.01'))
            else:
                gst_tax_amount = Decimal('0.00')
                base_taxable_value = final_payable
        else:
            # Exclusive: add GST on top of taxable subtotal
            base_taxable_value = net_taxable_subtotal
            gst_tax_amount = (base_taxable_value * (tax_pct / Decimal('100.00'))).quantize(Decimal('0.01'))
            final_payable = base_taxable_value + gst_tax_amount

        # 8. Extract Entitlements Summary
        entitlements_summary = []
        ent_defs = PackageEntitlementDefinition.objects.using(alias).filter(package_version=version)
        for ed in ent_defs:
            entitlements_summary.append({
                'type': ed.entitlement_type,
                'label': f"{ed.allocated_units} {ed.entitlement_type.replace('_', ' ').title()}",
                'units': ed.allocated_units,
                'validity_days': ed.validity_days,
            })

        return {
            'order_type': order_type_clean,
            'package': {
                'id': str(package.id),
                'name': package.name,
                'program_name': package.program.name if getattr(package, 'program', None) else 'Standard Program',
                'program_id': str(package.program_id) if getattr(package, 'program_id', None) else None,
                'version_id': str(version.id),
                'version_number': version.version_number,
                'validity_days': version.validity_days or 90,
            },
            'branch': {
                'id': str(branch.id) if branch else None,
                'name': branch.name if branch else 'All Studio Branches',
                'code': branch.code if branch else None,
            },
            'pricing': {
                'currency': price_obj.currency if price_obj else 'INR',
                'catalog_price': float(catalog_price),
                'current_plan_price': float(current_plan_price) if order_type_clean == 'UPGRADE' else None,
                'raw_difference': float(raw_difference) if order_type_clean == 'UPGRADE' else None,
                'subtotal_before_discount': float(subtotal_for_discount),
                'discount_amount': float(discount_amount),
                'base_taxable_value': float(base_taxable_value),
                'tax_percentage': float(tax_pct),
                'gst_tax_amount': float(gst_tax_amount),
                'final_payable': float(final_payable),
                'prices_include_tax': prices_include_tax,
                'savings_amount': float(discount_amount),
                'savings_percentage': round(float((discount_amount / subtotal_for_discount) * 100), 2) if subtotal_for_discount > Decimal('0.00') else 0.0,
            },
            'coupon': {
                'is_valid': coupon_result.get('is_valid', False),
                'code': coupon_result.get('code', coupon_code.strip().upper()) if coupon_code else None,
                'name': coupon_result.get('campaign_name') or coupon_result.get('name'),
                'discount_type': coupon_result.get('discount_type'),
                'discount_value': float(coupon_result.get('discount_value', 0)) if coupon_result else None,
                'discount_amount': float(discount_amount),
                'reason': coupon_result.get('reason'),
                'reason_code': coupon_result.get('reason_code'),
            } if coupon_result else None,
            'entitlements': entitlements_summary,
            'calculated_at': timezone.now().isoformat(),
        }
