"""
Turnstile Fleet & Token Sync Endpoints
======================================
1. GET /api/v1/tenant/access/turnstile/active-tokens/?branch_id=<uuid_or_code>
   Returns 22-char QR tokens for every CONFIRMED/ATTENDED booking at a branch
   within [-2h, +3h] of class start, deterministically ensuring an AccessPass
   exists so the terminal has the token BEFORE the member opens the mobile app.
   Also returns `revoked_tokens` (recently cancelled passes) for immediate purge.

2. GET /api/v1/tenant/access/turnstile/devices/?branch_id=<uuid_or_code>
   Returns active TurnstileDevice rows for a branch (used by daemon on startup
   and periodic refresh).

3. GET /api/v1/tenant/access/turnstile/stale-tokens/?branch_id=<uuid_or_code>&days=7
   Returns tokens for EXPIRED/CONSUMED_EXITED/CANCELLED passes older than N days
   so terminals purge old entries cleanly.

Authentication:
  Bearer HMAC-SHA256 of "SWEAT:active-tokens:<branch_param>:<UTC-hour-epoch>"
  signed with TURNSTILE_SYNC_SECRET (accepts current and previous UTC hour).
"""

import hashlib
import hmac
import logging
import os
import uuid
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import models
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .context import get_tenant_db_alias, set_tenant_db_alias
from .models_attendance import AccessPass
from .models_bookings import Booking
from .models_classes import ClassOccurrence
from .models_crm import TrialBooking
from .models_devices import TurnstileDevice
from .models_org import Branch, Organization
from .services_access_control import (
    generate_booking_qr_token,
    generate_trial_qr_token,
    get_access_pass_qr_token,
    hash_qr_token,
)
from .services_booking_policy import BookingPolicyService

logger = logging.getLogger(__name__)

_WINDOW_HOURS: int = 3
_LOOKBACK_HOURS: int = 2
_REVOKE_WINDOW_HOURS: int = 24


def _get_sync_secret() -> str:
    return os.environ.get('TURNSTILE_SYNC_SECRET', '') or getattr(settings, 'TURNSTILE_SYNC_SECRET', '')


def _verify_sync_token(auth_header: str, branch_ref: str) -> bool:
    """
    Validates HMAC-SHA256 Bearer token sent by the daemon.
    Accepts current hour and previous hour to tolerate clock skew.
    """
    sync_secret = _get_sync_secret()
    if not sync_secret:
        logger.error(
            'TURNSTILE_SYNC_SECRET env var is not set. '
            'Turnstile sync endpoints will reject all requests.'
        )
        return False

    if not auth_header or not auth_header.startswith('Bearer '):
        return False

    provided = auth_header.split(' ', 1)[1].strip()
    now_utc = timezone.now()
    for delta_hours in (0, -1):
        hour_epoch = int(
            (now_utc + timedelta(hours=delta_hours)).replace(
                minute=0, second=0, microsecond=0
            ).timestamp()
        )
        message = f'SWEAT:active-tokens:{branch_ref}:{hour_epoch}'.encode()
        expected = hmac.new(
            sync_secret.encode(), message, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(provided, expected):
            return True

    return False


def _resolve_tenant_alias(request) -> str:
    """Resolves tenant DB alias from middleware, test override, X-Tenant-Slug, or default 'sweat'."""
    alias = getattr(request, '_tenant_alias', None) or get_tenant_db_alias()
    if alias and alias != 'default':
        return alias

    from apps.master.models import Tenant
    from apps.authentication.views import _register_and_resolve_tenant

    explicit_slug = (
        (request.headers.get('X-Tenant-Slug') if hasattr(request, 'headers') else None)
        or request.META.get('HTTP_X_TENANT_SLUG')
        or (getattr(request, 'query_params', None) or getattr(request, 'GET', {})).get('tenant')
    )
    slug = 'sweat'
    if explicit_slug:
        clean_slug = str(explicit_slug).strip().lower()
        slug = 'sweat' if clean_slug in ('sweat-demo', 'sweat_demo', 'sweat-staging', 'sweat-dev') else clean_slug

    tenant = Tenant.objects.using('default').filter(slug=slug, status='ACTIVE').first()
    if not tenant:
        tenant = Tenant.objects.using('default').filter(status='ACTIVE').first()

    if tenant:
        alias = _register_and_resolve_tenant(tenant)
        set_tenant_db_alias(alias)
        return alias
    return 'default'


def _resolve_branch(branch_ref: str, alias: str) -> Optional[Branch]:
    """Resolves an active Branch by UUID, code, or case-insensitive name without raising ValidationError."""
    raw = str(branch_ref).strip()
    if not raw:
        return None
    try:
        uid = uuid.UUID(raw)
        return Branch.objects.using(alias).filter(id=uid, status='ACTIVE').select_related('organization').first()
    except (ValueError, TypeError):
        return (
            Branch.objects.using(alias)
            .filter(status='ACTIVE')
            .filter(models.Q(code__iexact=raw) | models.Q(name__iexact=raw))
            .select_related('organization')
            .first()
        )


def _authenticate_and_resolve(request) -> tuple[Optional[str], Optional[str], Optional[Branch], Optional[Response]]:
    """Shared validation: extracts branch param, verifies HMAC token, resolves tenant DB + Branch."""
    branch_ref = (
        request.query_params.get('branch_id')
        or request.query_params.get('branch')
    )
    if not branch_ref or not str(branch_ref).strip():
        return None, None, None, Response(
            {'error': 'branch_id query parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    branch_ref = str(branch_ref).strip()
    auth_header = request.headers.get('Authorization', '')
    if not _verify_sync_token(auth_header, branch_ref):
        logger.warning(
            'turnstile sync: invalid/missing HMAC token for branch=%s remote=%s',
            branch_ref,
            request.META.get('REMOTE_ADDR'),
        )
        return None, None, None, Response(
            {'error': 'Invalid or missing authorisation token.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    alias = _resolve_tenant_alias(request)
    branch = _resolve_branch(branch_ref, alias)
    if not branch:
        return None, None, None, Response(
            {'error': f'Active branch not found: {branch_ref}'},
            status=status.HTTP_404_NOT_FOUND,
        )

    return branch_ref, alias, branch, None


class TurnstileActiveTokensView(APIView):
    """
    GET /api/v1/tenant/access/turnstile/active-tokens/?branch_id=<uuid_or_code>
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        _, alias, branch, err_resp = _authenticate_and_resolve(request)
        if err_resp:
            return err_resp

        now = timezone.now()
        window_start = now - timedelta(hours=_LOOKBACK_HOURS)
        window_end = now + timedelta(hours=_WINDOW_HOURS)

        org = branch.organization or Organization.objects.using(alias).filter(status='ACTIVE').first()
        policy = BookingPolicyService.get_effective_policy(org, branch=branch, db_alias=alias) if org else {}
        qr_activation_minutes = int(policy.get('qr_activation_minutes_before_class', 20) or 20)

        bookings = (
            Booking.objects.using(alias)
            .filter(
                branch=branch,
                status__in=('CONFIRMED', 'ATTENDED'),
                occurrence__start_at__gte=window_start,
                occurrence__start_at__lte=window_end,
            )
            .select_related(
                'user_profile',
                'occurrence__class_template__category',
                'access_pass',
            )
            .order_by('occurrence__start_at')
        )

        secret_key: str = settings.SECRET_KEY
        token_list = []
        seen_pass_ids = set()
        covered_trial_pairs = set()

        for booking in bookings:
            occ = booking.occurrence
            profile = booking.user_profile
            if not occ or not occ.start_at or not profile:
                continue

            if booking.booking_type == 'TRIAL':
                covered_trial_pairs.add((profile.id, occ.id))

            start_at = occ.start_at
            class_tpl = occ.class_template
            duration = (class_tpl.default_duration_minutes if (class_tpl and class_tpl.default_duration_minutes) else 50)
            end_at = occ.end_at or (start_at + timedelta(minutes=duration))
            valid_from_dt = start_at - timedelta(minutes=qr_activation_minutes)
            valid_until_dt = end_at + timedelta(minutes=60)

            qr_token = generate_booking_qr_token(str(booking.id), secret_key)
            t_hash = hash_qr_token(qr_token)

            ap = getattr(booking, 'access_pass', None)
            if ap is None:
                if valid_until_dt < now:
                    continue
                ap, _ = AccessPass.objects.using(alias).get_or_create(
                    booking=booking,
                    defaults={
                        'user_profile': profile,
                        'branch': branch,
                        'token_hash': t_hash,
                        'pass_type': 'CLASS_BOOKING',
                        'valid_from': valid_from_dt,
                        'valid_until': valid_until_dt,
                        'status': 'ACTIVE',
                        'updated_at': now,
                    },
                )

            if ap.status not in ('ACTIVE', 'CONSUMED_ENTERED') or (ap.valid_until and ap.valid_until < now):
                continue

            seen_pass_ids.add(ap.id)
            user_id_for_terminal = 'P' + str(ap.id).replace('-', '')[:7]
            display_name = (
                f"{getattr(profile, 'first_name_snapshot', '')} "
                f"{getattr(profile, 'last_name_snapshot', '')}"
            ).strip() or getattr(profile, 'member_number', '') or 'Member'

            category = getattr(class_tpl, 'category', None) if class_tpl else None
            category_code = (getattr(category, 'code', '') or '').strip()

            token_list.append({
                'token': qr_token,
                'pass_id': str(ap.id),
                'user_id': user_id_for_terminal,
                'display_name': display_name,
                'class_name': getattr(class_tpl, 'name', 'Class') if class_tpl else 'Class',
                'class_category_code': category_code,
                'occurrence_start': start_at.isoformat(),
                'valid_from': ap.valid_from.isoformat(),
                'valid_until': ap.valid_until.isoformat(),
                'pass_status': ap.status,
                'direction': 'EXIT' if ap.status == 'CONSUMED_ENTERED' else 'BOTH',
            })

        # Also sync upcoming CRM / WhatsApp TrialBookings (excluding any covered by a mobile Booking)
        trials = list(
            TrialBooking.objects.using(alias)
            .filter(
                branch=branch,
                status__in=('BOOKED', 'CONFIRMED'),
                scheduled_start__gte=window_start,
                scheduled_start__lte=window_end,
            )
            .exclude(confirmation_status__in=('CANCELLED', 'DECLINED'))
            .select_related('lead', 'user_profile', 'access_pass')
            .order_by('scheduled_start')
        )
        occ_ids = [t.class_occurrence_id for t in trials if t.class_occurrence_id]
        occ_map = (
            ClassOccurrence.objects.using(alias)
            .filter(id__in=occ_ids)
            .select_related('class_template__category')
            .in_bulk()
        ) if occ_ids else {}
        superseded_trial_ids = set(
            TrialBooking.objects.using(alias)
            .filter(
                rescheduled_from_id__in=[t.id for t in trials],
                status__in=('BOOKED', 'CONFIRMED', 'SCHEDULED'),
            )
            .exclude(confirmation_status__in=('CANCELLED', 'DECLINED'))
            .values_list('rescheduled_from_id', flat=True)
        ) if trials else set()

        for trial in trials:
            if trial.id in superseded_trial_ids:
                continue
            if trial.user_profile_id and trial.class_occurrence_id and (trial.user_profile_id, trial.class_occurrence_id) in covered_trial_pairs:
                continue

            occ = occ_map.get(trial.class_occurrence_id) if trial.class_occurrence_id else None
            start_at = occ.start_at if (occ and occ.start_at) else trial.scheduled_start
            if not start_at:
                continue

            class_tpl = occ.class_template if occ else None
            duration = (class_tpl.default_duration_minutes if (class_tpl and class_tpl.default_duration_minutes) else 50)
            end_at = (occ.end_at if occ else None) or trial.scheduled_end or (start_at + timedelta(minutes=duration))
            valid_from_dt = start_at - timedelta(minutes=qr_activation_minutes)
            valid_until_dt = end_at + timedelta(minutes=60)

            qr_token = generate_trial_qr_token(str(trial.id), secret_key)
            t_hash = hash_qr_token(qr_token)

            ap = getattr(trial, 'access_pass', None)
            if ap is None:
                if valid_until_dt < now:
                    continue
                ap, _ = AccessPass.objects.using(alias).get_or_create(
                    trial_booking=trial,
                    defaults={
                        'user_profile': trial.user_profile,
                        'branch': branch,
                        'token_hash': t_hash,
                        'pass_type': 'TRIAL_BOOKING',
                        'valid_from': valid_from_dt,
                        'valid_until': valid_until_dt,
                        'status': 'ACTIVE',
                        'updated_at': now,
                    },
                )

            if ap.id in seen_pass_ids:
                continue
            if ap.status not in ('ACTIVE', 'CONSUMED_ENTERED') or (ap.valid_until and ap.valid_until < now):
                continue

            seen_pass_ids.add(ap.id)
            user_id_for_terminal = 'P' + str(ap.id).replace('-', '')[:7]
            lead = getattr(trial, 'lead', None)
            display_name = (
                f"{getattr(lead, 'first_name', '')} {getattr(lead, 'last_name', '')}"
            ).strip() or (getattr(lead, 'phone', '') if lead else '') or 'Trial Guest'

            category = getattr(class_tpl, 'category', None) if class_tpl else None
            category_code = (getattr(category, 'code', '') or '').strip()

            token_list.append({
                'token': qr_token,
                'pass_id': str(ap.id),
                'user_id': user_id_for_terminal,
                'display_name': display_name,
                'class_name': (getattr(class_tpl, 'name', None) if class_tpl else None) or trial.trial_type or 'Trial Session',
                'class_category_code': category_code,
                'occurrence_start': start_at.isoformat(),
                'valid_from': ap.valid_from.isoformat(),
                'valid_until': ap.valid_until.isoformat(),
                'pass_status': ap.status,
                'direction': 'EXIT' if ap.status == 'CONSUMED_ENTERED' else 'BOTH',
            })

        # Also return recently cancelled tokens so terminals revoke them within 60s
        revoke_cutoff = now - timedelta(hours=_REVOKE_WINDOW_HOURS)
        revoked_passes = (
            AccessPass.objects.using(alias)
            .filter(
                branch=branch,
                status='CANCELLED',
                updated_at__gte=revoke_cutoff,
            )
            .only('id', 'booking_id', 'trial_booking_id')
        )
        revoked_tokens = [
            t for t in (get_access_pass_qr_token(rp, secret_key) for rp in revoked_passes) if t
        ]

        logger.info(
            'active-tokens: branch=%s active=%d revoked=%d (window=%dh)',
            branch.name, len(token_list), len(revoked_tokens), _WINDOW_HOURS,
        )

        return Response({
            'branch_id': str(branch.id),
            'branch_name': branch.name,
            'as_of': now.isoformat(),
            'window_hours': _WINDOW_HOURS,
            'count': len(token_list),
            'tokens': token_list,
            'revoked_tokens': revoked_tokens,
        }, status=status.HTTP_200_OK)


class TurnstileDeviceListView(APIView):
    """
    GET /api/v1/tenant/access/turnstile/devices/?branch_id=<uuid_or_code>
    Returns active TurnstileDevice records for a branch.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        _, alias, branch, err_resp = _authenticate_and_resolve(request)
        if err_resp:
            return err_resp

        devices = TurnstileDevice.objects.using(alias).filter(
            branch=branch,
            is_active=True,
        ).order_by('name')

        device_list = [
            {
                'id': str(d.id),
                'branch_id': str(d.branch_id),
                'name': d.name,
                'direction': d.direction,
                'host': d.host,
                'port': d.port,
                'device_serial': d.device_serial,
                'class_category_code': d.class_category_code,
                'is_active': d.is_active,
            }
            for d in devices
        ]

        logger.info(
            'devices: branch=%s returned %d device(s)', branch.name, len(device_list)
        )

        return Response({
            'branch_id': str(branch.id),
            'branch_name': branch.name,
            'count': len(device_list),
            'devices': device_list,
        }, status=status.HTTP_200_OK)


class TurnstileStaleTokensView(APIView):
    """
    GET /api/v1/tenant/access/turnstile/stale-tokens/?branch_id=<uuid_or_code>&days=7
    Returns raw QR tokens whose AccessPass is EXPIRED/CONSUMED_EXITED/CANCELLED older than N days.
    """
    permission_classes = [permissions.AllowAny]
    _STALE_DAYS_DEFAULT = 7

    def get(self, request, *args, **kwargs):
        _, alias, branch, err_resp = _authenticate_and_resolve(request)
        if err_resp:
            return err_resp

        try:
            days = max(1, int(request.query_params.get('days', self._STALE_DAYS_DEFAULT)))
        except (TypeError, ValueError):
            days = self._STALE_DAYS_DEFAULT

        cutoff = timezone.now() - timedelta(days=days)

        stale_passes = (
            AccessPass.objects.using(alias)
            .filter(
                branch=branch,
                status__in=('EXPIRED', 'CONSUMED_EXITED', 'CANCELLED'),
                updated_at__lte=cutoff,
            )
            .only('id', 'booking_id', 'trial_booking_id')
        )

        secret_key: str = settings.SECRET_KEY
        stale_tokens = [
            t for t in (get_access_pass_qr_token(ap, secret_key) for ap in stale_passes) if t
        ]

        logger.info(
            'stale-tokens: branch=%s returning %d stale token(s) (cutoff=%s)',
            branch.name, len(stale_tokens), cutoff.date(),
        )

        return Response({
            'branch_id': str(branch.id),
            'days': days,
            'count': len(stale_tokens),
            'tokens': stale_tokens,
        }, status=status.HTTP_200_OK)
