"""
Security Policy Management — Phase 1 Layer 1.

Persists, validates, and enforces tenant security policies:
- Mandatory MFA requirement (enforce_mfa)
- Inactivity session timeout (session_timeout_minutes)
- Password length and complexity (password_min_length, require_special_character)
- Login lockout threshold (max_failed_attempts_lockout)
- IP whitelist

Authoritative persistence in tenant dedicated DB via OrganizationSettings.
Audited via immutable tenant audit events.
"""

import json
import logging
from typing import Dict, Any, Optional
from rest_framework import serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.conf import settings

from .models_org import Organization
from .models_govern import OrganizationSettings
from .audit import emit_audit_event, snapshot_model_state

logger = logging.getLogger(__name__)

SECURITY_POLICY_KEY = "security_policy"
REDIS_SEC_PREFIX = "tenant:security_policy:"

DEFAULT_SECURITY_POLICY: Dict[str, Any] = {
    "enforce_mfa": False,
    "session_timeout_minutes": 60,
    "password_min_length": 10,
    "require_special_character": True,
    "max_failed_attempts_lockout": 5,
    "ip_whitelist": [],
}


def _get_redis():
    try:
        from django_redis import get_redis_connection
        return get_redis_connection("default")
    except Exception:
        try:
            import redis
            return redis.from_url(getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0'))
        except Exception:
            return None


class SecurityPolicySerializer(serializers.Serializer):
    enforce_mfa = serializers.BooleanField(required=False, default=False)
    mfa_required = serializers.BooleanField(required=False, default=False)
    session_timeout_minutes = serializers.IntegerField(min_value=5, max_value=1440, default=60)
    password_min_length = serializers.IntegerField(min_value=8, max_value=128, default=10)
    require_special_character = serializers.BooleanField(default=True)
    max_failed_attempts_lockout = serializers.IntegerField(min_value=1, max_value=20, default=5)
    ip_whitelist = serializers.ListField(
        child=serializers.CharField(max_length=45),
        required=False,
        default=list
    )

    def validate(self, attrs):
        if 'mfa_required' in attrs and 'enforce_mfa' not in attrs:
            attrs['enforce_mfa'] = attrs['mfa_required']
        elif 'enforce_mfa' in attrs and 'mfa_required' not in attrs:
            attrs['mfa_required'] = attrs['enforce_mfa']
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        mfa_val = data.get('enforce_mfa', False) or data.get('mfa_required', False)
        data['enforce_mfa'] = mfa_val
        data['mfa_required'] = mfa_val
        return data


def get_tenant_security_policy(tenant_id: Optional[str] = None, db_alias: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns active security policy dictionary for a tenant.
    Checks Redis cache first, then OrganizationSettings in dedicated DB, falling back to defaults.
    """
    redis_client = _get_redis()
    if tenant_id and redis_client:
        try:
            cached = redis_client.get(f"{REDIS_SEC_PREFIX}{tenant_id}")
            if cached:
                return json.loads(cached.decode('utf-8') if isinstance(cached, bytes) else cached)
        except Exception as e:
            logger.debug("Redis cache miss for security policy tenant_id=%s: %s", tenant_id, e)

    if db_alias:
        try:
            from config.routers import set_tenant_db_alias
            set_tenant_db_alias(db_alias)
            org = Organization.objects.using(db_alias).first()
            if org:
                settings_obj = OrganizationSettings.objects.using(db_alias).filter(organization=org).first()
                if settings_obj and isinstance(settings_obj.notification_config, dict):
                    policy = settings_obj.notification_config.get(SECURITY_POLICY_KEY)
                    if policy and isinstance(policy, dict):
                        merged = {**DEFAULT_SECURITY_POLICY, **policy}
                        if tenant_id and redis_client:
                            try:
                                redis_client.set(f"{REDIS_SEC_PREFIX}{tenant_id}", json.dumps(merged), ex=3600)
                            except Exception:
                                pass
                        return merged
        except Exception as e:
            logger.error("Error reading OrganizationSettings for security policy db_alias=%s: %s", db_alias, e)

    return dict(DEFAULT_SECURITY_POLICY)


class SecurityPolicyView(APIView):
    """
    GET / PUT /api/v1/admin-config/security-policy/current/
    Also mounted at /api/v1/tenant/security-policy/current/
    
    Reads and updates the tenant organization's security policies.
    Requires authentication and checks RBAC permissions.
    Audits all configuration changes.
    """
    permission_classes = [IsAuthenticated]

    def _resolve_db_and_settings(self, request):
        db_alias = getattr(request, '_tenant_db_alias', None)
        if not db_alias and hasattr(request.user, '_db_alias'):
            db_alias = request.user._db_alias
        if not db_alias:
            from config.routers import get_tenant_db_alias
            db_alias = get_tenant_db_alias()

        if not db_alias or db_alias == 'default':
            return None, None, None

        org = Organization.objects.using(db_alias).first()
        if not org:
            return db_alias, None, None

        settings_obj, _ = OrganizationSettings.objects.using(db_alias).get_or_create(
            organization=org,
            defaults={'currency': org.currency or 'INR'}
        )
        return db_alias, org, settings_obj

    def get(self, request):
        db_alias, org, settings_obj = self._resolve_db_and_settings(request)
        if not settings_obj:
            # For platform admin or default context, return standard defaults
            return Response(SecurityPolicySerializer(DEFAULT_SECURITY_POLICY).data)

        cfg = settings_obj.notification_config or {}
        policy_data = cfg.get(SECURITY_POLICY_KEY, DEFAULT_SECURITY_POLICY)
        merged = {**DEFAULT_SECURITY_POLICY, **policy_data}
        return Response(SecurityPolicySerializer(merged).data)

    def put(self, request):
        db_alias, org, settings_obj = self._resolve_db_and_settings(request)
        if not settings_obj:
            return Response(
                {"error": "No active tenant organization context found to persist security policy."},
                status=status.HTTP_404_NOT_FOUND
            )

        # RBAC Check: Ensure user is authorized to edit organization settings
        user_roles = getattr(request.user, '_roles', []) or []
        is_admin = getattr(request.user, 'is_staff', False) or any(
            r in ('ORG_ADMIN', 'PLATFORM_SUPER_ADMIN', 'TENANT_ADMIN') for r in user_roles
        )
        # Check permissions set
        user_perms = getattr(request.user, '_permission_codes', set()) or set()
        if not is_admin and 'core.settings.edit' not in user_perms and 'admin.security.manage' not in user_perms:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You do not have permission to modify organization security policies.")

        serializer = SecurityPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        with transaction.atomic(using=db_alias):
            before_cfg = settings_obj.notification_config or {}
            before_policy = before_cfg.get(SECURITY_POLICY_KEY, DEFAULT_SECURITY_POLICY)

            # Update jsonb block
            new_cfg = dict(before_cfg)
            new_cfg[SECURITY_POLICY_KEY] = validated
            settings_obj.notification_config = new_cfg
            settings_obj.save(using=db_alias, update_fields=['notification_config', 'updated_at'])

            # Log audit event
            tenant_id = getattr(request.user, '_tenant_id', None) or str(org.id)
            emit_audit_event(
                action='UPDATE',
                resource_type='OrganizationSettings',
                resource_id=str(settings_obj.id),
                request=request,
                instance=settings_obj,
                before_state={SECURITY_POLICY_KEY: before_policy},
                after_state={SECURITY_POLICY_KEY: validated},
                description='Updated organization security policies',
                db_alias=db_alias,
            )

            # Invalidate Redis cache
            redis_client = _get_redis()
            if redis_client and tenant_id:
                try:
                    redis_client.set(f"{REDIS_SEC_PREFIX}{tenant_id}", json.dumps(validated), ex=3600)
                except Exception as e:
                    logger.warning("Failed to refresh security policy Redis cache: %s", e)

        return Response(SecurityPolicySerializer(validated).data, status=status.HTTP_200_OK)
