"""
apps/tenant_core/services_rewards.py — Business Services for Layer 2 Modules L & M: Referrals & Rewards

Implements:
1. Double-Entry Rewards & Loyalty Points / Credit Ledger (Earn, Redeem, Reversal, Adjustment).
2. Referral Code Generation and Attribution.
3. Referral Lifecycle (Invited -> Registered -> Qualified -> Rewarded).
4. Automated Benefit Allocation (Referrer & Referee Reward Granting).
5. Outbox Events and Business Audit Logging.
"""

import uuid
import logging
from decimal import Decimal
from typing import Optional, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_referrals import (
    ReferralProgram,
    ReferralIdentifier,
    Referral,
    ReferralQualificationRule,
    ReferralBenefitRule,
)
from .models_rewards import (
    RewardAccount,
    RewardLedger,
    OrderRewardRedemption,
)
from .models_crm import UserProfile
from .models_users import TenantUser
from .models_commerce import Order
from .models_memberships import Membership
from .models_govern import OrganizationSettings
from .services_reliability import record_business_audit, enqueue_outbox_event
from .context import get_current_tenant_db_alias

logger = logging.getLogger(__name__)


class ReferralRewardService:
    """
    Core business engine for Loyalty Rewards, Credit Ledgers, and Referral Lifecycle.
    """

    # -------------------------------------------------------------------------
    # REWARD LEDGER & ACCOUNTS
    # -------------------------------------------------------------------------

    @classmethod
    def get_or_create_reward_account(
        cls,
        user_profile: UserProfile,
        db_alias: Optional[str] = None,
    ) -> RewardAccount:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        account = RewardAccount.objects.using(alias).filter(user_profile=user_profile).first()
        if not account:
            account = RewardAccount.objects.using(alias).create(
                user_profile=user_profile,
                points_balance=Decimal('0.00'),
                credit_balance=Decimal('0.00'),
                lifetime_earned=Decimal('0.00'),
                lifetime_redeemed=Decimal('0.00'),
                status='ACTIVE',
            )
        return account

    @classmethod
    @transaction.atomic
    def earn_rewards(
        cls,
        user_profile: UserProfile,
        reward_type: str,
        quantity: Decimal,
        reason_code: str = 'ACTIVITY_EARN',
        reason: Optional[str] = None,
        order: Optional[Order] = None,
        referral: Optional[Referral] = None,
        membership: Optional[Membership] = None,
        expires_at=None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> RewardLedger:
        """
        Credits points or wallet credit to a member's reward account and appends to the immutable ledger.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        account = cls.get_or_create_reward_account(user_profile, db_alias=alias)

        if account.status != 'ACTIVE':
            raise ValidationError(f"Cannot credit rewards: Reward account is {account.status}.")

        if quantity <= Decimal('0.00'):
            raise ValidationError("Earn quantity must be strictly positive.")

        if reward_type == 'POINTS':
            account.points_balance += quantity
            balance_after = account.points_balance
        elif reward_type == 'CREDIT':
            account.credit_balance += quantity
            balance_after = account.credit_balance
        else:
            balance_after = Decimal('0.00')

        account.lifetime_earned += quantity
        account.save(using=alias, update_fields=['points_balance', 'credit_balance', 'lifetime_earned', 'updated_at'])

        ledger = RewardLedger.objects.using(alias).create(
            reward_account=account,
            user_profile=user_profile,
            transaction_type='EARN',
            reward_type=reward_type,
            quantity=quantity,
            referral=referral,
            order=order,
            membership=membership,
            reason_code=reason_code,
            reason=reason or f"Earned {quantity} {reward_type}",
            balance_after=balance_after,
            expires_at=expires_at,
            created_by_user=created_by_user,
        )

        org = user_profile.user.organization
        record_business_audit(
            organization=org,
            module='rewards',
            action_code='REWARD_EARNED',
            entity_type='RewardAccount',
            entity_id=account.id,
            actor_user=created_by_user,
            metadata={
                'user_profile_id': str(user_profile.id),
                'reward_type': reward_type,
                'quantity': str(quantity),
                'balance_after': str(balance_after),
                'reason_code': reason_code,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=org,
            event_type='rewards.earned',
            aggregate_type='RewardAccount',
            aggregate_id=account.id,
            payload={
                'account_id': str(account.id),
                'user_profile_id': str(user_profile.id),
                'reward_type': reward_type,
                'quantity': str(quantity),
                'balance_after': str(balance_after),
            },
            db_alias=alias,
        )

        return ledger

    @classmethod
    def redeem_rewards(
        cls,
        user_profile: UserProfile,
        reward_type: str,
        quantity: Decimal,
        monetary_value: Optional[Decimal] = None,
        order: Optional[Order] = None,
        reason_code: str = 'ORDER_REDEMPTION',
        reason: Optional[str] = None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[RewardLedger, Optional[OrderRewardRedemption]]:
        """
        Debits points or wallet credit for checkout redemption or gift card conversions.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            account = cls.get_or_create_reward_account(user_profile, db_alias=alias)
            # Lock reward account to serialize concurrent redemptions against balance
            account = RewardAccount.objects.using(alias).select_for_update().get(id=account.id)

            if account.status != 'ACTIVE':
                raise ValidationError(f"Cannot redeem rewards: Reward account is {account.status}.")

            if quantity <= Decimal('0.00'):
                raise ValidationError("Redeem quantity must be strictly positive.")

            if reward_type == 'POINTS':
                if account.points_balance < quantity:
                    raise ValidationError(
                        f"Insufficient points. Available: {account.points_balance}, Required: {quantity}."
                    )
                account.points_balance -= quantity
                balance_after = account.points_balance
            elif reward_type == 'CREDIT':
                if account.credit_balance < quantity:
                    raise ValidationError(
                        f"Insufficient credit balance. Available: {account.credit_balance}, Required: {quantity}."
                    )
                account.credit_balance -= quantity
                balance_after = account.credit_balance
            else:
                balance_after = Decimal('0.00')

            account.lifetime_redeemed += quantity
            account.save(using=alias, update_fields=['points_balance', 'credit_balance', 'lifetime_redeemed', 'updated_at'])

            ledger = RewardLedger.objects.using(alias).create(
                reward_account=account,
                user_profile=user_profile,
                transaction_type='REDEEM',
                reward_type=reward_type,
                quantity=-quantity,
                order=order,
                reason_code=reason_code,
                reason=reason or f"Redeemed {quantity} {reward_type}",
                balance_after=balance_after,
                created_by_user=created_by_user,
            )

            redemption = None
            if order:
                effective_val = monetary_value if monetary_value is not None else quantity
                redemption = OrderRewardRedemption.objects.using(alias).create(
                    order=order,
                    reward_account=account,
                    reward_ledger=ledger,
                    reward_type=reward_type,
                    units_redeemed=quantity,
                    monetary_value=effective_val,
                )

            org = user_profile.user.organization
            record_business_audit(
                organization=org,
                module='rewards',
                action_code='REWARD_REDEEMED',
                entity_type='RewardAccount',
                entity_id=account.id,
                actor_user=created_by_user,
                metadata={
                    'user_profile_id': str(user_profile.id),
                    'reward_type': reward_type,
                    'quantity': str(quantity),
                    'balance_after': str(balance_after),
                    'order_id': str(order.id) if order else None,
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=org,
                event_type='rewards.redeemed',
                aggregate_type='RewardAccount',
                aggregate_id=account.id,
                payload={
                    'account_id': str(account.id),
                    'user_profile_id': str(user_profile.id),
                    'reward_type': reward_type,
                    'quantity': str(quantity),
                    'balance_after': str(balance_after),
                },
                db_alias=alias,
            )

            return ledger, redemption

    @classmethod
    def get_reward_config(cls, organization=None, db_alias: Optional[str] = None) -> dict:
        """
        Retrieves the reward points policy from OrganizationSettings.membership_config['reward_points'].
        Defaults to enabled, 100 INR = 1 point, 1 point = 1.0 INR redemption value.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        org_settings = None
        if organization:
            org_settings = OrganizationSettings.objects.using(alias).filter(organization=organization).first()
        if not org_settings:
            org_settings = OrganizationSettings.objects.using(alias).first()

        cfg = {}
        if org_settings and isinstance(org_settings.membership_config, dict):
            cfg = org_settings.membership_config.get('reward_points') or {}

        return {
            'is_enabled': cfg.get('is_enabled', True),
            'earn_rate_rupees': int(cfg.get('earn_rate_rupees', 100) or 100),
            'points_per_rate': int(cfg.get('points_per_rate', 1) or 1),
            'point_value_rupees': Decimal(str(cfg.get('point_value_rupees', '1.0') or '1.0')),
            'max_redemption_percent': Decimal(str(cfg.get('max_redemption_percent', '80') or '80')),
        }

    @classmethod
    def calculate_max_redeemable_points(
        cls,
        order_amount: Decimal,
        user_profile: UserProfile,
        organization=None,
        db_alias: Optional[str] = None,
    ) -> dict:
        """
        Calculates the maximum reward points a member is allowed to redeem on an order.
        Enforces:
        1. Maximum 80% of net order amount can be paid using points (or org config max_redemption_percent).
        2. Member must always pay at least the remaining 20% via cash/UPI/POS/Razorpay.
        3. Cannot redeem more points than currently available in RewardAccount.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        config = cls.get_reward_config(organization=organization, db_alias=alias)
        account = cls.get_or_create_reward_account(user_profile, db_alias=alias)

        point_value = config['point_value_rupees']
        max_pct = config['max_redemption_percent'] / Decimal('100.00')

        available_points = account.points_balance
        available_monetary_value = (available_points * point_value).quantize(Decimal('0.01'))

        # Maximum monetary discount allowed by the 80% ceiling
        net_payable_base = max(Decimal('0.00'), order_amount)
        max_discount_allowed = (net_payable_base * max_pct).quantize(Decimal('0.01'))

        # Maximum points that translate to at most max_discount_allowed
        if point_value > Decimal('0.00'):
            max_points_by_ceiling = (max_discount_allowed / point_value).quantize(Decimal('1.00'))
        else:
            max_points_by_ceiling = Decimal('0.00')

        # Allowed points is min(available, ceiling)
        allowed_points = max(Decimal('0.00'), min(available_points, max_points_by_ceiling))
        allowed_monetary_discount = (allowed_points * point_value).quantize(Decimal('0.01'))
        # Ensure discount does not exceed 80% due to any rounding
        allowed_monetary_discount = min(allowed_monetary_discount, max_discount_allowed)
        min_cash_payable = max(Decimal('0.00'), net_payable_base - allowed_monetary_discount)

        return {
            'available_points': available_points,
            'available_monetary_value': available_monetary_value,
            'max_redemption_percent': config['max_redemption_percent'],
            'max_discount_allowed': max_discount_allowed,
            'max_redeemable_points': allowed_points,
            'max_redeemable_discount': allowed_monetary_discount,
            'min_cash_payable': min_cash_payable,
            'point_value_rupees': point_value,
        }

    @classmethod
    def apply_points_to_order(
        cls,
        order: Order,
        points_to_redeem: Decimal,
        user_profile: Optional[UserProfile] = None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[Decimal, RewardLedger, OrderRewardRedemption]:
        """
        Applies and redeems points on an order, strictly enforcing the 80% ceiling.
        Stacks with any existing coupon discount on the order:
        order.subtotal -> minus coupon discount_amount -> net amount -> apply at most 80% points.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).select_for_update().get(id=order.id)
        target_profile = user_profile or order.user_profile
        if not target_profile:
            raise ValidationError("Order must have a valid UserProfile to redeem reward points.")

        # Prevent double redemption on the same order
        existing_redemption = OrderRewardRedemption.objects.using(alias).filter(order=order).first()
        if existing_redemption:
            raise ValidationError(
                f"Reward points already redeemed for Order #{order.order_number} "
                f"({existing_redemption.units_redeemed} points)."
            )

        org = getattr(target_profile.user, 'organization', None) or (
            order.branch.organization if getattr(order, 'branch', None) else None
        )
        # Net amount subject to the 80% cap is order.subtotal minus any coupon discount (plus taxes if applicable)
        net_before_points = max(Decimal('0.00'), (order.subtotal or Decimal('0.00')) - (order.discount_amount or Decimal('0.00')))
        if net_before_points <= Decimal('0.00'):
            raise ValidationError("Order payable amount is zero; no points can be redeemed.")

        calc = cls.calculate_max_redeemable_points(
            order_amount=net_before_points,
            user_profile=target_profile,
            organization=org,
            db_alias=alias,
        )

        requested_pts = Decimal(str(points_to_redeem or '0.00'))
        if requested_pts <= Decimal('0.00'):
            raise ValidationError("Points to redeem must be greater than zero.")

        if requested_pts > calc['available_points']:
            raise ValidationError(
                f"Insufficient points. Available: {calc['available_points']}, Requested: {requested_pts}."
            )

        if requested_pts > calc['max_redeemable_points']:
            raise ValidationError(
                f"Maximum {calc['max_redemption_percent']}% of the order amount can be paid with points. "
                f"Max allowed points for this order: {calc['max_redeemable_points']} "
                f"(INR {calc['max_redeemable_discount']}). Requested: {requested_pts}."
            )

        monetary_discount = (requested_pts * calc['point_value_rupees']).quantize(Decimal('0.01'))
        monetary_discount = min(monetary_discount, calc['max_discount_allowed'])

        # Debit points from account and record immutable ledger entry
        ledger, redemption = cls.redeem_rewards(
            user_profile=target_profile,
            reward_type='POINTS',
            quantity=requested_pts,
            monetary_value=monetary_discount,
            order=order,
            reason_code='ORDER_REDEMPTION',
            reason=f"Redeemed {requested_pts} points (₹{monetary_discount}) on Order #{order.order_number}",
            created_by_user=created_by_user,
            db_alias=alias,
        )

        # Update order with reward discount and recalculate total
        order.reward_amount = monetary_discount
        order.total_amount = max(Decimal('0.00'), (order.subtotal - order.discount_amount - monetary_discount + order.tax_amount).quantize(Decimal('0.01')))
        order.save(using=alias, update_fields=['reward_amount', 'total_amount', 'updated_at'])

        return monetary_discount, ledger, redemption

    @classmethod
    def process_order_reward_accrual(
        cls,
        order: Order,
        user_profile: Optional[UserProfile] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Optional[RewardLedger]:
        """
        Calculates and credits reward points for paid orders (package purchase, boutique retail inventory, etc.)
        strictly following the tenant's admin reward configuration (default: 1 point per 100 INR).
        Ensures idempotent execution: if points were already accrued for this order, skips to prevent double-crediting.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        target_profile = user_profile or getattr(order, 'user_profile', None)
        if not target_profile:
            logger.info(
                "Skipping reward points accrual for order %s: No associated UserProfile found.",
                getattr(order, 'order_number', getattr(order, 'id', ''))
            )
            return None

        # Lock/atomic check to ensure idempotency across concurrent webhooks or retries
        with transaction.atomic(using=alias):
            already_accrued = RewardLedger.objects.using(alias).filter(
                order=order,
                transaction_type='EARN',
                reward_type='POINTS',
                reason_code='PURCHASE_EARN',
            ).exists()

            if already_accrued:
                logger.info(
                    "Reward points already accrued for order %s. Skipping duplicate accrual.",
                    getattr(order, 'order_number', getattr(order, 'id', ''))
                )
                return None

            org = getattr(target_profile.user, 'organization', None) or (
                order.branch.organization if getattr(order, 'branch', None) else None
            )
            config = cls.get_reward_config(organization=org, db_alias=alias)

            if not config['is_enabled']:
                logger.info(
                    "Reward points are disabled for organization %s. Skipping accrual for order %s.",
                    org, getattr(order, 'id', '')
                )
                return None

            earn_rate = config['earn_rate_rupees']
            points_per_rate = config['points_per_rate']
            if earn_rate <= 0:
                earn_rate = 100

            total_amount = Decimal(str(order.total_amount or '0.00'))
            if total_amount <= Decimal('0.00'):
                return None

            points_to_earn = int(total_amount // Decimal(str(earn_rate))) * points_per_rate
            if points_to_earn <= 0:
                logger.debug(
                    "Order %s total amount %s is below earn threshold (%s INR). No points accrued.",
                    order.id, total_amount, earn_rate
                )
                return None

            points_decimal = Decimal(str(points_to_earn))
            order_num = getattr(order, 'order_number', str(order.id)[:8])
            order_type_desc = getattr(order, 'order_type', 'PURCHASE') or 'PURCHASE'

            ledger = cls.earn_rewards(
                user_profile=target_profile,
                reward_type='POINTS',
                quantity=points_decimal,
                reason_code='PURCHASE_EARN',
                reason=f"Earned {points_to_earn} points on {order_type_desc} Order #{order_num} (₹{total_amount:.2f} spent)",
                order=order,
                created_by_user=actor_user,
                db_alias=alias,
            )

            logger.info(
                "Successfully credited %s reward points to member %s for order %s (total ₹%s).",
                points_to_earn, target_profile.id, order_num, total_amount
            )
            return ledger

    # -------------------------------------------------------------------------
    # REFERRAL LIFECYCLE
    # -------------------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def generate_referral_identifier(
        cls,
        owner_user: TenantUser,
        program: ReferralProgram,
        identifier_type: str = 'MEMBER_REFERRAL_CODE',
        source_profile_id: Optional[uuid.UUID] = None,
        custom_code: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> ReferralIdentifier:
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        if custom_code:
            code_val = custom_code.upper().strip()
        else:
            prefix = "REF" if identifier_type == 'MEMBER_REFERRAL_CODE' else "TRN"
            code_val = f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

        existing = ReferralIdentifier.objects.using(alias).filter(identifier_value=code_val).first()
        if existing:
            if custom_code:
                raise ValidationError(f"Referral code '{code_val}' is already taken.")
            code_val = f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

        identifier = ReferralIdentifier.objects.using(alias).create(
            organization=program.organization,
            owner_user=owner_user,
            referral_program=program,
            identifier_type=identifier_type,
            identifier_value=code_val,
            source_profile_id=source_profile_id,
            status='ACTIVE',
        )

        return identifier

    @classmethod
    @transaction.atomic
    def register_referral(
        cls,
        identifier_value: str,
        referred_user: Optional[TenantUser] = None,
        referred_email: Optional[str] = None,
        referred_phone: Optional[str] = None,
        source: str = 'WEB',
        db_alias: Optional[str] = None,
    ) -> Referral:
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        identifier = ReferralIdentifier.objects.using(alias).filter(
            identifier_value=identifier_value.strip().upper(),
            status='ACTIVE'
        ).select_related('referral_program', 'owner_user').first()

        if not identifier:
            raise ValidationError(f"Invalid or inactive referral code '{identifier_value}'.")

        # Prohibit self-referral
        if referred_user and referred_user.id == identifier.owner_user.id:
            raise ValidationError("Self-referrals are prohibited. Users cannot refer themselves.")

        target_email = referred_email or (referred_user.email if referred_user else None)
        if target_email and target_email.lower() == identifier.owner_user.email.lower():
            raise ValidationError("Self-referrals are prohibited. Users cannot refer their own email address.")

        # Check existing referral for this program and email/user
        existing = None
        if referred_user:
            existing = Referral.objects.using(alias).filter(
                referral_program=identifier.referral_program,
                referred_user=referred_user
            ).first()
        if not existing and target_email:
            existing = Referral.objects.using(alias).filter(
                referral_program=identifier.referral_program,
                referred_email__iexact=target_email
            ).first()

        if existing:
            raise ValidationError(f"User or email has already been referred (status: {existing.status}).")

        status_val = 'REGISTERED' if referred_user else 'INVITED'
        registered_at = timezone.now() if referred_user else None

        referral = Referral.objects.using(alias).create(
            referral_program=identifier.referral_program,
            referral_identifier=identifier,
            referrer_user=identifier.owner_user,
            referred_user=referred_user,
            identifier_used=identifier.identifier_value,
            referrer_type=identifier.source_profile_type,
            referred_email=target_email,
            referred_phone=referred_phone,
            source=source,
            status=status_val,
            registered_at=registered_at,
        )

        org = identifier.organization
        record_business_audit(
            organization=org,
            module='referrals',
            action_code='REFERRAL_REGISTERED',
            entity_type='Referral',
            entity_id=referral.id,
            actor_user=referred_user or identifier.owner_user,
            metadata={
                'identifier_used': identifier.identifier_value,
                'referrer_user_id': str(identifier.owner_user.id),
                'referred_user_id': str(referred_user.id) if referred_user else None,
                'status': status_val,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=org,
            event_type='referrals.registered',
            aggregate_type='Referral',
            aggregate_id=referral.id,
            payload={
                'referral_id': str(referral.id),
                'referrer_user_id': str(identifier.owner_user.id),
                'referred_email': target_email,
                'status': status_val,
            },
            db_alias=alias,
        )

        return referral

    @classmethod
    @transaction.atomic
    def qualify_and_reward_referral(
        cls,
        referral: Referral,
        event_type: str = 'FIRST_PURCHASE',
        order: Optional[Order] = None,
        db_alias: Optional[str] = None,
    ) -> Referral:
        """
        Evaluates qualification rules for the referral program, and grants rewards according to benefit rules.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        if referral.status in ['QUALIFIED', 'REWARDED']:
            return referral

        # Check qualification rule
        qual_rule = ReferralQualificationRule.objects.using(alias).filter(
            referral_program=referral.referral_program,
            qualification_event=event_type,
            status='ACTIVE'
        ).order_by('priority').first()

        if qual_rule:
            if qual_rule.minimum_order_amount and order:
                if order.total_amount < qual_rule.minimum_order_amount:
                    return referral

        # Mark as QUALIFIED
        now = timezone.now()
        referral.status = 'QUALIFIED'
        referral.qualified_at = now
        referral.save(using=alias, update_fields=['status', 'qualified_at', 'updated_at'])

        # Evaluate benefit rules
        benefit_rules = ReferralBenefitRule.objects.using(alias).filter(
            referral_program=referral.referral_program,
            status='ACTIVE'
        ).order_by('priority')

        # Grant benefits
        referrer_profile = UserProfile.objects.using(alias).filter(user=referral.referrer_user).first()
        referee_profile = None
        if referral.referred_user:
            referee_profile = UserProfile.objects.using(alias).filter(user=referral.referred_user).first()

        for rule in benefit_rules:
            # Referrer reward
            if rule.beneficiary in ['REFERRER', 'BOTH'] and referrer_profile:
                cls.earn_rewards(
                    user_profile=referrer_profile,
                    reward_type=rule.benefit_type,
                    quantity=Decimal(str(rule.benefit_value)),
                    reason_code='REFERRAL_REWARD_REFERRER',
                    reason=f"Referral reward for referring {referral.referred_email or 'friend'}",
                    referral=referral,
                    order=order,
                    db_alias=alias,
                )

            # Referee reward
            if rule.beneficiary in ['REFEREE', 'BOTH'] and referee_profile:
                cls.earn_rewards(
                    user_profile=referee_profile,
                    reward_type=rule.benefit_type,
                    quantity=Decimal(str(rule.benefit_value)),
                    reason_code='REFERRAL_WELCOME_REFEREE',
                    reason=f"Welcome reward via referral code {referral.identifier_used}",
                    referral=referral,
                    order=order,
                    db_alias=alias,
                )

        referral.status = 'REWARDED'
        referral.rewarded_at = timezone.now()
        referral.save(using=alias, update_fields=['status', 'rewarded_at', 'updated_at'])

        org = referral.referral_program.organization
        record_business_audit(
            organization=org,
            module='referrals',
            action_code='REFERRAL_QUALIFIED_AND_REWARDED',
            entity_type='Referral',
            entity_id=referral.id,
            metadata={
                'referral_id': str(referral.id),
                'status': 'REWARDED',
                'event_type': event_type,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=org,
            event_type='referrals.rewarded',
            aggregate_type='Referral',
            aggregate_id=referral.id,
            payload={
                'referral_id': str(referral.id),
                'referrer_user_id': str(referral.referrer_user.id),
                'status': 'REWARDED',
            },
            db_alias=alias,
        )

        return referral
