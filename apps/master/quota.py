"""
Master DB — Quota & Entitlement Enforcement Engine.
Provides centralized, fail-closed resolution of SaaS plan and tenant-override resource limits,
and evaluates live resource usage in dedicated tenant databases.
"""

import logging
from typing import Tuple, Optional
from django.utils import timezone
from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    ResourceMetric, SaasPlanResourceLimit, TenantResourceLimit, TenantSubscription
)

logger = logging.getLogger(__name__)


class QuotaError(Exception):
    """Base exception for quota resolution and evaluation errors."""
    pass


class QuotaConfigurationError(QuotaError):
    """Raised when quota limits are missing, invalid, or cannot be resolved."""
    pass


class QuotaExceededError(QuotaError):
    """Raised when a requested resource creation would exceed the authoritative quota."""
    def __init__(self, message: str, metric_code: str, current_usage: int, limit_value: int):
        super().__init__(message)
        self.metric_code = metric_code
        self.current_usage = current_usage
        self.limit_value = limit_value


class QuotaChecker:
    """
    Centralized service for authoritative quota enforcement.
    Limits are stored and resolved in Master DB ('default').
    Live usage is counted directly from the designated Tenant DB.
    """

    @classmethod
    def get_effective_limit(cls, tenant_id, metric_code: str) -> int:
        """
        Resolves the effective resource limit for a tenant and metric code.
        Order of precedence:
        1. TenantResourceLimit override on Master DB ('default')
        2. SaasPlanResourceLimit for current active/trialing subscription plan ('default')
        3. Unlimited only when limit_value == -1
        4. Fails closed if no quota configuration is found (raises QuotaConfigurationError)
        """
        # Ensure metric exists in Master DB
        metric = ResourceMetric.objects.using('default').filter(code=metric_code, is_active=True).first()
        if not metric:
            logger.error("QuotaChecker: Metric code '%s' not found or inactive in Master DB.", metric_code)
            raise QuotaConfigurationError(f"Resource metric '{metric_code}' is not configured.")

        # 1. Check for Tenant-specific limit override
        override = TenantResourceLimit.objects.using('default').filter(
            tenant_id=tenant_id,
            metric=metric,
        ).first()

        if override:
            logger.debug(
                "QuotaChecker: Resolved tenant override for tenant=%s, metric=%s -> %d",
                tenant_id, metric_code, override.limit_value
            )
            return override.limit_value

        # 2. Check Plan-level default limit from tenant's current subscription
        subscription = TenantSubscription.objects.using('default').filter(
            tenant_id=tenant_id,
        ).order_by('-created_at').select_related('plan').first()

        if not subscription or not subscription.plan:
            logger.error("QuotaChecker: No subscription or plan found for tenant=%s.", tenant_id)
            raise QuotaConfigurationError(f"No active subscription or plan found for tenant {tenant_id}.")

        plan_limit = SaasPlanResourceLimit.objects.using('default').filter(
            plan=subscription.plan,
            metric=metric,
        ).first()

        if not plan_limit:
            # Fail closed: do not assume unlimited if not configured
            logger.error(
                "QuotaChecker: No limit configured for plan='%s' and metric='%s'. Failing closed.",
                subscription.plan.code, metric_code
            )
            raise QuotaConfigurationError(
                f"No quota limit defined for plan '{subscription.plan.code}' on metric '{metric_code}'."
            )

        logger.debug(
            "QuotaChecker: Resolved plan limit for tenant=%s, plan=%s, metric=%s -> %d",
            tenant_id, subscription.plan.code, metric_code, plan_limit.limit_value
        )
        return plan_limit.limit_value

    @classmethod
    def get_live_usage(cls, metric_code: str, db_alias: str, org_id: Optional[str] = None) -> int:
        """
        Determines authoritative real-time resource usage from the tenant DB.
        """
        if metric_code == 'ACTIVE_USERS':
            from apps.tenant_core.models_users import TenantUser
            qs = TenantUser.objects.using(db_alias).filter(
                status__in=['ACTIVE', 'INVITED']
            )
            if org_id:
                qs = qs.filter(organization_id=org_id)
            return qs.count()

        elif metric_code == 'LOCATIONS':
            from apps.tenant_core.models_org import Branch
            qs = Branch.objects.using(db_alias).filter(status='ACTIVE')
            if org_id:
                qs = qs.filter(organization_id=org_id)
            return qs.count()

        else:
            raise ValueError(f"Unsupported live usage metric: {metric_code}")

    @classmethod
    def check_quota(
        cls,
        tenant_id,
        metric_code: str,
        db_alias: str,
        requested_increment: int = 1,
        org_id: Optional[str] = None
    ) -> Tuple[bool, int, int]:
        """
        Evaluates whether a requested increment is allowed under the authoritative quota.
        Returns:
            (is_allowed: bool, current_usage: int, limit_value: int)
        """
        limit = cls.get_effective_limit(tenant_id=tenant_id, metric_code=metric_code)

        if limit == -1:
            # Unlimited
            current_usage = cls.get_live_usage(metric_code=metric_code, db_alias=db_alias, org_id=org_id)
            return True, current_usage, -1

        current_usage = cls.get_live_usage(metric_code=metric_code, db_alias=db_alias, org_id=org_id)
        is_allowed = (current_usage + requested_increment) <= limit
        return is_allowed, current_usage, limit

    @classmethod
    def assert_quota_available(
        cls,
        tenant_id,
        metric_code: str,
        db_alias: str,
        requested_increment: int = 1,
        org_id: Optional[str] = None
    ):
        """
        Asserts that the requested resource increment is allowed.
        Raises QuotaExceededError if quota limit would be exceeded.
        """
        allowed, current_usage, limit = cls.check_quota(
            tenant_id=tenant_id,
            metric_code=metric_code,
            db_alias=db_alias,
            requested_increment=requested_increment,
            org_id=org_id,
        )

        if not allowed:
            raise QuotaExceededError(
                f"Resource quota exceeded for '{metric_code}'. "
                f"Current usage: {current_usage}, requested: {requested_increment}, limit: {limit}.",
                metric_code=metric_code,
                current_usage=current_usage,
                limit_value=limit,
            )
