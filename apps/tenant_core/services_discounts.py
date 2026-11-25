"""
apps/tenant_core/services_discounts.py - DiscountCouponEngineService for Layer 2 Module H

Capabilities:
- Percentage, fixed amount, and capped discounts.
- Branch and package scoped validations with authoritative order item matching.
- Global and per-user redemption limits with strict row-locking concurrency control.
- Dynamic eligibility rule evaluation:
  - Consumed sessions, remaining sessions, session usage percentage.
  - Package age, membership remaining days.
  - Current package to target package upgrade rules.
  - Multiple active membership support and unavailable metrics fail-closed evaluation.
- Authoritative tax recalculation:
  - Per-item mixed tax rates.
  - Explicit zero-rated item preservation (0.00% tax snapshot).
  - Missing snapshot fallback to OrganizationSettings.tax_rate_pct.
  - Proportional discount allocation across line items with exact penny rounding.
  - Deterministic idempotence on repeated redemptions.
- Transactional redemption audit snapshot and outbox event publishing.
"""

import logging
from decimal import Decimal
from typing import Optional, Dict, Any, List, Union
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRuleCondition,
    DiscountRuleAction,
    DiscountRedemption,
)
from .models_workforce import UserProfile
from .models_catalog import Package
from .models_org import Branch
from .models_users import TenantUser
from .models_commerce import Order
from .services_reliability import record_business_audit, enqueue_outbox_event
from .context import get_current_tenant_db_alias, set_tenant_db_alias

logger = logging.getLogger(__name__)


class DiscountCouponEngineService:
    """
    High performance, deterministic discount evaluation and coupon redemption engine.
    """

    # Dynamic transaction/order type semantic families for multi-platform compatibility
    ORDER_TYPE_FAMILIES = {
        'NEW_MEMBERSHIP': {
            'NEW_MEMBERSHIP', 'NEW_PACKAGE', 'NEW', 'LEAD_CONVERSION',
            'FIRST_PURCHASE', 'ENROLLMENT', 'SIGNUP', 'PURCHASE', 'NEW_SALE', 'PACKAGE_PURCHASE'
        },
        'NEW_PACKAGE': {
            'NEW_PACKAGE', 'NEW_MEMBERSHIP', 'NEW', 'LEAD_CONVERSION',
            'FIRST_PURCHASE', 'ENROLLMENT', 'SIGNUP', 'PURCHASE', 'NEW_SALE', 'PACKAGE_PURCHASE'
        },
        'PACKAGE_PURCHASE': {
            'PACKAGE_PURCHASE', 'NEW_PACKAGE', 'NEW_MEMBERSHIP', 'NEW', 'LEAD_CONVERSION',
            'FIRST_PURCHASE', 'ENROLLMENT', 'SIGNUP', 'PURCHASE', 'NEW_SALE'
        },
        'RENEWAL': {
            'RENEWAL', 'RENEW', 'MEMBERSHIP_RENEWAL', 'PACKAGE_RENEWAL'
        },
        'UPGRADE': {
            'UPGRADE', 'TIER_UPGRADE', 'MEMBERSHIP_UPGRADE'
        },
        'REJOIN': {
            'REJOIN', 'WINBACK', 'REACTIVATION'
        },
        'ADDON': {
            'ADDON', 'TOPUP', 'SESSION_PACK', 'EXTRA_SESSIONS'
        },
    }

    @classmethod
    def matches_order_type(cls, candidate_order_type: Optional[str], allowed_order_types: Optional[List[str]]) -> bool:
        """
        Dynamically checks whether candidate_order_type satisfies allowed_order_types.
        Enterprise-grade dynamic resolution:
        1. If allowed_order_types is unconfigured / empty / None / contains 'ALL', '*', 'ANY', returns True.
        2. Exact case-insensitive match -> True.
        3. Semantic alias expansion across transaction families (e.g., NEW_MEMBERSHIP, LEAD_CONVERSION, NEW_PACKAGE).
        """
        if not allowed_order_types:
            return True
        clean_allowed = [str(x).strip().upper() for x in allowed_order_types if x]
        if not clean_allowed or any(x in ('ALL', '*', 'ANY', 'UNIVERSAL') for x in clean_allowed):
            return True

        cand = str(candidate_order_type or '').strip().upper()
        if not cand:
            return False

        if cand in clean_allowed:
            return True

        # Semantic family alias expansion
        cand_synonyms = set(cls.ORDER_TYPE_FAMILIES.get(cand, {cand}))
        cand_synonyms.add(cand)

        allowed_synonyms = set()
        for a in clean_allowed:
            allowed_synonyms.add(a)
            allowed_synonyms.update(cls.ORDER_TYPE_FAMILIES.get(a, set()))

        return bool(cand_synonyms & allowed_synonyms)

    # Dynamic member lifecycle state semantic families
    MEMBER_TYPE_FAMILIES = {
        'NEW': {'NEW', 'PROSPECT', 'LEAD', 'FIRST_TIME', 'NEW_MEMBER'},
        'EXPIRED': {'EXPIRED', 'INACTIVE', 'CHURNED', 'LAPSED', 'WINBACK', 'REJOIN', 'FORMER'},
        'ACTIVE': {'ACTIVE', 'CURRENT', 'EXISTING_ACTIVE', 'MEMBER'},
        'EXISTING': {'ACTIVE', 'CURRENT', 'EXPIRED', 'INACTIVE', 'CHURNED', 'LAPSED', 'WINBACK', 'REJOIN', 'FORMER'},
        'NEW_OR_EXPIRED': {'NEW', 'PROSPECT', 'LEAD', 'FIRST_TIME', 'NEW_MEMBER', 'EXPIRED', 'INACTIVE', 'CHURNED', 'LAPSED', 'WINBACK', 'REJOIN', 'FORMER'},
    }

    @classmethod
    def resolve_authoritative_member_profile(
        cls,
        member_identifier: Optional[str],
        request_user=None,
        db_alias: Optional[str] = None
    ) -> Optional[UserProfile]:
        """
        Authoritative resolver for a genuine customer member profile.
        Supports lookup across:
        1. UserProfile UUID
        2. TenantUser UUID (user_id)
        3. Member number (e.g. MEM-12345)
        4. Membership UUID (membership_id / contract_id) -> user_profile
        5. Membership member_id -> user_profile
        6. End-user customer fallback (excludes staff/admin accounts from being treated as purchasing customer)
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        if not member_identifier and request_user and getattr(request_user, 'is_authenticated', False):
            # Only fall back to request.user if the user is a genuine customer/member, NOT a staff/admin user
            user_type = str(getattr(request_user, 'user_type', '')).upper()
            is_staff = getattr(request_user, 'is_staff', False) or user_type in ('STAFF', 'ADMIN', 'MANAGER', 'OWNER', 'COACH', 'TRAINER')
            if not is_staff and user_type in ('MEMBER', 'CUSTOMER'):
                return UserProfile.objects.using(alias).filter(user=request_user).first()
            return None

        if not member_identifier:
            return None

        ident = str(member_identifier).strip()
        try:
            # 1. Direct UserProfile ID
            p = UserProfile.objects.using(alias).filter(id=ident).first()
            if p:
                return p

            # 2. TenantUser ID (user_id)
            p = UserProfile.objects.using(alias).filter(user_id=ident).first()
            if p:
                return p

            # 3. Member number
            p = UserProfile.objects.using(alias).filter(member_number=ident).first()
            if p:
                return p

            # 4. Membership ID -> user_profile
            from .models_memberships import Membership
            mem = Membership.objects.using(alias).filter(id=ident).select_related('user_profile').first()
            if mem and mem.user_profile:
                return mem.user_profile

            # 5. Membership membership_number -> user_profile
            mem = Membership.objects.using(alias).filter(membership_number=ident).select_related('user_profile').first()
            if mem and mem.user_profile:
                return mem.user_profile
        except Exception as e:
            logger.debug("Error in resolve_authoritative_member_profile for '%s': %s", ident, e)

        return None

    @classmethod
    def get_member_lifecycle_state(cls, user_profile: Optional[UserProfile], db_alias: Optional[str] = None) -> str:
        """
        Determines authoritative member lifecycle state:
        - 'NEW': First-time user / lead with zero past memberships.
        - 'ACTIVE': Has at least 1 currently active, valid, unexpired membership.
        - 'EXPIRED': Has prior membership(s), but all are expired, cancelled, or inactive.
        """
        if not user_profile:
            return 'NEW'
        alias = db_alias or (getattr(user_profile, '_state', None) and user_profile._state.db) or get_current_tenant_db_alias() or 'default'
        from .models_memberships import Membership
        today = timezone.now().date()
        mems = list(Membership.objects.using(alias).filter(user_profile=user_profile))
        if not mems:
            return 'NEW'
        has_active = any(
            m.status == 'ACTIVE' and (m.end_date is None or m.end_date >= today)
            for m in mems
        )
        return 'ACTIVE' if has_active else 'EXPIRED'

    @classmethod
    def matches_member_type(cls, candidate_state: Optional[str], allowed_member_types: Optional[Union[str, List[str]]]) -> bool:
        """
        Dynamically checks whether candidate_state satisfies allowed_member_types.
        Supports:
        - Unrestricted / None / empty / 'ALL' / '*' -> True
        - Single string or list of allowed member types
        - Semantic family alias expansion (NEW, EXPIRED, ACTIVE, NEW_OR_EXPIRED)
        """
        if not allowed_member_types:
            return True
        if isinstance(allowed_member_types, str):
            allowed_list = [allowed_member_types]
        else:
            allowed_list = [str(x) for x in allowed_member_types if x]
        clean_allowed = [x.strip().upper() for x in allowed_list if x]
        if not clean_allowed or any(x in ('ALL', '*', 'ANY', 'UNIVERSAL') for x in clean_allowed):
            return True

        cand = str(candidate_state or 'NEW').strip().upper()

        if cand in clean_allowed:
            return True

        allowed_synonyms = set()
        for a in clean_allowed:
            allowed_synonyms.add(a)
            allowed_synonyms.update(cls.MEMBER_TYPE_FAMILIES.get(a, set()))

        if 'NEW_OR_EXPIRED' in allowed_synonyms:
            if cand in ('NEW', 'EXPIRED'):
                return True
            if cand == 'ACTIVE':
                return False

        if 'ACTIVE' in allowed_synonyms and 'NEW' not in allowed_synonyms and 'EXPIRED' not in allowed_synonyms:
            return cand == 'ACTIVE'

        cand_synonyms = set(cls.MEMBER_TYPE_FAMILIES.get(cand, {cand}))
        cand_synonyms.add(cand)

        return bool(cand_synonyms & allowed_synonyms)

    @classmethod
    def validate_coupon(
        cls,
        code_str: str,
        user_profile: Optional[UserProfile] = None,
        order_subtotal: Optional[Union[Decimal, float, str]] = None,
        branch: Optional[Branch] = None,
        package: Optional[Package] = None,
        db_alias: Optional[str] = None,
        order: Optional[Order] = None,
        order_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate a coupon code against campaign rules, usage limits, and scopes.
        Supports recomputing authoritative subtotal and package scope directly from order records.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        code_clean = (code_str or '').strip().upper()
        if not code_clean:
            return {'is_valid': False, 'reason': 'Coupon code is required.', 'reason_code': 'REQUIRED'}

        # Prioritize active codes belonging to currently active campaigns
        discount_code = DiscountCode.objects.using(alias).select_related('campaign', 'branch', 'package').filter(
            code__iexact=code_clean,
            status='ACTIVE',
            campaign__status='ACTIVE'
        ).order_by('-created_at').first()
        if not discount_code:
            discount_code = DiscountCode.objects.using(alias).select_related('campaign', 'branch', 'package').filter(
                code__iexact=code_clean
            ).order_by('-created_at').first()
        if not discount_code:
            return {'is_valid': False, 'reason': f"Coupon code '{code_str}' is invalid or does not exist.", 'reason_code': 'NOT_FOUND'}

        # Scope & isolation: Verify campaign belongs to the same organization as user/branch
        if user_profile and hasattr(user_profile, 'user') and getattr(user_profile.user, 'organization_id', None):
            if discount_code.campaign.organization_id != user_profile.user.organization_id:
                return {'is_valid': False, 'reason': 'Cross-organization redemption prohibited: coupon belongs to a different organization.', 'reason_code': 'CROSS_ORGANIZATION'}
        if branch and hasattr(branch, 'organization_id'):
            if discount_code.campaign.organization_id != branch.organization_id:
                return {'is_valid': False, 'reason': 'Cross-organization redemption prohibited: coupon belongs to a different organization.', 'reason_code': 'CROSS_ORGANIZATION'}

        if discount_code.status != 'ACTIVE':
            return {'is_valid': False, 'reason': f"Coupon code '{code_str}' is {discount_code.status.lower()}.", 'reason_code': 'INACTIVE'}

        campaign = discount_code.campaign
        if not campaign.is_currently_valid():
            now = timezone.now()
            r_code = 'EXPIRED' if (campaign.valid_until and now > campaign.valid_until) else 'INACTIVE'
            return {'is_valid': False, 'reason': f"Campaign '{campaign.name}' is inactive or expired.", 'reason_code': r_code}

        # Tenant & Organization scope isolation
        if order is not None:
            if campaign.organization_id != order.branch.organization_id:
                return {'is_valid': False, 'reason': 'Coupon campaign belongs to a different organization.', 'reason_code': 'ORG_MISMATCH'}
            if user_profile and user_profile.user.organization_id != order.branch.organization_id:
                return {'is_valid': False, 'reason': 'Member belongs to a different organization.', 'reason_code': 'ORG_MISMATCH'}
            if order.user_profile_id and user_profile and order.user_profile_id != user_profile.id:
                return {'is_valid': False, 'reason': 'Order belongs to a different member.', 'reason_code': 'MEMBER_MISMATCH'}
            if branch is None:
                branch = order.branch

        if branch is not None and campaign.organization_id != branch.organization_id:
            return {'is_valid': False, 'reason': 'Branch belongs to a different organization.', 'reason_code': 'ORG_MISMATCH'}

        if user_profile is not None and campaign.organization_id != user_profile.user.organization_id:
            return {'is_valid': False, 'reason': 'Member belongs to a different organization.', 'reason_code': 'ORG_MISMATCH'}

        # Branch scope
        camp_cfg = campaign.configuration if isinstance(campaign.configuration, dict) else {}
        code_branch_id = str(discount_code.branch_id) if discount_code.branch_id else None
        camp_branch_ids = [str(x) for x in camp_cfg.get('branch_ids', []) if x] if isinstance(camp_cfg.get('branch_ids'), list) else []
        camp_single_branch_id = str(camp_cfg.get('branch_id')) if (camp_cfg.get('branch_id') and not camp_branch_ids) else None

        allowed_branch_ids = [code_branch_id] if code_branch_id else (camp_branch_ids if camp_branch_ids else ([camp_single_branch_id] if camp_single_branch_id else []))

        if allowed_branch_ids:
            allowed_branch_set = set(allowed_branch_ids)
            if branch and str(branch.id) not in allowed_branch_set:
                if len(allowed_branch_ids) == 1:
                    branch_label = discount_code.branch.name if discount_code.branch else (Branch.objects.using(alias).filter(id=allowed_branch_ids[0]).values_list('name', flat=True).first() or 'the designated branch')
                    return {'is_valid': False, 'reason': f"Coupon is only valid at {branch_label}.", 'reason_code': 'BRANCH_NOT_ELIGIBLE'}
                else:
                    return {'is_valid': False, 'reason': 'Coupon is not valid at this branch.', 'reason_code': 'BRANCH_NOT_ELIGIBLE'}
            elif not branch and order is not None and str(order.branch_id) not in allowed_branch_set:
                return {'is_valid': False, 'reason': 'Coupon is not valid at this branch.', 'reason_code': 'BRANCH_NOT_ELIGIBLE'}

        # Package and Program scope (Dynamic & configurable)
        # Priority 1: Explicit package lock on the DiscountCode itself
        # Priority 2: Multi-package scope on Campaign configuration ('package_ids')
        # Priority 3: Single-package scope on Campaign configuration ('package_id')
        # Priority 4: Whole-program scope on Campaign configuration ('program_ids')
        code_pkg_id = str(discount_code.package_id) if discount_code.package_id else None
        camp_pkg_ids = [str(x) for x in camp_cfg.get('package_ids', []) if x] if isinstance(camp_cfg.get('package_ids'), list) else []
        camp_single_pkg_id = str(camp_cfg.get('package_id')) if (camp_cfg.get('package_id') and not camp_pkg_ids) else None
        allowed_package_ids = [code_pkg_id] if code_pkg_id else (camp_pkg_ids if camp_pkg_ids else ([camp_single_pkg_id] if camp_single_pkg_id else []))
        allowed_program_ids = [str(x) for x in camp_cfg.get('program_ids', []) if x] if isinstance(camp_cfg.get('program_ids'), list) else []

        if allowed_package_ids or allowed_program_ids:
            allowed_package_set = set(allowed_package_ids)
            allowed_program_set = set(allowed_program_ids)
            pkg_eligible = False

            if package:
                pkg_ok = (not allowed_package_set) or (str(package.id) in allowed_package_set)
                prog_ok = (not allowed_program_set) or (bool(getattr(package, 'program_id', None)) and str(package.program_id) in allowed_program_set)
                pkg_eligible = pkg_ok and prog_ok
            elif order is not None:
                for _it in order.items.using(alias).filter(package_id__isnull=False).select_related('package'):
                    pkg_ok = (not allowed_package_set) or (str(_it.package_id) in allowed_package_set)
                    _it_prog = getattr(getattr(_it, 'package', None), 'program_id', None)
                    prog_ok = (not allowed_program_set) or (bool(_it_prog) and str(_it_prog) in allowed_program_set)
                    if pkg_ok and prog_ok:
                        pkg_eligible = True
                        break

            if not pkg_eligible:
                if not package and order is None:
                    if len(allowed_package_ids) == 1:
                        pkg_label = discount_code.package.name if discount_code.package else (Package.objects.using(alias).filter(id=allowed_package_ids[0]).values_list('name', flat=True).first() or 'the designated package')
                        return {'is_valid': False, 'reason': f"Coupon requires selecting target package '{pkg_label}'.", 'reason_code': 'PACKAGE_NOT_ELIGIBLE'}
                    else:
                        return {'is_valid': False, 'reason': f"Coupon is restricted to specific packages or programs. Please select an eligible package under 'Target Package'.", 'reason_code': 'PACKAGE_NOT_ELIGIBLE'}
                elif package:
                    if allowed_program_set and not allowed_package_set:
                        return {'is_valid': False, 'reason': 'Coupon is not valid for this program.', 'reason_code': 'PROGRAM_NOT_ELIGIBLE'}
                    elif len(allowed_package_ids) == 1:
                        pkg_label = discount_code.package.name if discount_code.package else (Package.objects.using(alias).filter(id=allowed_package_ids[0]).values_list('name', flat=True).first() or 'the designated package')
                        return {'is_valid': False, 'reason': f"Coupon is only valid for package '{pkg_label}'.", 'reason_code': 'PACKAGE_NOT_ELIGIBLE'}
                    else:
                        return {'is_valid': False, 'reason': f"Coupon is not valid for package '{package.name}'. It is only applicable to the configured packages or programs.", 'reason_code': 'PACKAGE_NOT_ELIGIBLE'}
                else:
                    return {'is_valid': False, 'reason': 'Coupon is not valid for the packages in this order.', 'reason_code': 'PACKAGE_NOT_ELIGIBLE'}

        # User lock - restrict a coupon to specific member(s) via configuration['allowed_user_ids']
        allowed_user_ids = camp_cfg.get('allowed_user_ids') if isinstance(camp_cfg.get('allowed_user_ids'), list) else None
        if allowed_user_ids:
            uid = str(user_profile.id) if user_profile else None
            if not uid or uid not in [str(x) for x in allowed_user_ids]:
                return {'is_valid': False, 'reason': 'This coupon is not available for your account.', 'reason_code': 'USER_NOT_ELIGIBLE'}

        # Order-type gate (Dynamic & configurable across ALL platforms)
        allowed_order_types = camp_cfg.get('order_types') if isinstance(camp_cfg.get('order_types'), list) else None
        if allowed_order_types:
            otype = None
            if order_type:
                otype = str(order_type).strip().upper()
            elif order is not None and getattr(order, 'order_type', None):
                otype = str(order.order_type).strip().upper()
            elif user_profile is not None:
                member_state = cls.get_member_lifecycle_state(user_profile, alias)
                if member_state == 'ACTIVE':
                    otype = 'RENEWAL'
                elif member_state == 'EXPIRED':
                    otype = 'REJOIN'
                else:
                    otype = 'NEW_MEMBERSHIP'
            else:
                otype = 'NEW_MEMBERSHIP'

            if not cls.matches_order_type(otype, allowed_order_types):
                return {
                    'is_valid': False,
                    'reason': 'This coupon is not valid for this transaction type.',
                    'reason_code': 'ORDER_TYPE_NOT_ELIGIBLE',
                }

        # Member lifecycle gate (NEW, ACTIVE, EXPIRED/INACTIVE/REJOIN, NEW_OR_EXPIRED)
        allowed_member_types = camp_cfg.get('allowed_member_types') or camp_cfg.get('member_type')
        if allowed_member_types:
            member_state = cls.get_member_lifecycle_state(user_profile, alias)
            if not cls.matches_member_type(member_state, allowed_member_types):
                if member_state == 'ACTIVE':
                    reason = 'This offer is only for new members or rejoining expired members; you currently have an active membership.'
                elif member_state == 'EXPIRED':
                    reason = 'This offer is only for active members.'
                else:
                    reason = 'This offer is not eligible for your current membership status.'
                return {'is_valid': False, 'reason': reason, 'reason_code': 'MEMBER_TYPE_NOT_ELIGIBLE'}

        # Minimum order amount & Authoritative Subtotal calculation
        if order is not None and order.items.using(alias).exists():
            subtotal_dec = sum(item.unit_price_snapshot * item.quantity for item in order.items.using(alias).all())
        else:
            subtotal_dec = Decimal(str(order_subtotal or '0'))

        if campaign.minimum_order_amount and subtotal_dec < campaign.minimum_order_amount:
            return {
                'is_valid': False,
                'reason': f"Minimum order amount of {campaign.minimum_order_amount} required to use this coupon.",
                'reason_code': 'MINIMUM_ORDER_NOT_MET',
            }

        # Global usage limit (only count PAID orders)
        if campaign.usage_limit is not None:
            total_redemptions = DiscountRedemption.objects.using(alias).filter(
                campaign=campaign, order__status='PAID'
            ).count()
            if total_redemptions >= campaign.usage_limit:
                return {'is_valid': False, 'reason': 'Coupon usage limit has been reached.', 'reason_code': 'USAGE_LIMIT_EXCEEDED'}

        # Per-user limit (only count PAID orders)
        if campaign.per_user_limit is not None and user_profile:
            user_redemptions = DiscountRedemption.objects.using(alias).filter(
                campaign=campaign, user_profile=user_profile, order__status='PAID'
            ).count()
            if user_redemptions >= campaign.per_user_limit:
                return {'is_valid': False, 'reason': 'You have already reached the maximum usage limit for this coupon.', 'reason_code': 'PER_USER_LIMIT_EXCEEDED'}

        # Calculate discount amount
        discount_amount = Decimal('0.00')
        if campaign.discount_type == 'PERCENTAGE':
            pct = Decimal(str(campaign.discount_value))
            discount_amount = (subtotal_dec * (pct / Decimal('100.0'))).quantize(Decimal('0.01'))
            if campaign.max_discount and discount_amount > campaign.max_discount:
                discount_amount = campaign.max_discount
        elif campaign.discount_type == 'FIXED':
            discount_amount = min(Decimal(str(campaign.discount_value)), subtotal_dec).quantize(Decimal('0.01'))

        return {
            'is_valid': True,
            'code_id': str(discount_code.id),
            'code': discount_code.code,
            'coupon_code': discount_code.code,
            'campaign_id': str(campaign.id),
            'campaign_name': campaign.name,
            'discount_type': campaign.discount_type,
            'discount_value': str(campaign.discount_value),
            'discount_amount': str(discount_amount),
            'final_subtotal': str(max(Decimal('0.00'), subtotal_dec - discount_amount)),
        }

    @staticmethod
    def _evaluate_condition(condition: DiscountRuleCondition, context: Dict[str, Any]) -> bool:
        """
        Evaluate an individual condition row against context variables.
        Fails closed (returns False) if the relevant metric is unavailable (None).
        """
        ctype = condition.condition_type.upper()
        op = condition.operator
        target_num = condition.numeric_value

        actual_val = context.get(ctype.lower()) or context.get(ctype)
        if actual_val is None:
            if 'USAGE_PERCENTAGE' in ctype:
                actual_val = context.get('usage_percentage') or context.get('session_usage_percentage')
            elif 'CONSUMED' in ctype:
                actual_val = context.get('consumed_sessions') or context.get('sessions_consumed')
            elif 'REMAINING' in ctype:
                actual_val = context.get('remaining_sessions') or context.get('sessions_remaining')
            elif 'AGE' in ctype:
                actual_val = context.get('package_age_days') or context.get('membership_age_days')

        if actual_val is None:
            return False

        try:
            actual_num = Decimal(str(actual_val))
            if op == 'GREATER_THAN':
                return actual_num > target_num
            elif op == 'GREATER_THAN_OR_EQUAL':
                return actual_num >= target_num
            elif op == 'LESS_THAN':
                return actual_num < target_num
            elif op == 'LESS_THAN_OR_EQUAL':
                return actual_num <= target_num
            elif op == 'EQUALS':
                return actual_num == target_num
            elif op == 'NOT_EQUALS':
                return actual_num != target_num
        except (ValueError, TypeError):
            pass

        return False

    @classmethod
    def resolve_member_context(cls, user_profile: UserProfile, db_alias: Optional[str] = None, membership_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Authoritative member context resolver.
        Derives active membership, packages, branch, age, and session entitlement balances directly from domain models.
        Handles multiple active memberships, source-package scoping, unlimited entitlements, zero allocations,
        and unavailable metrics fail-closed behavior. Zero mock/fallback metrics.
        """
        alias = db_alias or (getattr(user_profile, '_state', None) and user_profile._state.db) or get_current_tenant_db_alias() or 'default'
        from .models_memberships import Membership, MembershipEntitlement, MembershipFreeze

        today = timezone.now().date()

        all_user_memberships = list(
            Membership.objects.using(alias)
            .filter(user_profile=user_profile)
            .select_related('package', 'package_version', 'home_branch', 'purchase_branch')
            .order_by('-created_at')
        )

        active_freeze_mem_ids = set(MembershipFreeze.objects.using(alias).filter(
            membership__user_profile=user_profile,
            status='ACTIVE',
            freeze_from__lte=today,
            freeze_until__gte=today
        ).values_list('membership_id', flat=True))

        def _derive_op_status(m):
            if not m:
                return 'INACTIVE'
            if m.id in active_freeze_mem_ids or m.status == 'FROZEN':
                return 'FROZEN'
            if m.status == 'ACTIVE' and m.end_date and m.end_date < today:
                return 'EXPIRED'
            return m.status

        # Truly active, unexpired, non-frozen memberships
        active_memberships = [
            m for m in all_user_memberships
            if m.status == 'ACTIVE'
            and (m.end_date is None or m.end_date >= today)
            and m.id not in active_freeze_mem_ids
        ]

        active_membership = None
        if membership_id:
            active_membership = next((m for m in all_user_memberships if str(m.id) == str(membership_id)), None)
        if not active_membership and active_memberships:
            active_membership = active_memberships[0]
        if not active_membership and all_user_memberships:
            active_membership = all_user_memberships[0]

        all_memberships_summary = [
            {
                'id': str(m.id),
                'membership_number': m.membership_number,
                'package_name': m.package.name,
                'package_id': str(m.package_id),
                'status': _derive_op_status(m),
            }
            for m in (active_memberships if active_memberships else all_user_memberships)
        ]

        active_package_ids = [str(m.package_id) for m in active_memberships]
        if not active_package_ids and active_membership and active_membership.package_id:
            active_package_ids = [str(active_membership.package_id)]

        if not active_membership:
            # Fallback to resolve member's branch from profile, user, or organization default
            user = user_profile.user
            branch = (
                user_profile.preferred_branch
                or (getattr(user, 'home_branch', None) if user else None)
            )
            if not branch and user:
                from .models import UserBranch
                ub = UserBranch.objects.using(alias).filter(user=user, is_active=True).select_related('branch').first()
                if ub and ub.branch:
                    branch = ub.branch
            if not branch and user and getattr(user, 'organization_id', None):
                from .models import Branch
                branch = Branch.objects.using(alias).filter(organization_id=user.organization_id, status='ACTIVE').first()

            account_start = user_profile.joining_date or (user_profile.created_at.date() if user_profile.created_at else None) or (user.created_at.date() if user and user.created_at else None)
            account_age_days = max(0, (today - account_start).days) if account_start else 0
            mem_num = user_profile.member_number or f"MEM-{str(user_profile.id)[:8].upper()}"
            branch_name = branch.name if branch else 'Main Branch'
            branch_id = str(branch.id) if branch else None

            return {
                'has_membership': False,
                'membership_id': None,
                'membership_number': mem_num,
                'membership_status': 'INACTIVE',
                'current_package_id': None,
                'current_package_name': 'None (No active package)',
                'current_package_version_id': None,
                'branch_id': branch_id,
                'branch_name': branch_name,
                'start_date': str(account_start) if account_start else None,
                'end_date': None,
                'package_age_days': account_age_days,
                'remaining_days': 0,
                'sessions_allocated': 0.0,
                'sessions_consumed': 0.0,
                'sessions_remaining': 0.0,
                'usage_percentage': 0.0,
                'is_unlimited': False,
                'metrics_available': True,
                'active_package_ids': [],
                'all_memberships': all_memberships_summary,
            }

        membership_op_status = _derive_op_status(active_membership)
        package_age_days = max(0, (today - active_membership.start_date).days) if active_membership.start_date else None
        remaining_days = max(0, (active_membership.end_date - today).days) if active_membership.end_date else 0
        if membership_op_status == 'EXPIRED':
            remaining_days = 0
        branch = active_membership.home_branch or active_membership.purchase_branch

        entitlements = list(MembershipEntitlement.objects.using(alias).filter(membership=active_membership))
        has_sessions = False
        is_unlimited = False
        total_allocated = Decimal('0.00')
        total_consumed = Decimal('0.00')

        for ent in entitlements:
            if ent.is_unlimited:
                is_unlimited = True
                has_sessions = True
            elif ent.allocated_units is not None:
                has_sessions = True
                total_allocated += ent.allocated_units
                total_consumed += (ent.consumed_units or Decimal('0.00'))

        if is_unlimited:
            sessions_allocated = None
            sessions_consumed = float(total_consumed)
            sessions_remaining = None
            usage_pct = None
            metrics_available = False
        elif has_sessions and total_allocated > Decimal('0.00'):
            sessions_allocated = float(total_allocated)
            sessions_consumed = float(total_consumed)
            sessions_remaining = float(max(Decimal('0.00'), total_allocated - total_consumed))
            usage_pct = float(min(Decimal('100.00'), (total_consumed / total_allocated * Decimal('100.0')).quantize(Decimal('0.01'))))
            metrics_available = True
        elif has_sessions:
            # Zero allocations: total_allocated == 0.00
            sessions_allocated = 0.0
            sessions_consumed = float(total_consumed)
            sessions_remaining = 0.0
            usage_pct = None  # Undefined: cannot divide by zero
            metrics_available = False
        else:
            sessions_allocated = None
            sessions_consumed = None
            sessions_remaining = None
            usage_pct = None
            metrics_available = False

        return {
            'has_membership': True,
            'membership_id': str(active_membership.id),
            'membership_number': active_membership.membership_number,
            'membership_status': membership_op_status,
            'current_package_id': str(active_membership.package_id),
            'current_package_name': active_membership.package.name,
            'current_package_version_id': str(active_membership.package_version_id),
            'branch_id': str(branch.id) if branch else None,
            'branch_name': branch.name if branch else None,
            'start_date': str(active_membership.start_date) if active_membership.start_date else None,
            'end_date': str(active_membership.end_date) if active_membership.end_date else None,
            'package_age_days': package_age_days,
            'remaining_days': remaining_days,
            'sessions_allocated': sessions_allocated,
            'sessions_consumed': sessions_consumed,
            'sessions_remaining': sessions_remaining,
            'usage_percentage': usage_pct,
            'is_unlimited': is_unlimited,
            'metrics_available': metrics_available,
            'active_package_ids': active_package_ids,
            'all_memberships': all_memberships_summary,
        }

    @classmethod
    def evaluate_member_offers(
        cls,
        user_profile: UserProfile,
        context: Optional[Dict[str, Any]] = None,
        branch: Optional[Branch] = None,
        target_package: Optional[Package] = None,
        db_alias: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Evaluate active eligibility rules and return qualified dynamic offers and upgrade incentives.
        Guarantees thread-local tenant context restoration via try/finally block.
        """
        alias = db_alias or (getattr(user_profile, '_state', None) and user_profile._state.db) or get_current_tenant_db_alias() or 'default'
        previous_alias = get_current_tenant_db_alias()
        try:
            if alias and alias != 'default':
                set_tenant_db_alias(alias)

            ctx = dict(context or {})
            # Authoritative context auto-resolution if missing from caller
            if user_profile and ('usage_percentage' not in ctx or 'sessions_consumed' not in ctx or 'package_age_days' not in ctx or 'active_package_ids' not in ctx):
                resolved = cls.resolve_member_context(user_profile, db_alias=alias)
                if resolved.get('has_membership'):
                    if 'current_package_id' not in ctx or not ctx['current_package_id']:
                        ctx['current_package_id'] = resolved.get('current_package_id')
                    if 'active_package_ids' not in ctx:
                        ctx['active_package_ids'] = resolved.get('active_package_ids', [])
                    if 'usage_percentage' not in ctx or ctx['usage_percentage'] is None:
                        ctx['usage_percentage'] = resolved.get('usage_percentage')
                    if 'sessions_consumed' not in ctx or ctx['sessions_consumed'] is None:
                        ctx['sessions_consumed'] = resolved.get('sessions_consumed')
                    if 'sessions_remaining' not in ctx or ctx['sessions_remaining'] is None:
                        ctx['sessions_remaining'] = resolved.get('sessions_remaining')
                    if 'package_age_days' not in ctx or ctx['package_age_days'] is None:
                        ctx['package_age_days'] = resolved.get('package_age_days')

            now = timezone.now()

            from django.db.models import Prefetch

            rules = DiscountEligibilityRule.objects.using(alias).filter(
                organization=user_profile.user.organization,
                status='ACTIVE',
                valid_from__lte=now,
            ).prefetch_related(
                Prefetch('conditions', queryset=DiscountRuleCondition.objects.using(alias)),
                Prefetch('actions', queryset=DiscountRuleAction.objects.using(alias)),
            )

            # Filter validity end
            rules = [r for r in rules if not r.valid_until or r.valid_until >= now]

            matching_offers = []

            for rule in rules:
                # Parse scope metadata from rule.configuration (with fallback to legacy description comment)
                cfg = getattr(rule, 'configuration', None) or {}
                scope_branch_ids = [str(x) for x in cfg.get('branch_ids', []) if x]
                scope_pkg_ids = [str(x) for x in cfg.get('package_ids', []) if x]
                scope_prog_ids = [str(x) for x in cfg.get('program_ids', []) if x]

                if not scope_branch_ids and not scope_pkg_ids and not scope_prog_ids and rule.description and '<!-- SCOPE:' in rule.description:
                    try:
                        import json, re
                        m = re.search(r'<!-- SCOPE:\s*(\{.*?\})\s*-->', rule.description)
                        if m:
                            scope_data = json.loads(m.group(1))
                            scope_branch_ids = [str(x) for x in scope_data.get('branch_ids', []) if x]
                            scope_pkg_ids = [str(x) for x in scope_data.get('package_ids', []) if x]
                            scope_prog_ids = [str(x) for x in scope_data.get('program_ids', []) if x]
                    except Exception:
                        pass

                # Branch check
                if scope_branch_ids:
                    if branch and str(branch.id) not in scope_branch_ids:
                        continue
                elif rule.branch and branch and rule.branch_id != branch.id:
                    continue

                # Program check
                if scope_prog_ids:
                    if target_package:
                        pkg_prog_id = str(getattr(target_package, 'program_id', None) or '')
                        if not pkg_prog_id or pkg_prog_id not in scope_prog_ids:
                            continue

                # Target package check
                if scope_pkg_ids:
                    if target_package and str(target_package.id) not in scope_pkg_ids:
                        continue
                elif rule.target_package and target_package and rule.target_package_id != target_package.id:
                    continue

                # Source package check across member active packages
                current_pkg_id = ctx.get('current_package_id')
                active_pkg_ids = ctx.get('active_package_ids') or ([str(current_pkg_id)] if current_pkg_id else [])
                if rule.source_package:
                    req_pkg_id = str(rule.source_package_id)
                    if not active_pkg_ids or req_pkg_id not in [str(p) for p in active_pkg_ids]:
                        continue

                # Conditions evaluation (fails closed on unavailable metrics)
                conditions = list(rule.conditions.all())
                if conditions:
                    matches = [cls._evaluate_condition(c, ctx) for c in conditions]
                    if rule.evaluation_mode == 'ALL_CONDITIONS':
                        is_qualified = all(matches)
                    else:
                        is_qualified = any(matches)
                else:
                    is_qualified = True

                if is_qualified:
                    actions = list(rule.actions.select_related('discount_campaign', 'discount_code').all())
                    for act in actions:
                        camp = act.discount_campaign
                        code_obj = act.discount_code
                        code_str = code_obj.code if code_obj else (
                            camp.codes.filter(status='ACTIVE').values_list('code', flat=True).first() if camp else None
                        )
                        pct = act.discount_percentage if act.discount_percentage else (
                            camp.discount_value if (camp and camp.discount_type == 'PERCENTAGE') else None
                        )
                        amt = act.discount_amount if act.discount_amount else (
                            camp.discount_value if (camp and camp.discount_type == 'FIXED') else None
                        )
                        max_d = act.maximum_discount if act.maximum_discount else (
                            camp.max_discount if camp else None
                        )
                        matching_offers.append({
                            'rule_id': str(rule.id),
                            'rule_name': rule.name,
                            'rule_type': rule.rule_type,
                            'action_type': act.action_type,
                            'message': act.message or rule.description or rule.name,
                            'campaign_id': str(camp.id) if camp else None,
                            'campaign_name': camp.name if camp else None,
                            'coupon_code': code_str,
                            'discount_percentage': str(pct) if pct else None,
                            'discount_amount': str(amt) if amt else None,
                            'maximum_discount': str(max_d) if max_d else None,
                            'auto_apply': act.auto_apply,
                            'priority': rule.priority,
                        })

            matching_offers.sort(key=lambda x: x['priority'])
            return matching_offers
        finally:
            set_tenant_db_alias(previous_alias)

    @classmethod
    def recalculate_order_totals(
        cls,
        order: Order,
        discount_amount: Decimal,
        db_alias: Optional[str] = None,
    ) -> None:
        """
        Authoritative checkout calculation engine.
        Recomputes item-level discounts, item-level taxes, and order totals.

        Rules:
        1. Mixed tax rates: Each line item calculates tax using its own snapshot (or org fallback if snapshot is None).
        2. Zero-rated items: Explicit 0.00% tax_percent_snapshot is preserved and not overwritten by org settings.
        3. Missing configuration: If an item tax_percent_snapshot is None, falls back to OrganizationSettings.tax_rate_pct.
           If OrganizationSettings.tax_rate_pct is also None/missing, defaults to Decimal('0.00') with an explicit note.
        4. Proportional discount allocation: Order discount is allocated across line items proportionally to line subtotal.
        5. Exact rounding: Rounding cents are assigned to the last line item so sum(item_discounts) == total discount.
        6. Idempotence: Repeated redemption or recalculation recomputes deterministically from immutable base prices.
        """
        alias = db_alias or 'default'
        items = list(order.items.using(alias).all().order_by('-total_amount', 'id'))

        from .models_govern import OrganizationSettings
        org_settings = OrganizationSettings.objects.using(alias).filter(organization=order.branch.organization).first()
        org_default_tax = (org_settings.tax_rate_pct if (org_settings and org_settings.tax_rate_pct is not None) else Decimal('0.00'))

        if items:
            line_subtotals = [item.unit_price_snapshot * item.quantity for item in items]
            order_subtotal = sum(line_subtotals)
            effective_discount = min(discount_amount, order_subtotal)

            allocated_discounts = []
            if order_subtotal > Decimal('0.00') and effective_discount > Decimal('0.00'):
                running_allocated = Decimal('0.00')
                for i, sub in enumerate(line_subtotals):
                    if i == len(items) - 1:
                        item_disc = effective_discount - running_allocated
                    else:
                        item_disc = (effective_discount * sub / order_subtotal).quantize(Decimal('0.01'))
                        running_allocated += item_disc
                    allocated_discounts.append(max(Decimal('0.00'), item_disc))
            else:
                allocated_discounts = [Decimal('0.00')] * len(items)

            total_item_tax = Decimal('0.00')
            total_item_amount = Decimal('0.00')

            for item, item_disc, line_sub in zip(items, allocated_discounts, line_subtotals):
                line_after_discount = max(Decimal('0.00'), line_sub - item_disc)

                # Explicitly zero-rated is preserved; None falls back to org default tax
                if item.tax_percent_snapshot is not None:
                    item_tax_pct = item.tax_percent_snapshot
                else:
                    item_tax_pct = org_default_tax

                line_tax = (line_after_discount * item_tax_pct / Decimal('100.00')).quantize(Decimal('0.01'))
                line_total = line_after_discount + line_tax

                item.discount_amount = item_disc
                item.tax_amount = line_tax
                item.total_amount = line_total
                item.save(using=alias, update_fields=['discount_amount', 'tax_amount', 'total_amount', 'updated_at'])

                total_item_tax += line_tax
                total_item_amount += line_total

            order.subtotal = order_subtotal
            order.discount_amount = effective_discount
            order.tax_amount = total_item_tax
            order.total_amount = max(Decimal('0.00'), total_item_amount - getattr(order, 'reward_amount', Decimal('0.00')))
        else:
            # Standalone order without line items
            effective_discount = min(discount_amount, order.subtotal)
            net_after_discount = max(Decimal('0.00'), order.subtotal - effective_discount - getattr(order, 'reward_amount', Decimal('0.00')))
            if order.subtotal > Decimal('0.00') and order.tax_amount > Decimal('0.00'):
                tax_pct = (order.tax_amount / order.subtotal).quantize(Decimal('0.0001'))
            elif org_settings and org_settings.tax_rate_pct is not None:
                tax_pct = (org_settings.tax_rate_pct / Decimal('100.00')).quantize(Decimal('0.0001'))
            else:
                tax_pct = Decimal('0.0000')

            order.discount_amount = effective_discount
            order.tax_amount = (net_after_discount * tax_pct).quantize(Decimal('0.01'))
            order.total_amount = net_after_discount + order.tax_amount

        order.save(using=alias, update_fields=['subtotal', 'discount_amount', 'tax_amount', 'total_amount', 'updated_at'])

    @classmethod
    def redeem_coupon(
        cls,
        order: Order,
        code_str: str,
        user_profile: UserProfile,
        created_by_user=None,
        db_alias: Optional[str] = None,
    ) -> DiscountRedemption:
        """
        Atomically validate and record coupon redemption on an order.
        Enforces strict row locks on Order, Campaign, and Code to prevent concurrent double redemptions.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            # 1. Row-lock the order to serialize concurrent submissions on the same order
            order = Order.objects.using(alias).select_for_update().get(id=order.id)
            if order.status != 'PENDING_PAYMENT':
                raise ValidationError(f"Cannot redeem coupon on order with status '{order.status}'. Order must be PENDING_PAYMENT.")

            # 2. Scope & isolation checks
            if user_profile and user_profile.user.organization_id != order.branch.organization_id:
                raise ValidationError("Cross-organization redemption prohibited: member does not belong to order organization.")
            if order.user_profile_id and user_profile and order.user_profile_id != user_profile.id:
                raise ValidationError("Order belongs to a different member.")

            # 3. Check for existing redemptions on this order
            existing_redemptions = DiscountRedemption.objects.using(alias).filter(order=order)
            if existing_redemptions.exists():
                raise ValidationError("A coupon has already been redeemed for this order.")

            # 4. Validate coupon using authoritative order data
            val_result = cls.validate_coupon(
                code_str=code_str,
                user_profile=user_profile,
                order_subtotal=order.subtotal,
                branch=order.branch,
                db_alias=alias,
                order=order,
            )
            if not val_result['is_valid']:
                raise ValidationError(val_result['reason'])

            discount_amount = Decimal(val_result['discount_amount'])

            # 5. Lock code and campaign to serialize concurrent redemptions against usage limits
            discount_code = DiscountCode.objects.using(alias).select_for_update().get(id=val_result['code_id'])
            campaign = DiscountCampaign.objects.using(alias).select_for_update().get(id=discount_code.campaign_id)

            if campaign.organization_id != order.branch.organization_id:
                raise ValidationError("Cross-organization redemption prohibited: campaign does not belong to order organization.")

            if campaign.usage_limit is not None:
                total_redemptions = DiscountRedemption.objects.using(alias).filter(
                    campaign=campaign, order__status='PAID'
                ).count()
                if total_redemptions >= campaign.usage_limit:
                    raise ValidationError("Coupon usage limit has been reached.")
            if campaign.per_user_limit is not None and user_profile:
                user_redemptions = DiscountRedemption.objects.using(alias).filter(
                    campaign=campaign, user_profile=user_profile, order__status='PAID'
                ).count()
                if user_redemptions >= campaign.per_user_limit:
                    raise ValidationError("You have reached your redemption limit for this coupon.")

            # 6. Record redemption
            redemption = DiscountRedemption.objects.using(alias).create(
                discount_code=discount_code,
                campaign=campaign,
                user_profile=user_profile,
                order=order,
                discount_amount=discount_amount,
                eligibility_snapshot=val_result,
            )

            # 7. Update order and items using authoritative tax engine
            cls.recalculate_order_totals(order=order, discount_amount=discount_amount, db_alias=alias)

            # 8. Business audit & domain outbox
            record_business_audit(
                organization=order.branch.organization,
                module='commerce',
                action_code='COUPON_REDEEMED',
                entity_type='DiscountRedemption',
                entity_id=redemption.id,
                branch=order.branch,
                actor_user=created_by_user if isinstance(created_by_user, TenantUser) else None,
                metadata={
                    'order_id': str(order.id),
                    'code': discount_code.code,
                    'discount_amount': str(discount_amount),
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=order.branch.organization,
                event_type='commerce.coupon.redeemed',
                aggregate_type='Order',
                aggregate_id=order.id,
                payload={
                    'redemption_id': str(redemption.id),
                    'code': discount_code.code,
                    'discount_amount': str(discount_amount),
                    'order_id': str(order.id),
                },
                db_alias=alias,
            )

            return redemption
