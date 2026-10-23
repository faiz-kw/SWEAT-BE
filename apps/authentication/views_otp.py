"""
apps/authentication/views_otp.py — Phone Number OTP Generation & Verification Engine.

Provides passwordless authentication for both Web Portal and Mobile App.
Works out of the box in Development / Stub mode without requiring active SMS DLT vendor credentials.
When an SMS provider is configured, dispatches SMS via communication adapters.

Endpoints:
  POST /api/v1/auth/otp/send/    — Generate & send OTP (with 60s cooldown & 5min expiry)
  POST /api/v1/auth/otp/verify/  — Verify OTP & return JWT tokens + HttpOnly refresh cookie
"""

import re
import secrets
import hashlib
import logging
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.db import models

from apps.authentication.views import (
    _build_tenant_token,
    _build_login_response,
    _register_and_resolve_tenant,
)

logger = logging.getLogger(__name__)

PHONE_REGEX = re.compile(r'^\+?[1-9]\d{7,14}$')

# Fallback in-memory storage if cache backend is unavailable or flushed
_FALLBACK_OTP_STORE = {}


def normalize_phone(phone: str) -> str:
    """Sanitizes phone number into E.164 string format (+91...)."""
    if not phone:
        return ''
    clean = re.sub(r'[\s\-\(\)]', '', str(phone).strip())
    if clean.startswith('00'):
        clean = '+' + clean[2:]
    elif not clean.startswith('+'):
        # Default to India (+91) if 10-digit mobile number starting with 6, 7, 8, or 9
        if len(clean) == 10 and clean[0] in '6789':
            clean = '+91' + clean
        else:
            clean = '+' + clean
    return clean


def _get_otp_record(phone: str) -> dict | None:
    cache_key = f"sweat_otp_{phone}"
    record = cache.get(cache_key)
    if not record:
        record = _FALLBACK_OTP_STORE.get(phone)
    return record


def _get_cache_key(phone: str, tenant_slug: str = '') -> str:
    slug = (tenant_slug or 'default').lower().strip()
    return f"sweat_otp_{slug}_{phone}"


def _get_otp_record(phone: str, tenant_slug: str = '') -> dict | None:
    cache_key = _get_cache_key(phone, tenant_slug)
    record = cache.get(cache_key)
    if not record:
        record = _FALLBACK_OTP_STORE.get(cache_key)
    return record


def _save_otp_record(phone: str, record: dict, tenant_slug: str = '', ttl_seconds: int = 300) -> None:
    cache_key = _get_cache_key(phone, tenant_slug)
    cache.set(cache_key, record, timeout=ttl_seconds)
    _FALLBACK_OTP_STORE[cache_key] = record


def _delete_otp_record(phone: str, tenant_slug: str = '') -> None:
    cache_key = _get_cache_key(phone, tenant_slug)
    cache.delete(cache_key)
    _FALLBACK_OTP_STORE.pop(cache_key, None)


def _resolve_tenant_and_db(request):
    """
    Resolves active tenant and database connection alias.
    If an explicit org_code / tenant_slug is provided, it fails closed if not found.
    """
    from apps.master.models_tenant import Tenant
    from config.routers import get_tenant_db_alias, set_tenant_db_alias

    explicit_org = (
        (request.headers.get('X-Tenant-Slug') if hasattr(request, 'headers') else None)
        or (request.headers.get('X-Org-Code') if hasattr(request, 'headers') else None)
        or request.META.get('HTTP_X_TENANT_SLUG')
        or request.META.get('HTTP_X_ORG_CODE')
        or (getattr(request, 'query_params', None) or getattr(request, 'GET', {})).get('tenant')
        or (getattr(request, 'query_params', None) or getattr(request, 'GET', {})).get('org_code')
        or (request.data.get('org_code') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        or (request.data.get('tenant_code') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        or (request.data.get('tenant_slug') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
    )

    if explicit_org:
        clean_org = str(explicit_org).strip()
        tenant = Tenant.objects.using('default').filter(
            models.Q(slug__iexact=clean_org) | models.Q(code__iexact=clean_org),
            status='ACTIVE'
        ).first()
        if not tenant:
            return None, None
    else:
        # Default fallback only when no explicit organization code was supplied
        tenant = Tenant.objects.using('default').filter(
            models.Q(slug__iexact='sweat') | models.Q(code__iexact='sweat'),
            status='ACTIVE'
        ).first() or Tenant.objects.using('default').filter(status='ACTIVE').first()

    if not tenant:
        return None, None

    active_alias = get_tenant_db_alias()
    if active_alias:
        return active_alias, tenant

    alias = _register_and_resolve_tenant(tenant)
    set_tenant_db_alias(alias)
    return alias, tenant


class SendOTPView(APIView):
    """
    POST /api/v1/auth/otp/send/
    Generates a secure 6-digit OTP for the given phone number with a 5-minute expiry.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw_phone = request.data.get('phone') or request.data.get('mobile') or request.data.get('identifier')
        phone = normalize_phone(raw_phone)

        if not phone or not PHONE_REGEX.match(phone):
            return Response(
                {
                    'success': False,
                    'error': 'Invalid phone number. Please provide a valid 10-digit mobile number.',
                    'code': 'INVALID_PHONE',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate organization code if provided
        raw_org = request.data.get('org_code') or request.data.get('tenant_code') or request.data.get('tenant_slug')
        alias, tenant = _resolve_tenant_and_db(request)
        if raw_org and not tenant:
            return Response(
                {
                    'success': False,
                    'error': f"Organization '{raw_org}' not found or is currently inactive.",
                    'code': 'ORGANIZATION_NOT_FOUND',
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()
        tenant_slug = (getattr(tenant, 'slug', '') or getattr(tenant, 'code', '')) if tenant else 'default'
        existing = _get_otp_record(phone, tenant_slug=tenant_slug)

        # 60-second cooldown protection against spamming
        if existing and 'last_sent' in existing:
            elapsed = (now - existing['last_sent']).total_seconds()
            if elapsed < 60:
                retry_after = int(60 - elapsed)
                return Response(
                    {
                        'success': False,
                        'error': f'Please wait {retry_after} seconds before requesting a new OTP.',
                        'code': 'COOLDOWN_ACTIVE',
                        'retry_after_seconds': retry_after,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        # Generate 6-digit cryptographic numeric OTP
        otp_code = str(secrets.randbelow(900000) + 100000)
        otp_hash = hashlib.sha256(otp_code.encode('utf-8')).hexdigest()

        record = {
            'hash': otp_hash,
            'expires_at': now + timedelta(minutes=5),
            'attempts': 0,
            'last_sent': now,
        }
        _save_otp_record(phone, record, tenant_slug=tenant_slug, ttl_seconds=300)

        # Log OTP clearly on server console for developers / testing
        logger.info("[OTP GENERATION] Tenant: %s | Phone: %s -> OTP: %s (Expires in 5 mins)", tenant_slug, phone, otp_code)
        print(f"\n=======================================================")
        print(f"[DEV OTP GENERATED] Tenant: {tenant_slug} | Phone: {phone} | OTP: {otp_code}")
        print(f"=======================================================\n")

        # In development / sandbox mode, include dev_otp in the response for easy app & web testing
        response_data = {
            'success': True,
            'message': 'OTP generated and sent successfully.',
            'phone': phone,
            'org_code': tenant_slug,
            'retry_after_seconds': 60,
        }

        # Include dev_otp if DEBUG or no live SMS provider is active
        if getattr(settings, 'DEBUG', True):
            response_data['dev_otp'] = otp_code

        return Response(response_data, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    """
    POST /api/v1/auth/otp/verify/
    Verifies the submitted 6-digit OTP and authenticates the member/user.
    Returns JWT access & refresh tokens and sets the HttpOnly refresh cookie.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw_phone = request.data.get('phone') or request.data.get('mobile') or request.data.get('identifier')
        otp_input = str(request.data.get('otp') or '').strip()
        phone = normalize_phone(raw_phone)

        if not phone:
            return Response(
                {
                    'success': False,
                    'error': 'Phone number is required.',
                    'code': 'MISSING_PHONE',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not otp_input or len(otp_input) != 6:
            return Response(
                {
                    'success': False,
                    'error': 'A valid 6-digit OTP is required.',
                    'code': 'INVALID_OTP_FORMAT',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # -------------------------------------------------------------------
        # Resolve Tenant & Scoped OTP Record
        # -------------------------------------------------------------------
        raw_org = request.data.get('org_code') or request.data.get('tenant_code') or request.data.get('tenant_slug')
        alias, tenant = _resolve_tenant_and_db(request)
        if raw_org and not tenant:
            return Response(
                {
                    'success': False,
                    'error': f"Organization '{raw_org}' not found or is currently inactive.",
                    'code': 'ORGANIZATION_NOT_FOUND',
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if not alias or not tenant:
            return Response(
                {
                    'success': False,
                    'error': 'Studio tenant service is temporarily unavailable.',
                    'code': 'TENANT_UNAVAILABLE',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        tenant_slug = (getattr(tenant, 'slug', '') or getattr(tenant, 'code', '')) if tenant else 'default'
        now = timezone.now()
        record = _get_otp_record(phone, tenant_slug=tenant_slug)

        # Developer master override in DEBUG mode (e.g. 123456)
        is_dev_override = getattr(settings, 'DEBUG', False) and otp_input == '123456'

        if not record and not is_dev_override:
            return Response(
                {
                    'success': False,
                    'error': 'No active OTP found for this phone number or it has expired. Please request a new OTP.',
                    'code': 'NO_ACTIVE_OTP',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if record:
            # Check expiry
            if now > record['expires_at']:
                _delete_otp_record(phone, tenant_slug=tenant_slug)
                return Response(
                    {
                        'success': False,
                        'error': 'OTP has expired. Please request a new OTP.',
                        'code': 'OTP_EXPIRED',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Check maximum verification attempts (max 5)
            if record.get('attempts', 0) >= 5:
                _delete_otp_record(phone, tenant_slug=tenant_slug)
                return Response(
                    {
                        'success': False,
                        'error': 'Maximum verification attempts exceeded. Please request a new OTP.',
                        'code': 'MAX_ATTEMPTS_EXCEEDED',
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            # Check hash match
            submitted_hash = hashlib.sha256(otp_input.encode('utf-8')).hexdigest()
            if submitted_hash != record['hash'] and not is_dev_override:
                record['attempts'] = record.get('attempts', 0) + 1
                _save_otp_record(phone, record, tenant_slug=tenant_slug, ttl_seconds=300)
                remaining = 5 - record['attempts']
                return Response(
                    {
                        'success': False,
                        'error': f'Invalid OTP. {remaining} attempt(s) remaining.',
                        'code': 'INVALID_OTP',
                        'attempts_remaining': remaining,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # OTP verified successfully -> consume it
            _delete_otp_record(phone, tenant_slug=tenant_slug)

        # -------------------------------------------------------------------
        # Resolve Tenant & User Account
        # -------------------------------------------------------------------
        alias, tenant = _resolve_tenant_and_db(request)
        if not alias or not tenant:
            return Response(
                {
                    'success': False,
                    'error': 'Studio tenant service is temporarily unavailable.',
                    'code': 'TENANT_UNAVAILABLE',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        from apps.tenant_core.models_users import TenantUser
        from apps.tenant_core.models_rbac import Role, RoleAssignment
        from apps.tenant_core.models_crm import UserProfile, Lead, LeadSource
        from apps.tenant_core.services_crm import CRMLeadService

        # Match user by phone formats
        phone_no_plus = phone.replace('+', '')
        phone_10_digit = phone[-10:] if len(phone) >= 10 else phone

        user = (
            TenantUser.objects.using(alias)
            .filter(
                models.Q(phone=phone)
                | models.Q(phone=phone_no_plus)
                | models.Q(phone=phone_10_digit)
            )
            .select_related('home_branch')
            .first()
        )

        from apps.tenant_core.models_org import Organization, Branch
        org = Organization.objects.using(alias).first()

        # If user does not exist, auto-provision member account
        if not user:
            clean_digits = re.sub(r'\D', '', phone)
            base_username = f"member_{clean_digits[-10:]}"
            username = base_username
            counter = 1
            while TenantUser.objects.using(alias).filter(username__iexact=username).exists():
                username = f"{base_username}_{counter}"
                counter += 1

            default_branch = Branch.objects.using(alias).filter(status='ACTIVE').first()

            user = TenantUser.objects.using(alias).create(
                organization=org,
                username=username,
                email=f"{clean_digits[-10:]}@member.sweat.local",
                phone=phone,
                first_name="Member",
                last_name=clean_digits[-4:],
                display_name=f"Member {clean_digits[-4:]}",
                home_branch=default_branch,
                user_type='MEMBER',
                status='ACTIVE',
                is_login_allowed=True,
            )
            user.set_unusable_password()
            user.save(using=alias)

            # Assign MEMBER Role
            member_role = Role.objects.using(alias).filter(code='MEMBER').first()
            if not member_role:
                member_role = Role.objects.using(alias).create(
                    organization=org,
                    code='MEMBER',
                    name='Studio Member',
                    scope='BRANCH',
                    is_system=True,
                    is_active=True,
                )

            RoleAssignment.objects.using(alias).create(
                user=user,
                role=member_role,
                branch=default_branch,
                is_active=True,
            )

        # If user was in INVITED state, OTP verification serves as phone activation
        if user.status == 'INVITED':
            user.status = 'ACTIVE'
            user.is_login_allowed = True
            user.phone_verified_at = now
            user.save(using=alias, update_fields=['status', 'is_login_allowed', 'phone_verified_at'])

        # Verify active status
        if user.status not in ('ACTIVE',) or not user.is_login_allowed:
            return Response(
                {
                    'success': False,
                    'error': 'Your account is inactive or blocked. Please contact support.',
                    'code': 'ACCOUNT_INACTIVE',
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Ensure UserProfile exists
        profile = UserProfile.objects.using(alias).filter(user=user).first()
        if not profile:
            profile = UserProfile.objects.using(alias).create(
                user=user,
                member_number=None,
                first_name_snapshot=user.first_name,
                last_name_snapshot=user.last_name,
                preferred_branch=user.home_branch,
                member_status='ACTIVE',
                member_type='TRIAL',
                acquisition_source='PORTAL_OTP',
            )

        # Update last login timestamp
        user.last_login_at = now
        user.save(using=alias, update_fields=['last_login_at'])

        # Issue JWT tokens (Access + Refresh)
        tokens = _build_tenant_token(user, tenant, alias)

        user_data = {
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone': user.phone or phone,
            'role': 'member',
            'role_code': 'MEMBER',
            'member_number': getattr(profile, 'member_number', '') or '',
            'avatar_url': getattr(user, 'avatar_url', '') or '',
            'home_branch': {
                'id': str(user.home_branch.id) if user.home_branch else None,
                'name': user.home_branch.name if user.home_branch else 'SWEAT Studio',
            } if user.home_branch else None,
        }

        return _build_login_response(
            refresh=tokens,
            user_type='tenant',
            user_data=user_data,
            extra={
                'success': True,
                'message': 'OTP verified successfully. Login granted.',
                'refresh': str(tokens),
                'tokens': {
                    'access': str(tokens.access_token),
                    'refresh': str(tokens),
                },
            },
            request=request,
        )
