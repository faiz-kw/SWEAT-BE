"""
apps/tenant_core/services_discounts.py — DiscountCouponEngineService for Layer 2 Module H

Capabilities:
- Percentage, fixed amount, and capped discounts.
- Branch and package scoped validations.
- Global and per-user redemption limits.
- Dynamic eligibility rule evaluation:
  - Consumed sessions, remaining sessions, session usage percentage.
  - Package age, membership remaining days.
  - Current package to target package upgrade rules.
- Transactional redemption audit snapshot and outbox event publishing.
"""

import logging
from decimal import Decimal
from typing import Optional, Dict, Any, List
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
from .context import get_current_tenant_db_alias

logger = logging.getLogger(__name__)


class DiscountCouponEngineService:
    """
    High performance, deterministic discount evaluation and coupon redemption engine.
    """

    @staticmethod
    def validate_coupon(
        code_str: str,
        user_profile: UserProfile,
        order_subtotal: Decimal,
        branch: Optional[Branch] = None,
        package: Optional[Package] = None,
    ) -> Dict[str, Any]:
        """
        Validate a coupon code against campaign rules, usage limits, and scopes.
        """
        code_clean = (code_str or '').strip().upper()
        if not code_clean:
            return {'is_valid': False, 'reason': 'Coupon code is required.'}

        try:
            discount_code = DiscountCode.objects.select_related('campaign', 'branch', 'package').get(
                code__iexact=code_clean
            )
        except DiscountCode.DoesNotExist:
            return {'is_valid': False, 'reason': f"Coupon code '{code_str}' is invalid or does not exist."}

        if discount_code.status != 'ACTIVE':
            return {'is_valid': False, 'reason': f"Coupon code '{code_str}' is {discount_code.status.lower()}."}

        campaign = discount_code.campaign
        if not campaign.is_currently_valid():
            return {'is_valid': False, 'reason': f"Campaign '{campaign.name}' is inactive or expired."}

        # Branch scope
        if discount_code.branch and branch and discount_code.branch_id != branch.id:
            return {'is_valid': False, 'reason': f"Coupon is only valid at {discount_code.branch.name}."}

        # Package scope
        if discount_code.package and package and discount_code.package_id != package.id:
            return {'is_valid': False, 'reason': f"Coupon is only valid for package '{discount_code.package.name}'."}

        # Minimum order amount
        subtotal_dec = Decimal(str(order_subtotal or '0'))
        if campaign.minimum_order_amount and subtotal_dec < campaign.minimum_order_amount:
            return {
                'is_valid': False,
                'reason': f"Minimum order amount of {campaign.minimum_order_amount} required to use this coupon.",
            }

        # Global usage limit
        if campaign.usage_limit is not None:
            total_redemptions = DiscountRedemption.objects.filter(campaign=campaign).count()
            if total_redemptions >= campaign.usage_limit:
                return {'is_valid': False, 'reason': 'Coupon usage limit has been reached.'}

        # Per-user limit
        if campaign.per_user_limit is not None and user_profile:
            user_redemptions = DiscountRedemption.objects.filter(
                campaign=campaign, user_profile=user_profile
            ).count()
            if user_redemptions >= campaign.per_user_limit:
                return {'is_valid': False, 'reason': 'You have already reached the maximum usage limit for this coupon.'}

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
        """
        ctype = condition.condition_type.upper()
        op = condition.operator
        target_num = condition.numeric_value

        # Fetch actual value from context
        actual_val = context.get(ctype.lower()) or context.get(ctype)
        if actual_val is None:
            # Check mappings
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
    def evaluate_member_offers(
        cls,
        user_profile: UserProfile,
        context: Optional[Dict[str, Any]] = None,
        branch: Optional[Branch] = None,
        target_package: Optional[Package] = None,
    ) -> List[Dict[str, Any]]:
        """
        Evaluate active eligibility rules and return qualified dynamic offers and upgrade incentives.
        """
        ctx = context or {}
        now = timezone.now()

        rules = DiscountEligibilityRule.objects.filter(
            organization=user_profile.user.organization,
            status='ACTIVE',
            valid_from__lte=now,
        ).prefetch_related('conditions', 'actions')

        # Filter validity end
        rules = [r for r in rules if not r.valid_until or r.valid_until >= now]

        matching_offers = []

        for rule in rules:
            # Branch check
            if rule.branch and branch and rule.branch_id != branch.id:
                continue

            # Target package check
            if rule.target_package and target_package and rule.target_package_id != target_package.id:
                continue

            # Source package check
            current_pkg_id = ctx.get('current_package_id')
            if rule.source_package and current_pkg_id and str(rule.source_package_id) != str(current_pkg_id):
                continue

            # Conditions
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
                actions = list(rule.actions.all())
                for act in actions:
                    matching_offers.append({
                        'rule_id': str(rule.id),
                        'rule_name': rule.name,
                        'rule_type': rule.rule_type,
                        'action_type': act.action_type,
                        'message': act.message or rule.description or rule.name,
                        'discount_percentage': str(act.discount_percentage) if act.discount_percentage else None,
                        'discount_amount': str(act.discount_amount) if act.discount_amount else None,
                        'maximum_discount': str(act.maximum_discount) if act.maximum_discount else None,
                        'auto_apply': act.auto_apply,
                        'priority': rule.priority,
                    })

        matching_offers.sort(key=lambda x: x['priority'])
        return matching_offers

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
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            val_result = cls.validate_coupon(
                code_str=code_str,
                user_profile=user_profile,
                order_subtotal=order.subtotal,
                branch=order.branch,
            )
            if not val_result['is_valid']:
                raise ValidationError(val_result['reason'])

            discount_amount = Decimal(val_result['discount_amount'])
            discount_code = DiscountCode.objects.using(alias).get(id=val_result['code_id'])
            # Lock campaign to serialize concurrent redemptions against usage limits
            campaign = DiscountCampaign.objects.using(alias).select_for_update().get(id=discount_code.campaign_id)
            if campaign.usage_limit is not None:
                total_redemptions = DiscountRedemption.objects.using(alias).filter(campaign=campaign).count()
                if total_redemptions >= campaign.usage_limit:
                    raise ValidationError("Coupon usage limit has been reached.")
            if campaign.per_user_limit is not None and user_profile:
                user_redemptions = DiscountRedemption.objects.using(alias).filter(campaign=campaign, user_profile=user_profile).count()
                if user_redemptions >= campaign.per_user_limit:
                    raise ValidationError("You have reached your redemption limit for this coupon.")

            redemption = DiscountRedemption.objects.using(alias).create(
                discount_code=discount_code,
                campaign=campaign,
                user_profile=user_profile,
                order=order,
                discount_amount=discount_amount,
                eligibility_snapshot=val_result,
            )

            # Update order discount_amount and total_amount
            order.discount_amount = discount_amount
            tax_pct = Decimal('0.18')
            if order.subtotal > Decimal('0.00'):
                tax_pct = (order.tax_amount / order.subtotal).quantize(Decimal('0.0001'))

            net_after_discount = max(Decimal('0.00'), order.subtotal - discount_amount - getattr(order, 'reward_amount', Decimal('0.00')))
            order.tax_amount = (net_after_discount * tax_pct).quantize(Decimal('0.01'))
            order.total_amount = net_after_discount + order.tax_amount
            order.save(using=alias, update_fields=['discount_amount', 'tax_amount', 'total_amount', 'updated_at'])

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
