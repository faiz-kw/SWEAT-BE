"""
apps/tenant_core/services_payment_policy.py — Backend-authoritative Payment Policy Service

Implements:
1. Hierarchy of Payment Policy resolution:
   Package/Version override -> Branch override -> Organization/Tenant Settings -> System Defaults.
2. Channel-specific Payment Provider validation (CASH + RAZORPAY only; CASH blocked on Member/Mobile).
3. Cash Payment Approval Policy (require approval by default, maker-checker segregation of duties).
4. Razorpay Partial Payment Policy (eligibility, minimum payment, installment limits, activation rules).
5. Authoritative Balance Calculations (Decimal money, failed attempts ignored).
"""

import logging
from decimal import Decimal
from typing import Dict, Any, Optional, Tuple
from django.utils import timezone
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

DEFAULT_PAYMENT_POLICY: Dict[str, Any] = {
    'payment_methods': {
        'staff_cash_enabled': True,
        'staff_razorpay_enabled': True,
        'member_razorpay_enabled': True,
    },
    'razorpay_methods': {
        'upi': True,
        'card': True,
        'emi': True,
        'netbanking': True,
        'wallet': True,
        'paylater': False,  # Pay Later disabled by default for SWEAT
    },
    'cash_policy': {
        'require_approval': True,  # Default for SWEAT: ON
        'no_self_approval': True,
        'provisional_sessions_allowed': 4,
        'hold_after_provisional_limit': True,
        'approval_required_for_success': True,
        'approval_required_for_activation': True,
        'allowed_recorder_roles': ['ADMIN', 'MANAGER', 'SALES_REP', 'CASHIER'],
        'allowed_approver_roles': ['ADMIN', 'MANAGER', 'FINANCE'],
    },
    'partial_payment_policy': {
        'enabled': True,  # Configurable per tenant / branch / package
        'min_first_payment_type': 'PERCENTAGE',  # 'PERCENTAGE' or 'FIXED'
        'min_first_payment_percentage': '30.00',  # 30% default minimum
        'min_first_payment_amount': '1000.00',
        'max_installments': 3,
        'min_installment_amount': '500.00',
        'balance_due_days': 30,
        'activation_rule': 'FULL_PAYMENT_ONLY',  # 'FULL_PAYMENT_ONLY' or 'MINIMUM_PARTIAL_PAYMENT'
        'allow_booking_with_outstanding_balance': False,
        'overdue_grace_days': 7,
    },
}


class PaymentPolicyService:
    """
    Authoritative backend payment policy resolver and validator.
    """

    @classmethod
    def get_effective_policy(
        cls,
        organization,
        branch=None,
        package=None,
        package_version=None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolves effective payment policy following hierarchy:
        Package/Version -> Branch -> Organization -> System Defaults.
        """
        alias = db_alias or 'default'
        policy = {
            'payment_methods': dict(DEFAULT_PAYMENT_POLICY['payment_methods']),
            'razorpay_methods': dict(DEFAULT_PAYMENT_POLICY['razorpay_methods']),
            'cash_policy': dict(DEFAULT_PAYMENT_POLICY['cash_policy']),
            'partial_payment_policy': dict(DEFAULT_PAYMENT_POLICY['partial_payment_policy']),
        }

        # 1. Organization level overrides
        try:
            from .models_govern import OrganizationSettings
            org_settings = OrganizationSettings.objects.using(alias).filter(organization=organization).first()
            if org_settings and isinstance(org_settings.membership_config, dict):
                org_policy = org_settings.membership_config.get('payment_policy')
                if isinstance(org_policy, dict):
                    cls._deep_merge(policy, org_policy)
        except Exception as e:
            logger.debug("Failed reading OrganizationSettings payment_policy: %s", e)

        # 2. Branch level overrides
        if branch:
            try:
                from .models_govern import BranchSettings
                branch_settings = BranchSettings.objects.using(alias).filter(branch=branch).first()
                if branch_settings and isinstance(branch_settings.membership_config, dict):
                    branch_policy = branch_settings.membership_config.get('payment_policy')
                    if isinstance(branch_policy, dict):
                        cls._deep_merge(policy, branch_policy)
            except Exception as e:
                logger.debug("Failed reading BranchSettings payment_policy: %s", e)

        # 3. Package / Version overrides
        target_pkg = package_version or package
        if target_pkg and hasattr(target_pkg, 'metadata') and isinstance(target_pkg.metadata, dict):
            pkg_policy = target_pkg.metadata.get('payment_policy')
            if isinstance(pkg_policy, dict):
                cls._deep_merge(policy, pkg_policy)

        return policy

    @classmethod
    def _deep_merge(cls, base: dict, override: dict) -> None:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                cls._deep_merge(base[k], v)
            else:
                base[k] = v

    @classmethod
    def update_organization_policy(
        cls,
        organization,
        policy_data: Dict[str, Any],
        actor=None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Updates organization-level payment policy.
        """
        alias = db_alias or 'default'
        from .models_govern import OrganizationSettings
        from .services_reliability import record_business_audit

        org_settings, _ = OrganizationSettings.objects.using(alias).get_or_create(
            organization=organization
        )
        if not isinstance(org_settings.membership_config, dict):
            org_settings.membership_config = {}

        if isinstance(policy_data, dict) and 'payment_policy' in policy_data and isinstance(policy_data['payment_policy'], dict):
            policy_data = policy_data['payment_policy']
        current_policy = org_settings.membership_config.get('payment_policy') or {}
        cls._deep_merge(current_policy, policy_data)
        org_settings.membership_config['payment_policy'] = current_policy
        org_settings.save(using=alias, update_fields=['membership_config', 'updated_at'])

        record_business_audit(
            organization=organization,
            module='commerce',
            action_code='PAYMENT_POLICY_UPDATED',
            entity_type='OrganizationSettings',
            entity_id=org_settings.id,
            actor_user=actor,
            metadata={'updated_policy': current_policy},
            db_alias=alias,
        )

        return cls.get_effective_policy(organization, db_alias=alias)

    @classmethod
    def get_razorpay_checkout_config(
        cls,
        organization,
        branch=None,
        db_alias: Optional[str] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Resolves Razorpay checkout configuration for the tenant:
        - Discovers any tenant-configured Razorpay dashboard configuration ID (config_id).
        - Builds the official Razorpay Checkout config object to filter payment methods.
          Specifically excludes any method where razorpay_methods[m] is False (e.g. 'paylater').
        """
        alias = db_alias or 'default'
        policy = cls.get_effective_policy(organization, branch=branch, db_alias=alias)
        rzp_methods = policy.get('razorpay_methods') or DEFAULT_PAYMENT_POLICY['razorpay_methods']

        config_id = None
        try:
            from .models_infra import Integration
            integration = Integration.objects.using(alias).filter(
                integration_type='PAYMENT',
                provider__iexact='Razorpay',
                status='ACTIVE',
            ).first()
            if integration and isinstance(integration.configuration, dict):
                config_id = integration.configuration.get('config_id') or integration.configuration.get('razorpay_config_id')
        except Exception:
            pass

        hide_list = []
        method_keys = ['upi', 'card', 'emi', 'netbanking', 'wallet', 'paylater']
        for key in method_keys:
            if not rzp_methods.get(key, True):
                hide_list.append({'method': key})

        # Pay Later is specifically excluded for SWEAT unless explicitly enabled
        if not rzp_methods.get('paylater', False):
            if not any(h.get('method') == 'paylater' for h in hide_list):
                hide_list.append({'method': 'paylater'})

        checkout_config = {
            'display': {
                'hide': hide_list,
            },
        }
        return config_id, checkout_config

    @classmethod
    def validate_payment_provider(
        cls,
        provider: str,
        channel: str = 'STAFF',
        organization=None,
        branch=None,
        db_alias: Optional[str] = None,
    ) -> None:
        """
        Validates whether the requested payment provider is permitted for the channel and policy.
        SWEAT strictly supports ONLY CASH and RAZORPAY for new payments.
        CASH is forbidden for MEMBER / MOBILE channels.
        """
        prov = str(provider).strip().upper()
        chan = str(channel).strip().upper()

        # Reject unsupported providers for new payments
        if prov not in ['CASH', 'RAZORPAY']:
            raise ValidationError(
                f"Payment provider '{provider}' is not supported for new payments. SWEAT supports only CASH and RAZORPAY.",
                code="UNSUPPORTED_PAYMENT_PROVIDER"
            )

        # Reject CASH on member self-service channels
        is_member_chan = any(t in chan for t in ['MEMBER', 'MOBILE', 'CLIENT', 'APP', 'PORTAL', 'WEB']) and 'STAFF' not in chan and 'CRM' not in chan
        if is_member_chan and prov == 'CASH':
            raise ValidationError(
                "Cash payment is not permitted for member self-service. Cash must be collected at the centre by staff.",
                code="PAYMENT_METHOD_NOT_ALLOWED_FOR_CHANNEL"
            )

        if organization:
            policy = cls.get_effective_policy(organization, branch=branch, db_alias=db_alias)
            methods = policy.get('payment_methods', {})
            if prov == 'CASH' and not methods.get('staff_cash_enabled', True):
                raise ValidationError("Cash payment is currently disabled by organization policy.", code="PAYMENT_METHOD_DISABLED")
            if prov == 'RAZORPAY':
                if chan in ['MEMBER', 'MOBILE_APP', 'WEB', 'CLIENT'] and not methods.get('member_razorpay_enabled', True):
                    raise ValidationError("Online payment is currently disabled for member self-service.", code="PAYMENT_METHOD_DISABLED")
                if chan == 'STAFF' and not methods.get('staff_razorpay_enabled', True):
                    raise ValidationError("Online payment is currently disabled for staff transactions.", code="PAYMENT_METHOD_DISABLED")

    @classmethod
    def calculate_order_balance(cls, order, db_alias: Optional[str] = None) -> Tuple[Decimal, Decimal, Decimal, int]:
        """
        Authoritative calculation of:
        (total_amount, total_successfully_paid, outstanding_balance, successful_transactions_count)
        Failed and cancelled attempts do NOT reduce outstanding balance.
        """
        alias = db_alias or 'default'
        from .models_commerce import PaymentTransaction

        total_amount = Decimal(str(order.total_amount))
        successful_txns = PaymentTransaction.objects.using(alias).filter(
            order=order,
            status='SUCCESS',
        )
        total_paid = sum(Decimal(str(t.amount)) for t in successful_txns)
        outstanding = max(Decimal('0.00'), total_amount - total_paid)
        return total_amount, total_paid, outstanding, successful_txns.count()

    @classmethod
    def validate_partial_payment(
        cls,
        order,
        requested_amount: Decimal,
        policy: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> Decimal:
        """
        Validates partial payment eligibility and amount constraints.
        Returns the sanitized Decimal amount to be charged.
        """
        alias = db_alias or 'default'
        if policy is None:
            policy = cls.get_effective_policy(
                organization=order.branch.organization,
                branch=order.branch,
                db_alias=alias,
            )

        partial_cfg = policy.get('partial_payment_policy') or policy.get('partial_payment') or {}
        if not partial_cfg.get('enabled', False):
            raise ValidationError(
                "Partial payments are not permitted for this purchase by organization policy.",
                code="PARTIAL_PAYMENT_DISABLED"
            )

        total_amount, total_paid, outstanding, success_count = cls.calculate_order_balance(order, db_alias=alias)

        if outstanding <= Decimal('0.00'):
            raise ValidationError("This order has already been fully paid.", code="ORDER_ALREADY_PAID")

        req_amt = Decimal(str(requested_amount)).quantize(Decimal('0.01'))
        if req_amt <= Decimal('0.00'):
            raise ValidationError("Payment amount must be greater than zero.", code="INVALID_AMOUNT")

        if req_amt > outstanding:
            raise ValidationError(
                f"Requested payment amount (₹{req_amt}) exceeds outstanding balance (₹{outstanding}).",
                code="AMOUNT_EXCEEDS_OUTSTANDING"
            )

        # Check max installments limit
        max_installments = int(partial_cfg.get('max_installments', 3))
        if success_count >= max_installments - 1:
            # Must pay full remaining balance in final installment
            if req_amt < outstanding:
                raise ValidationError(
                    f"This is the final allowed installment ({success_count + 1}/{max_installments}). You must pay the full remaining balance of ₹{outstanding}.",
                    code="FINAL_INSTALLMENT_MUST_BE_FULL"
                )

        # First payment minimum threshold
        if success_count == 0:
            min_type = partial_cfg.get('min_first_payment_type', 'PERCENTAGE')
            if min_type == 'PERCENTAGE':
                min_pct = Decimal(str(partial_cfg.get('min_first_payment_percentage', '30.00')))
                min_required = (total_amount * min_pct / Decimal('100.00')).quantize(Decimal('0.01'))
            else:
                min_required = Decimal(str(partial_cfg.get('min_first_payment_amount', '1000.00')))

            min_required = min(min_required, total_amount)
            if req_amt < min_required:
                raise ValidationError(
                    f"First partial payment must be at least ₹{min_required} ({partial_cfg.get('min_first_payment_percentage', '30')}% of total ₹{total_amount}).",
                    code="AMOUNT_BELOW_MINIMUM"
                )
        else:
            # Subsequent installment minimum
            min_inst = Decimal(str(partial_cfg.get('min_installment_amount', '500.00')))
            min_required = min(min_inst, outstanding)
            if req_amt < min_required:
                raise ValidationError(
                    f"Installment payment must be at least ₹{min_required}.",
                    code="AMOUNT_BELOW_MINIMUM"
                )

        return req_amt

    @classmethod
    def should_activate_membership(
        cls,
        order,
        total_paid: Decimal,
        policy: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> bool:
        """
        Determines whether a membership should be activated based on total amount paid and policy.
        """
        alias = db_alias or 'default'
        total_amount = Decimal(str(order.total_amount))

        # Full payment always activates
        if total_paid >= total_amount:
            return True

        if policy is None:
            policy = cls.get_effective_policy(
                organization=order.branch.organization,
                branch=order.branch,
                db_alias=alias,
            )

        partial_cfg = policy.get('partial_payment_policy') or policy.get('partial_payment') or {}
        if not partial_cfg.get('enabled', False):
            return False

        rule = partial_cfg.get('activation_rule', 'FULL_PAYMENT_ONLY')
        if rule == 'MINIMUM_PARTIAL_PAYMENT':
            # Check if total_paid meets minimum first payment
            min_type = partial_cfg.get('min_first_payment_type', 'PERCENTAGE')
            if min_type == 'PERCENTAGE':
                min_pct = Decimal(str(partial_cfg.get('min_first_payment_percentage', '30.00')))
                min_required = (total_amount * min_pct / Decimal('100.00')).quantize(Decimal('0.01'))
            else:
                min_required = Decimal(str(partial_cfg.get('min_first_payment_amount', '1000.00')))

            return total_paid >= min_required

        return False
