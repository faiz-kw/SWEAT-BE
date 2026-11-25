"""
apps/tenant_core/views_public_trial.py — Public 1-Tap WhatsApp Action Landing Handlers.
Replicates the high-converting 1-tap browser confirmation & management flow (e.g. Enrich video workflow)
without requiring member authentication. Protected by cryptographic HMAC timestamp signatures.
"""

import html
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.conf import settings
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.core.signing import BadSignature, SignatureExpired

from apps.tenant_core.communication.whatsapp_tokens import verify_trial_action_token
from apps.tenant_core.models_attendance import AccessPass
from apps.tenant_core.models_classes import ClassOccurrence
from apps.tenant_core.models_crm import TrialBooking, TrialStatusHistory
from apps.tenant_core.services_access_control import (
    generate_trial_qr_token,
    hash_qr_token,
    push_qr_to_studio_terminals,
)
from apps.tenant_core.services_booking_policy import BookingPolicyService
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_reliability import record_business_audit
from apps.tenant_core.communication.whatsapp_variables import fmt_ist_datetime
from apps.tenant_core.context import tenant_database_context
from apps.master.models_tenant import Tenant

logger = logging.getLogger(__name__)


def _resolve_trial_booking(trial_id, tenant_id=None):
    """
    Safely resolves TrialBooking across tenant databases.
    If tenant_id is provided, checks that tenant's database directly.
    Otherwise discovers the active tenant containing the trial.
    Returns (trial, tenant_uuid, db_alias) or (None, None, None).
    """
    if tenant_id:
        try:
            with tenant_database_context(tenant_id) as alias:
                tb = TrialBooking.objects.using(alias).select_related('lead', 'branch').filter(id=trial_id).first()
                if tb:
                    return tb, tenant_id, alias
        except Exception as e:
            logger.warning("Could not resolve trial in specified tenant %s: %s", tenant_id, e)

    for t in Tenant.objects.using('default').filter(status='ACTIVE'):
        try:
            with tenant_database_context(t.id) as alias:
                tb = TrialBooking.objects.using(alias).select_related('lead', 'branch').filter(id=trial_id).first()
                if tb:
                    return tb, t.id, alias
        except Exception:
            continue
    return None, None, None


def _render_html_response(title: str, heading: str, subtext: str, badge_color: str, icon_svg: str, details_html: str, action_button_html: str = "") -> HttpResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — SWEAT Elite</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: #0b0f19;
            color: #f3f4f6;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px 16px;
        }}
        .card {{
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 20px;
            max-width: 440px;
            width: 100%;
            padding: 32px 24px;
            text-align: center;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.5);
        }}
        .icon-circle {{
            width: 72px;
            height: 72px;
            border-radius: 50%;
            background: rgba({badge_color}, 0.15);
            border: 2px solid rgba({badge_color}, 0.4);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 20px;
            color: rgb({badge_color});
        }}
        .title {{
            font-size: 24px;
            font-weight: 800;
            letter-spacing: -0.02em;
            margin-bottom: 8px;
            color: #ffffff;
        }}
        .subtext {{
            font-size: 14px;
            color: #9ca3af;
            line-height: 1.5;
            margin-bottom: 24px;
        }}
        .details-box {{
            background: #1a2234;
            border: 1px solid #283548;
            border-radius: 14px;
            padding: 18px 16px;
            text-align: left;
            margin-bottom: 24px;
            font-size: 13px;
        }}
        .detail-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }}
        .detail-row:last-child {{
            border-bottom: none;
            padding-bottom: 0;
        }}
        .detail-row:first-child {{
            padding-top: 0;
        }}
        .detail-label {{
            color: #9ca3af;
            font-weight: 500;
        }}
        .detail-val {{
            color: #f3f4f6;
            font-weight: 700;
            text-align: right;
        }}
        .brand-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: #10b981;
            margin-bottom: 12px;
        }}
        .btn {{
            display: block;
            width: 100%;
            padding: 14px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 700;
            text-decoration: none;
            text-align: center;
            transition: all 0.2s;
            cursor: pointer;
            border: none;
        }}
        .btn-primary {{
            background: #10b981;
            color: #ffffff;
            margin-bottom: 10px;
        }}
        .btn-secondary {{
            background: #1f2937;
            color: #d1d5db;
            border: 1px solid #374151;
        }}
        .footer-note {{
            font-size: 11px;
            color: #6b7280;
            margin-top: 20px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="brand-badge">⚡ SWEAT STUDIO PASS</div>
        <div class="icon-circle">
            {icon_svg}
        </div>
        <h1 class="title">{heading}</h1>
        <p class="subtext">{subtext}</p>
        {details_html}
        {action_button_html}
        <div class="footer-note">SWEAT Performance & Wellness · Enterprise Studio Network</div>
    </div>
</body>
</html>"""
    return HttpResponse(html, content_type='text/html')


class PublicTrialConfirmationView(View):
    """
    Handles 1-tap confirmation URL clicks from WhatsApp buttons.
    Replicates Enrich Beauty appointment confirmation checkmark page.
    """
    def get(self, request, token):
        try:
            trial_id, action, tenant_id = verify_trial_action_token(token)
            if action != 'CONFIRM':
                raise BadSignature("Invalid action token")
        except SignatureExpired:
            return _render_html_response(
                title="Link Expired",
                heading="Link Has Expired",
                subtext="This confirmation link has expired. Please contact studio reception.",
                badge_color="239, 68, 68",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>',
                details_html="",
            )
        except BadSignature:
            return _render_html_response(
                title="Invalid Link",
                heading="Invalid Link",
                subtext="We couldn't verify this confirmation link.",
                badge_color="239, 68, 68",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>',
                details_html="",
            )

        trial, tenant_uuid, db_alias = _resolve_trial_booking(trial_id, tenant_id)
        if not trial:
            return _render_html_response(
                title="Session Not Found",
                heading="Booking Not Found",
                subtext="We couldn't locate this trial reservation. Please contact studio reception.",
                badge_color="239, 68, 68",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>',
                details_html="",
            )

        if trial.status == 'CANCELLED':
            return _render_html_response(
                title="Session Cancelled",
                heading="Session Cancelled",
                subtext="This trial reservation was previously released or cancelled.",
                badge_color="245, 158, 11",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>',
                details_html="",
            )

        # Atomic confirmation update inside tenant database context
        with tenant_database_context(tenant_uuid):
            with transaction.atomic(using=db_alias):
                if trial.confirmation_status != 'CONFIRMED':
                    trial.confirmation_status = 'CONFIRMED'
                    trial.confirmed_at = timezone.now()
                    trial.confirmation_channel = 'WHATSAPP'
                    trial.save(using=db_alias, update_fields=['confirmation_status', 'confirmed_at', 'confirmation_channel', 'updated_at'])

                    TrialStatusHistory.objects.using(db_alias).create(
                        trial_booking=trial,
                        from_status=trial.status,
                        to_status=trial.status,
                        reason_code='CONFIRMED_VIA_WHATSAPP_LINK',
                    )

                    if trial.lead:
                        if trial.lead.current_status != 'TRIAL_CONFIRMED':
                            trial.lead.current_status = 'TRIAL_CONFIRMED'
                            trial.lead.save(using=db_alias, update_fields=['current_status', 'updated_at'])
                        from apps.tenant_core.models_crm import LeadActivity
                        LeadActivity.objects.using(db_alias).create(
                            lead=trial.lead,
                            activity_type='TRIAL',
                            outcome='Confirmed via WhatsApp Link',
                            notes=f"Customer confirmed attendance for {trial.trial_type or 'Trial Session'} on {fmt_ist_datetime(trial.scheduled_start)} via WhatsApp 1-tap confirmation button.",
                        )

        if request.headers.get('accept') == 'application/json':
            return JsonResponse({'status': 'CONFIRMED', 'trial_id': str(trial.id)})

        lead_name = f"{trial.lead.first_name} {trial.lead.last_name}".strip() if trial.lead else "Athlete"
        branch_name = trial.branch.name if trial.branch else "SWEAT Studio"
        branch_maps_url = trial.branch.get_google_maps_url() if hasattr(trial.branch, 'get_google_maps_url') else "https://maps.google.com"
        date_str = fmt_ist_datetime(trial.scheduled_start)

        details_html = f"""
        <div class="details-box">
            <div class="detail-row">
                <span class="detail-label">Athlete</span>
                <span class="detail-val">{lead_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Session</span>
                <span class="detail-val">{trial.trial_type or 'SWEAT Session'}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Scheduled For</span>
                <span class="detail-val">{date_str}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Studio</span>
                <span class="detail-val">{branch_name}</span>
            </div>
        </div>
        """

        action_buttons = f"""
        <a href="{branch_maps_url}" target="_blank" class="btn btn-primary">Open Google Maps Directions 🧭</a>
        """

        check_icon = '<svg width="40" height="40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"></path></svg>'

        return _render_html_response(
            title="Confirmed",
            heading="You're Confirmed!",
            subtext=f"The studio knows to expect you. Please arrive 20 minutes before start time for gear setup.",
            badge_color="16, 185, 129",
            icon_svg=check_icon,
            details_html=details_html,
            action_button_html=action_buttons,
        )


class PublicTrialRescheduleView(View):
    """
    Handles 1-tap reschedule link clicks.
    Enforces the strict 1-reschedule rule.
    """
    def get(self, request, token):
        try:
            trial_id, action, tenant_id = verify_trial_action_token(token)
            if action not in ('RESCHEDULE', 'REBOOK'):
                raise BadSignature("Invalid action token")
        except (SignatureExpired, BadSignature) as err:
            return _render_html_response(
                title="Link Invalid",
                heading="Link Expired or Invalid",
                subtext="This reschedule link is no longer valid. Please contact our support team.",
                badge_color="239, 68, 68",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>',
                details_html="",
            )

        trial, tenant_uuid, db_alias = _resolve_trial_booking(trial_id, tenant_id)
        if not trial:
            return _render_html_response(
                title="Session Not Found",
                heading="Booking Not Found",
                subtext="We couldn't locate this trial reservation. Please contact studio reception.",
                badge_color="239, 68, 68",
                icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>',
                details_html="",
            )

        with tenant_database_context(tenant_uuid):
            # Enforce 1-reschedule policy
            already_rescheduled = (
                trial.rescheduled_from is not None
                or TrialStatusHistory.objects.using(db_alias).filter(trial_booking=trial, to_status='RESCHEDULED').exists()
            )

            if already_rescheduled and action != 'REBOOK':
                support_phone = trial.branch.phone if trial.branch else "+919136344868"
                return _render_html_response(
                    title="Reschedule Limit Reached",
                    heading="Reschedule Already Used",
                    subtext="Your 1-time complimentary reschedule has already been used for this trial booking.",
                    badge_color="245, 158, 11",
                    icon_svg='<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>',
                    details_html=f"""
                    <div class="details-box">
                        <p style="color:#d1d5db; line-height:1.6;">To change your schedule further, please call our studio team directly at <strong>{support_phone}</strong>.</p>
                    </div>
                    """,
                    action_button_html=f'<a href="tel:{support_phone}" class="btn btn-primary">Call Studio Support 📞</a>',
                )

            # Mark trial as reschedule requested via authoritative CRM service
            from apps.tenant_core.services_crm import CRMLeadService
            CRMLeadService.request_reschedule_trial(
                trial=trial,
                reason="Customer clicked 1-tap Reschedule link from WhatsApp notification",
                db_alias=db_alias,
            )

        support_phone = trial.branch.phone if (trial.branch and trial.branch.phone) else "+919136344868"
        athlete_name = f"{trial.lead.first_name} {trial.lead.last_name}".strip() if trial.lead else "Athlete"
        branch_name = trial.branch.name if trial.branch else "SWEAT Fitness"

        details_html = f"""
        <div class="details-box">
            <div class="detail-row">
                <span class="detail-label">Athlete</span>
                <span class="detail-val">{athlete_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Current Session</span>
                <span class="detail-val">{trial.trial_type or 'SWEAT Workout'}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Studio</span>
                <span class="detail-val">{branch_name}</span>
            </div>
            <div style="margin-top:14px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.08); color:#d1d5db; line-height:1.6; font-size:13px;">
                Our studio team has received your request. Your wellness counselor will call you within 2 business hours to book you into a convenient upcoming session.
            </div>
        </div>
        """
        action_button = f'<a href="tel:{support_phone}" class="btn btn-primary">Call Studio Directly ({support_phone}) 📞</a>'

        calendar_icon = '<svg width="40" height="40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>'

        return _render_html_response(
            title="Reschedule Request Received",
            heading="Request Received",
            subtext="We'll help you find another time that fits your schedule.",
            badge_color="59, 130, 246",
            icon_svg=calendar_icon,
            details_html=details_html,
            action_button_html=action_button,
        )


class PublicTrialPassView(View):
    """
    Renders digital studio pass with QR code for turnstile check-in.
    Enforces trial lifecycle, ensures AccessPass with deterministic 22-char QR token,
    and pushes active tokens to studio terminals.
    """
    _ERR_ICON = '<svg width="36" height="36" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>'

    def get(self, request, token):
        try:
            trial_id, action, tenant_id = verify_trial_action_token(token)
            if action != 'PASS':
                raise BadSignature("Invalid action token")
        except (SignatureExpired, BadSignature):
            return _render_html_response(
                title="Pass Expired",
                heading="Pass Expired",
                subtext="This studio pass has expired or is invalid.",
                badge_color="239, 68, 68",
                icon_svg=self._ERR_ICON,
                details_html="",
            )

        trial, tenant_uuid, db_alias = _resolve_trial_booking(trial_id, tenant_id)
        if not trial:
            return _render_html_response(
                title="Pass Not Found",
                heading="Pass Not Found",
                subtext="This studio pass could not be located. Please contact studio reception.",
                badge_color="239, 68, 68",
                icon_svg=self._ERR_ICON,
                details_html="",
            )

        with tenant_database_context(tenant_uuid) as alias:
            trial = (
                TrialBooking.objects.using(alias)
                .select_related('lead', 'lead__organization', 'branch', 'branch__organization')
                .filter(id=trial_id)
                .first()
            )
            if not trial:
                return _render_html_response(
                    title="Pass Not Found",
                    heading="Pass Not Found",
                    subtext="This studio pass could not be located. Please contact studio reception.",
                    badge_color="239, 68, 68",
                    icon_svg=self._ERR_ICON,
                    details_html="",
                )

            has_replacement = TrialBooking.objects.using(alias).filter(
                rescheduled_from=trial,
                status__in=['BOOKED', 'CONFIRMED', 'SCHEDULED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).exists()

            if (
                trial.status in ('CANCELLED', 'RESCHEDULED', 'NO_SHOW')
                or trial.confirmation_status in ('CANCELLED', 'DECLINED')
                or has_replacement
            ):
                return _render_html_response(
                    title="Pass No Longer Valid",
                    heading="Pass No Longer Valid",
                    subtext="This trial booking was cancelled, superseded, or missed. Please contact studio reception for assistance.",
                    badge_color="239, 68, 68",
                    icon_svg=self._ERR_ICON,
                    details_html="",
                )

            occ = None
            if trial.class_occurrence_id:
                occ = (
                    ClassOccurrence.objects.using(alias)
                    .select_related('class_template', 'class_template__category', 'branch', 'branch__organization')
                    .filter(id=trial.class_occurrence_id)
                    .first()
                )

            pass_branch = trial.branch or (occ.branch if occ else None)
            start_at = (occ.start_at if (occ and occ.start_at) else trial.scheduled_start)
            end_at = (occ.end_at if (occ and occ.end_at) else trial.scheduled_end)
            if start_at and not end_at:
                tpl_dur = occ.class_template.default_duration_minutes if (occ and occ.class_template and occ.class_template.default_duration_minutes) else 50
                end_at = start_at + timedelta(minutes=tpl_dur)

            now = timezone.now()
            if end_at and (end_at + timedelta(minutes=15)) < now:
                return _render_html_response(
                    title="Pass Expired",
                    heading="Session Window Ended",
                    subtext="This trial session has concluded and its turnstile pass has expired.",
                    badge_color="239, 68, 68",
                    icon_svg=self._ERR_ICON,
                    details_html="",
                )

            org = (
                (pass_branch.organization if pass_branch else None)
                or (trial.lead.organization if trial.lead else None)
            )
            policy = BookingPolicyService.get_effective_policy(org, branch=pass_branch, db_alias=alias) if org else {}
            qr_activation_minutes = int(policy.get('qr_activation_minutes_before_class', 20) or 20)

            valid_from_dt = (start_at - timedelta(minutes=qr_activation_minutes)) if start_at else now
            valid_until_dt = (end_at + timedelta(minutes=60)) if end_at else (now + timedelta(hours=2))

            existing_pass = AccessPass.objects.using(alias).filter(trial_booking=trial).first()
            if existing_pass and existing_pass.status == 'CANCELLED':
                return _render_html_response(
                    title="Pass Cancelled",
                    heading="Pass Cancelled",
                    subtext="This turnstile access pass has been revoked.",
                    badge_color="239, 68, 68",
                    icon_svg=self._ERR_ICON,
                    details_html="",
                )

            if existing_pass and existing_pass.status == 'CONSUMED_EXITED':
                return _render_html_response(
                    title="Session Completed",
                    heading="Thanks for Visiting SWEAT!",
                    subtext="You have checked out of this trial session. Our wellness counselor will follow up with you shortly.",
                    badge_color="59, 130, 246",
                    icon_svg='<svg width="40" height="40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>',
                    details_html="",
                )

            qr_token = generate_trial_qr_token(str(trial.id), settings.SECRET_KEY)
            t_hash = hash_qr_token(qr_token)
            lead_name = f"{trial.lead.first_name} {trial.lead.last_name}".strip() if trial.lead else "Athlete"
            category_code = (
                occ.class_template.category.code
                if occ and occ.class_template and getattr(occ.class_template, 'category', None)
                else ''
            )

            pass_obj = existing_pass
            if pass_branch:
                if existing_pass and existing_pass.status == 'CONSUMED_ENTERED':
                    existing_pass.valid_until = valid_until_dt
                    existing_pass.updated_at = now
                    existing_pass.save(using=alias, update_fields=['valid_until', 'updated_at'])
                    pass_obj = existing_pass
                else:
                    pass_obj, _ = AccessPass.objects.using(alias).update_or_create(
                        trial_booking=trial,
                        defaults={
                            'user_profile': trial.user_profile,
                            'branch': pass_branch,
                            'token_hash': t_hash,
                            'pass_type': 'TRIAL_BOOKING',
                            'valid_from': valid_from_dt,
                            'valid_until': valid_until_dt,
                            'status': 'ACTIVE',
                            'updated_at': now,
                        },
                    )
                if pass_obj and (not start_at or now >= (start_at - timedelta(hours=2))):
                    push_qr_to_studio_terminals(
                        pass_id=str(pass_obj.id),
                        display_name=lead_name or "Trial Guest",
                        qr_token=qr_token,
                        branch=pass_branch,
                        category_code=category_code,
                        db_alias=alias,
                    )

            tz_str = (pass_branch.timezone if pass_branch and pass_branch.timezone else None) or 'Asia/Kolkata'
            try:
                branch_tz = ZoneInfo(tz_str)
            except Exception:
                branch_tz = ZoneInfo('Asia/Kolkata')

            date_str = fmt_ist_datetime(start_at) if start_at else ""
            act_local = valid_from_dt.astimezone(branch_tz).strftime('%I:%M %p').lstrip('0')
            branch_name = html.escape(pass_branch.name if pass_branch else "SWEAT Studio")
            session_label = html.escape(
                (occ.class_template.name if (occ and occ.class_template) else None)
                or trial.trial_type
                or 'SWEAT Session'
            )
            escaped_lead_name = html.escape(lead_name or "Athlete")

        window_note = (
            f"Checked In — Scan QR at Exit Gate when leaving"
            if (pass_obj and pass_obj.status == 'CONSUMED_ENTERED')
            else (
                f"Gate entry opens at {act_local} ({qr_activation_minutes} mins before class)"
                if (start_at and now < valid_from_dt)
                else "Scan at Turnstile or Front Desk Scanner"
            )
        )

        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={qr_token}&bgcolor=17-24-39&color=243-244-246"

        details_html = f"""
        <div style="background:#111827; padding:16px; border-radius:12px; margin-bottom:20px; border: 1px solid #374151; text-align:center;">
            <img src="{qr_image_url}" alt="Access QR Code" style="width:180px; height:180px; border-radius:8px; display:block; margin:0 auto;" />
            <div style="font-size:12px; color:#9ca3af; margin-top:10px;">{html.escape(window_note)}</div>
        </div>
        <div class="details-box">
            <div class="detail-row">
                <span class="detail-label">Athlete</span>
                <span class="detail-val">{escaped_lead_name}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Session</span>
                <span class="detail-val">{session_label}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Session Time</span>
                <span class="detail-val">{html.escape(date_str)}</span>
            </div>
            <div class="detail-row">
                <span class="detail-label">Studio</span>
                <span class="detail-val">{branch_name}</span>
            </div>
        </div>
        """

        pass_icon = '<svg width="40" height="40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"></path></svg>'

        return _render_html_response(
            title="Studio Entry Pass",
            heading="Studio Access Pass",
            subtext="Present this pass to the front-desk turnstile scanner upon arrival.",
            badge_color="16, 185, 129",
            icon_svg=pass_icon,
            details_html=details_html,
        )
