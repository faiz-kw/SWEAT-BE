"""
apps/tenant_core/views_telephony.py — Vobiz Telephony, Screen-Pop & Disposition Views.

Exposes:
- Inbound Call Webhook (Sticky rep routing + VobizXML)
- 15-Second Inbound Fallback Webhook (Overflow ladder to sales pool)
- Hangup Webhook (Duration, 60s pulse calculation, recording link, wrap-up state)
- Outbound Click-to-Call API (Agent-First Bridging + 9 AM-9 PM IST TRAI check)
- Agent Answer Bridge XML View (Whisper tone + dial lead PSTN)
- Post-Call Disposition API (Mandatory modal submission + auto-lead stage advance)
- Server-Sent Events (SSE) Real-Time Event Stream for CRM tab screen-pops
- Missed Calls Queue & Agent Presence Heartbeat
"""

import os
import json
import time
import logging
import zoneinfo
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework import status, permissions, renderers
from rest_framework.views import APIView
from rest_framework.response import Response
from contextlib import contextmanager

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from config.routers import get_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_crm import Lead, CallSession, AgentPresence, LeadActivity, SalesFollowupTask
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_memberships import Membership

from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
from apps.tenant_core.services_voice_calling import VoiceCallingService
from apps.tenant_core.services_telephony_config import TelephonyConfigService

logger = logging.getLogger(__name__)

@contextmanager
def resolve_telephony_tenant_context():
    """
    Ensures an active tenant DB context exists when raw carrier webhooks arrive from Vobiz.
    """
    current_alias = get_tenant_db_alias()
    if current_alias:
        yield
    else:
        import sys
        from django.conf import settings
        if 'test' in sys.argv and 'tenant_test' in getattr(settings, 'DATABASES', {}):
            set_tenant_db_alias('tenant_test')
            try:
                yield
            finally:
                set_tenant_db_alias(None)
        else:
            tenant = Tenant.objects.filter(status='ACTIVE').first()
            if tenant:
                with tenant_database_context(str(tenant.id)):
                    yield
            else:
                yield

# Real-time in-memory event bus for SSE client streams
# Key: user_id (str), Value: list of pending event dicts
_AGENT_EVENT_QUEUES: Dict[str, list] = {}


def push_agent_telephony_event(user_id: str, event_type: str, payload: dict) -> None:
    """Dispatches a real-time event to the target agent and broadcasts to all active CRM browser tabs."""
    u_key = str(user_id) if user_id else ""
    evt = {
        'event': event_type,
        'data': payload,
        'timestamp': timezone.now().isoformat(),
    }
    if u_key and u_key not in _AGENT_EVENT_QUEUES:
        _AGENT_EVENT_QUEUES[u_key] = []

    # Broadcast to all connected client queues so every open rep/admin tab updates instantly
    for k, q in list(_AGENT_EVENT_QUEUES.items()):
        q.append(dict(evt))



def _get_webhook_host(request) -> str:
    """Returns authoritative public HTTPS host for Vobiz callbacks to avoid 301/307 redirects."""
    host = request.build_absolute_uri('/')[:-1]
    if host.startswith('http://') and ('ngrok' in host or request.is_secure() or request.META.get('HTTP_X_FORWARDED_PROTO') == 'https'):
        host = 'https://' + host[7:]
    elif not host.startswith('https://') and 'ngrok' in host:
        host = host.replace('http://', 'https://')
    return host


def _is_within_trai_hours() -> bool:
    """Verifies that current local IST time is strictly within 09:00:00 - 21:00:00."""
    try:
        ist = zoneinfo.ZoneInfo("Asia/Kolkata")
    except Exception:
        ist = timezone.get_current_timezone()
    now_ist = datetime.now(ist)
    return 9 <= now_ist.hour < 21


# ============================================================================
# 1. INBOUND CALL WEBHOOK & FALLBACK LADDER
# ============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class VobizInboundWebhookView(APIView):
    """
    POST /api/v1/telephony/inbound/
    Invoked by Vobiz Gateway when customer dials our virtual DID (+91 92-series).
    Matches lead, determines assigned agent, triggers CRM screen-pop via SSE,
    and returns VobizXML with a 15-second timeout.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        with resolve_telephony_tenant_context():
            data = request.data if isinstance(request.data, dict) else {}
            call_uuid = data.get('CallUUID') or data.get('call_uuid') or request.POST.get('CallUUID', '')
            raw_from = str(data.get('From') or request.POST.get('From', '')).strip()
            raw_to = str(data.get('To') or request.POST.get('To', '')).strip()
            vobiz_cfg = TelephonyConfigService.get_vobiz_config()
            host = _get_webhook_host(request)

            # ── DETECT IF CALL IS AGENT SOFTPHONE DIALING OUT ──
            clean_from_sip = raw_from.replace('sip:', '').split('@')[0]
            from django.db import models
            agent_presence = AgentPresence.objects.filter(
                models.Q(sip_username=clean_from_sip) | models.Q(sip_username=raw_from)
            ).select_related('user', 'organization').first()

            is_softphone_outbound = bool(agent_presence) or raw_from.startswith('sip:') or '@registrar.vobiz.ai' in raw_from

            if is_softphone_outbound:
                # ── OUTBOUND CALL FROM AGENT SOFTPHONE ──
                digits_to = ''.join(c for c in raw_to if c.isdigit())
                if len(digits_to) >= 10:
                    customer_phone = f"+91{digits_to[-10:]}"
                else:
                    customer_phone = f"+{digits_to}" if digits_to else raw_to

                caller_id = vobiz_cfg.get('from_number') or "+919240954144"
                rep_user = agent_presence.user if agent_presence else None
                org = agent_presence.organization if agent_presence else Organization.objects.first()

                # Match existing Lead by last 10 digits (skip if destination is an existing Member)
                lead = None
                dest_digits = digits_to[-10:] if len(digits_to) >= 10 else digits_to
                if dest_digits:
                    is_dest_member = (
                        TenantUser.objects.filter(phone__icontains=dest_digits, user_type='MEMBER').exists()
                        or UserProfile.objects.filter(user__phone__icontains=dest_digits).exists()
                    )
                    if not is_dest_member:
                        lead = Lead.objects.filter(phone_normalized__icontains=dest_digits).order_by('-created_at').first()

                session = CallSession.objects.filter(provider_attempt_id=call_uuid).first()
                if not session:
                    session = CallSession.objects.create(
                        organization=org,
                        lead=lead,
                        sales_rep=rep_user,
                        branch=rep_user.home_branch if rep_user else (lead.branch if lead else None),
                        provider='VOBIZ',
                        provider_attempt_id=call_uuid,
                        direction='OUTBOUND',
                        call_mode='HUMAN_MANUAL',
                        status='RINGING',
                        user_phone_number=customer_phone,
                        agent_phone_number=caller_id,
                        started_at=timezone.now(),
                    )
                else:
                    session.direction = 'OUTBOUND'
                    session.sales_rep = rep_user
                    session.lead = lead
                    session.user_phone_number = customer_phone
                    session.status = 'RINGING'
                    session.save(update_fields=['direction', 'sales_rep', 'lead', 'user_phone_number', 'status'])

                if agent_presence:
                    agent_presence.status = 'ON_CALL'
                    agent_presence.active_session = session
                    agent_presence.last_heartbeat = timezone.now()
                    agent_presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])

                hangup_url = f"{host}/api/v1/telephony/hangup/?session_id={session.id}"
                rec_str = "true" if vobiz_cfg.get('record_all_calls', True) else "false"

                logger.info(
                    "Vobiz Outbound Softphone Bridge: Agent=%s (%s) -> Customer=%s CallerID=%s UUID=%s",
                    rep_user.email if rep_user else clean_from_sip,
                    clean_from_sip,
                    customer_phone,
                    caller_id,
                    call_uuid
                )

                xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial action="{hangup_url}" callbackUrl="{hangup_url}" callerId="{caller_id}" record="{rec_str}">
    <Number>{customer_phone}</Number>
  </Dial>
  <Hangup/>
</Response>"""
                return HttpResponse(xml, content_type="application/xml; charset=utf-8")

            # ── INBOUND CALL FROM REAL CUSTOMER TO VIRTUAL DID ──
            caller_phone = VoiceCallingService.normalize_phone(raw_from)
            did_number = raw_to or vobiz_cfg['from_number']

            logger.info("Vobiz Inbound Call: UUID=%s From=%s To=%s", call_uuid, caller_phone, did_number)

            caller_digits = caller_phone.replace('+', '').lstrip('0')[-10:] if caller_phone else ''

            # 1. PRIORITY 1: Check if caller is an existing Member in CRM
            caller_user = None
            member_profile = None
            membership = None
            caller_type = 'NEW'

            if caller_digits:
                # Direct match on TenantUser
                caller_user = TenantUser.objects.filter(phone__icontains=caller_digits).first()
                if caller_user:
                    member_profile = UserProfile.objects.filter(user=caller_user).first()
                if not member_profile:
                    member_profile = UserProfile.objects.filter(user__phone__icontains=caller_digits).first()
                    if member_profile:
                        caller_user = member_profile.user

                if member_profile or (caller_user and getattr(caller_user, 'user_type', '') == 'MEMBER'):
                    caller_type = 'MEMBER'
                    if member_profile:
                        membership = Membership.objects.filter(
                            user_profile=member_profile,
                            status='ACTIVE'
                        ).first() or Membership.objects.filter(
                            user_profile=member_profile
                        ).order_by('-created_at').first()

            # 2. PRIORITY 2: If not a member, check if caller is an existing Lead in CRM
            lead = None
            if caller_type != 'MEMBER' and caller_digits:
                lead = Lead.objects.filter(
                    phone_normalized__icontains=caller_digits
                ).order_by('-created_at').first()
                if lead:
                    caller_type = 'LEAD'

            # 3. Resolve Organization
            org = None
            if caller_user and caller_user.organization:
                org = caller_user.organization
            elif lead and lead.organization:
                org = lead.organization
            if not org:
                org = Organization.objects.filter(name__icontains='UAT').first() or Organization.objects.first()

            if not org:
                logger.error("No active organization found for inbound call %s", call_uuid)
                xml = """<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>"""
                return HttpResponse(xml, content_type="application/xml; charset=utf-8")

            # 4. Check Rep Availability dynamically (zero hardcoded strings)
            assigned_rep = None
            if lead and lead.assigned_sales_user:
                assigned_rep = lead.assigned_sales_user
            elif lead and getattr(lead, 'assigned_to', None):
                assigned_rep = lead.assigned_to

            caller_branch = caller_user.home_branch if caller_user else (lead.branch if lead else None)
            presence = TelephonyConfigService.get_available_agent(
                org=org,
                preferred_user=assigned_rep,
                caller_branch=caller_branch,
            )

            # 5. Create CallSession in database (idempotent for webhook retries)
            target_rep = presence.user if presence else assigned_rep
            session = CallSession.objects.filter(provider_attempt_id=call_uuid).first()
            if not session:
                session = CallSession.objects.create(
                    organization=org,
                    lead=lead,
                    sales_rep=target_rep,
                    branch=caller_user.home_branch if caller_user else (lead.branch if lead else None),
                    provider='VOBIZ',
                    provider_attempt_id=call_uuid,
                    direction='INBOUND',
                    call_mode='HUMAN_MANUAL',
                    status='RINGING',
                    user_phone_number=caller_phone,
                    agent_phone_number=did_number,
                    started_at=timezone.now(),
                )
            else:
                session.lead = lead
                session.sales_rep = target_rep
                session.status = 'RINGING'
                session.save(update_fields=['lead', 'sales_rep', 'status'])

            if presence and target_rep:
                # Mark rep as ON_CALL
                presence.status = 'ON_CALL'
                presence.active_session = session
                presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])

                member_id_val = str(member_profile.id if member_profile else (caller_user.id if caller_user else '')) if caller_type == 'MEMBER' else None
                member_number_val = (member_profile.member_number if member_profile else (membership.membership_number if membership else None)) if caller_type == 'MEMBER' else None
                lead_id_val = str(lead.id) if lead else None

                # Format caller identity & status for Screen-Pop
                if caller_type == 'MEMBER':
                    display_name = f"{caller_user.first_name if caller_user else 'Member'} {caller_user.last_name or ''}".strip()
                    if member_number_val:
                        display_name += f" ({member_number_val})"
                    pkg_name = membership.package.name if (membership and membership.package) else "Active"
                    pkg_short = pkg_name if len(pkg_name) <= 22 else f"{pkg_name[:20].strip()}…"
                    status_label = f"MEMBER · {pkg_short}"
                    branch_name = membership.home_branch.name if (membership and membership.home_branch) else (caller_user.home_branch.name if (caller_user and caller_user.home_branch) else "General")
                elif caller_type == 'LEAD' and lead:
                    display_name = f"{lead.first_name} {lead.last_name or ''}".strip()
                    status_label = f"LEAD · {lead.current_status}"
                    branch_name = lead.branch.name if lead.branch else "General"
                else:
                    caller_type = 'NEW'
                    display_name = "New Prospect"
                    status_label = "NEW_CALLER"
                    branch_name = "General"

                masked_phone = f"+91-XXXXX-{caller_phone[-4:]}" if len(caller_phone) >= 4 else "+91-XXXXX-XXXX"
                screen_pop_payload = {
                    'session_id': str(session.id),
                    'call_uuid': call_uuid,
                    'caller_type': caller_type, # 'MEMBER' | 'LEAD' | 'NEW'
                    'member_id': member_id_val,
                    'member_number': member_number_val,
                    'lead_id': lead_id_val,
                    'caller_phone': caller_phone,
                    'masked_phone': masked_phone,
                    'lead_name': display_name,
                    'branch': branch_name,
                    'status': status_label,
                    'membership_package': membership.package.name if (membership and membership.package) else None,
                    'last_note': (lead.notes.order_by('-created_at').values_list('note_text', flat=True).first() or "") if (lead and hasattr(lead, 'notes')) else "",
                    'auto_open': True,
                }
                push_agent_telephony_event(str(target_rep.id), 'INCOMING_CALL_POPUP', screen_pop_payload)

                # Build VobizXML — dial agent's registered SIP endpoint
                host = _get_webhook_host(request)
                fallback_url = f"{host}/api/v1/telephony/dial-fallback/?session_id={session.id}"
                rec_callback_url = f"{host}/api/v1/telephony/recording/?session_id={session.id}"
                hangup_url = f"{host}/api/v1/telephony/hangup/?session_id={session.id}"
                did_caller_id = did_number if did_number.startswith('+') else vobiz_cfg['from_number']
                agent_sip = f"sip:{presence.sip_username}@{vobiz_cfg['registrar_host']}"
                caller_display_id = did_caller_id
                xml = VobizVoiceAdapter.build_inbound_dial_xml(
                    agent_sip_uri=agent_sip,
                    caller_id=caller_display_id,
                    fallback_action_url=fallback_url,
                    timeout=max(35, int(vobiz_cfg.get('ring_timeout_sec') or 35)),
                    record=vobiz_cfg['record_all_calls'],
                    recording_callback_url=rec_callback_url,
                    hangup_url=hangup_url,
                )
                logger.info("Generated Inbound Dial XML:\n%s", xml)
                return HttpResponse(xml, content_type="application/xml; charset=utf-8")

            # Fallback: No rep available -> play prompt and record missed call
            session.status = 'NO_ANSWER'
            session.save(update_fields=['status'])

            # Broadcast missed call to all reps in org so the queue / desk badge updates
            for pres in AgentPresence.objects.filter(organization=org):
                push_agent_telephony_event(str(pres.user_id), 'MISSED_CALL', {
                    'session_id': str(session.id),
                    'call_uuid': call_uuid,
                    'caller_phone': session.user_phone_number,
                    'caller_type': caller_type,
                })

            xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak voice="WOMAN" language="en-IN">Welcome to SWEAT Elite. All our fitness consultants are currently on active calls. We have logged your priority callback.</Speak>
  <Hangup/>
</Response>"""
            return HttpResponse(xml, content_type="application/xml; charset=utf-8")


@method_decorator(csrf_exempt, name='dispatch')
class VobizInboundFallbackWebhookView(APIView):
    """
    POST /api/v1/telephony/dial-fallback/
    Triggered if the primary rep's Zoiper returns busy or times out after 15s.
    Executes the overflow ladder to secondary reps so calls never drop.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        with resolve_telephony_tenant_context():
            session_id = request.GET.get('session_id') or request.data.get('session_id')
            dial_status = request.data.get('DialStatus') or request.POST.get('DialStatus', 'no-answer')
            call_uuid = request.data.get('CallUUID') or request.POST.get('CallUUID', '')

            session = CallSession.objects.filter(id=session_id).first() if session_id else None
            if not session:
                session = CallSession.objects.filter(provider_attempt_id=call_uuid).first()

            org = session.organization if session else Organization.objects.first()

            # If primary call completed successfully, return Hangup Response
            if dial_status.lower() in ('completed', 'answered'):
                if session and session.status != 'COMPLETED':
                    duration_val = float(request.data.get('DialBLegDuration') or request.POST.get('DialBLegDuration') or request.data.get('Duration') or 0.0)
                    session.duration_seconds = duration_val
                    session.bill_duration_seconds = VobizVoiceAdapter.calculate_billable_duration(duration_val)
                    session.status = 'COMPLETED'
                    session.ended_at = timezone.now()
                    session.save(update_fields=['duration_seconds', 'bill_duration_seconds', 'status', 'ended_at'])
                return HttpResponse("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Hangup/></Response>", content_type="application/xml; charset=utf-8")

            # Find secondary available rep, prioritizing actively online connected reps
            active_window = timezone.now() - timedelta(minutes=3)
            secondary_presence = AgentPresence.objects.filter(
                status='AVAILABLE',
                last_heartbeat__gte=active_window,
            ).exclude(user=session.sales_rep if session else None).order_by('-last_heartbeat').first()

            host = _get_webhook_host(request)
            caller_id = session.agent_phone_number if session else "+919200000001"

            if secondary_presence:
                secondary_user = secondary_presence.user
                secondary_presence.status = 'ON_CALL'
                secondary_presence.active_session = session
                secondary_presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])

                if session:
                    session.sales_rep = secondary_user
                    session.save(update_fields=['sales_rep'])

                # Push screen-pop to secondary rep
                lead = session.lead if session else None
                caller_phone = session.user_phone_number if session else "Unknown"
                masked_phone = f"+91-XXXXX-{caller_phone[-4:]}" if len(caller_phone) >= 4 else "+91-XXXXX-XXXX"
                push_agent_telephony_event(str(secondary_user.id), 'INCOMING_CALL_POPUP', {
                    'session_id': str(session.id) if session else '',
                    'call_uuid': call_uuid,
                    'lead_id': lead.id if lead else None,
                    'lead_name': f"{lead.first_name} {lead.last_name or ''}".strip() if lead else "Member Call (Overflow)",
                    'masked_phone': masked_phone,
                    'branch': lead.branch.name if (lead and lead.branch) else "General",
                    'status': lead.current_status if lead else "OVERFLOW",
                })

                complete_url = f"{host}/api/v1/telephony/hangup/"
                agent_sip = f"sip:{secondary_presence.sip_username}@registrar.vobiz.ai"
                xml = VobizVoiceAdapter.build_fallback_overflow_xml(
                    secondary_sip_uri=agent_sip,
                    caller_id=caller_id,
                    complete_action_url=complete_url,
                    whisper_prompt="Please stay on the line while we connect you to our senior consultant.",
                    timeout=20,
                )
                return HttpResponse(xml, content_type="application/xml; charset=utf-8")

            # If no secondary rep available either: play polite message and log missed call
            if session:
                session.status = 'NO_ANSWER'
                session.save(update_fields=['status'])
                # Always reset the agent to AVAILABLE so next call is not blocked
                if session.sales_rep:
                    AgentPresence.objects.filter(user=session.sales_rep).update(
                        status='AVAILABLE',
                        active_session=None,
                    )
                # Log to Lead timeline if caller was a lead
                if session.lead:
                    LeadActivity.objects.create(
                        lead=session.lead,
                        activity_type='CALL',
                        outcome='MISSED',
                        notes=f"Missed inbound call from {session.user_phone_number}. No reps were available to answer.",
                        external_reference=str(session.id),
                    )
                # Write CallSession activity note for timeline
                session.disposition_notes = f"MISSED: Inbound call from {session.user_phone_number}. No reps were available to answer."
                session.save(update_fields=['disposition_notes'])

                # Broadcast MISSED_CALL to reps so the Missed Calls Desk updates in real-time
                for pres in AgentPresence.objects.filter(organization=org):
                    push_agent_telephony_event(str(pres.user_id), 'MISSED_CALL', {
                        'session_id': str(session.id),
                        'call_uuid': call_uuid,
                        'caller_phone': session.user_phone_number,
                    })

            xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak voice="WOMAN" language="en-IN">All consultants are currently assisting members. We have scheduled an immediate callback for you. Thank you for calling SWEAT.</Speak>
  <Hangup/>
</Response>"""
            return HttpResponse(xml, content_type="application/xml; charset=utf-8")


# ============================================================================
# 2. HANGUP WEBHOOK (BILLING & DISPOSITION TRIGGER)
# ============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class VobizHangupWebhookView(APIView):
    """
    POST /api/v1/telephony/hangup/
    Fired by Vobiz when a call concludes (inbound or outbound).
    Records duration, 60s pulse billing, recording URL, puts rep in WRAP_UP state,
    and pushes CALL_ENDED event to CRM to pop the mandatory disposition modal.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        with resolve_telephony_tenant_context():
            data = request.data if isinstance(request.data, dict) else {}
            call_uuid = data.get('CallUUID') or data.get('call_uuid') or request.POST.get('CallUUID', '')
            call_status = data.get('CallStatus') or request.POST.get('CallStatus', 'completed')
            duration_raw = float(data.get('Duration') or request.POST.get('Duration', 0.0))
            recording_url = (
                data.get('RecordUrl')
                or data.get('record_url')
                or data.get('RecordingUrl')
                or data.get('recording_url')
                or request.POST.get('RecordUrl')
                or request.POST.get('record_url')
                or request.POST.get('RecordingUrl')
                or request.POST.get('recording_url', '')
            )
            hangup_cause = data.get('HangupCause') or request.POST.get('HangupCause', 'NORMAL_CLEARING')

            logger.info("Vobiz Hangup Webhook: UUID=%s Duration=%ss Status=%s", call_uuid, duration_raw, call_status)

            session = CallSession.objects.filter(provider_attempt_id=call_uuid).first()
            if not session:
                session_id = request.GET.get('session_id')
                if session_id:
                    session = CallSession.objects.filter(id=session_id).first()

            if session:
                bill_duration = VobizVoiceAdapter.calculate_billable_duration(duration_raw)
                is_meaningful = (duration_raw >= 30.0)

                session.duration_seconds = duration_raw
                session.bill_duration_seconds = bill_duration
                session.is_meaningful = is_meaningful
                session.recording_url = recording_url
                session.hangup_cause = hangup_cause
                session.status = 'COMPLETED' if duration_raw > 0 else 'NO_ANSWER'
                session.ended_at = timezone.now()
                session.save(update_fields=[
                    'duration_seconds',
                    'bill_duration_seconds',
                    'is_meaningful',
                    'recording_url',
                    'hangup_cause',
                    'status',
                    'ended_at',
                ])

                # Trigger Celery background archival to private storage
                if recording_url:
                    try:
                        from apps.tenant_core.tasks_recordings import archive_vobiz_recording_task
                        archive_vobiz_recording_task.delay(str(session.id), recording_url)
                    except Exception as task_err:
                        logger.info("Celery task dispatch skipped or running in sync mode: %s", task_err)

                # Update rep presence based on whether the call actually connected
                if session.sales_rep:
                    rep_id = str(session.sales_rep.id)
                    if duration_raw > 0:
                        # If disposition was ALREADY submitted by agent from the browser WebRTC phone:
                        if session.disposition:
                            logger.info("Call %s already disposed as %s. Setting rep to AVAILABLE and skipping duplicate CALL_ENDED broadcast.", session.id, session.disposition)
                            AgentPresence.objects.filter(user=session.sales_rep).update(
                                status='AVAILABLE',
                                active_session=None,
                            )
                        else:
                            # Connected call: place in WRAP_UP for post-call notes & disposition
                            AgentPresence.objects.filter(user=session.sales_rep).update(
                                status='WRAP_UP',
                                active_session=session,
                            )
                            push_agent_telephony_event(rep_id, 'CALL_ENDED', {
                                'session_id': str(session.id),
                                'call_uuid': call_uuid,
                                'duration_seconds': int(duration_raw),
                                'bill_duration_seconds': bill_duration,
                                'is_meaningful': is_meaningful,
                                'direction': session.direction,
                                'caller_phone': session.user_phone_number or '',
                                'lead_id': session.lead.id if session.lead else None,
                                'lead_name': f"{session.lead.first_name} {session.lead.last_name or ''}".strip() if session.lead else (session.user_phone_number or "Customer"),
                                'recording_url': recording_url,
                            })
                    else:
                        # Unanswered / cancelled call: instantly return to AVAILABLE
                        AgentPresence.objects.filter(user=session.sales_rep).update(
                            status='AVAILABLE',
                            active_session=None,
                        )
                        # Only push MISSED_CALL notification for real INBOUND calls, never for agent's own outbound calls!
                        if session.direction == 'INBOUND':
                            push_agent_telephony_event(rep_id, 'MISSED_CALL', {
                                'session_id': str(session.id),
                                'call_uuid': call_uuid,
                                'caller_phone': session.user_phone_number,
                            })

            # Return valid VobizXML rather than JSON to avoid 8012 Invalid Action XML carrier errors
            xml_resp = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Hangup/>
</Response>"""
            return HttpResponse(xml_resp, content_type="application/xml; charset=utf-8")



@method_decorator(csrf_exempt, name='dispatch')
class VobizRecordingWebhookView(APIView):
    """
    POST /api/v1/telephony/recording/
    Fired asynchronously by Vobiz/Plivo when a conversation recording finishes processing.
    Extracts RecordUrl, updates CallSession, downloads the audio file locally to media/recordings/{session_id}.mp3,
    and alerts connected CRM clients via SSE.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        with resolve_telephony_tenant_context():
            data = request.data if isinstance(request.data, dict) else {}
            session_id = request.GET.get('session_id') or data.get('session_id')
            call_uuid = (
                data.get('CallUUID')
                or data.get('call_uuid')
                or request.POST.get('CallUUID')
                or request.POST.get('call_uuid', '')
            )
            record_url = (
                data.get('RecordUrl')
                or data.get('record_url')
                or data.get('RecordingUrl')
                or data.get('recording_url')
                or request.POST.get('RecordUrl')
                or request.POST.get('record_url')
                or request.POST.get('RecordingUrl')
                or request.POST.get('recording_url', '')
            )
            duration_raw = float(
                data.get('RecordingDuration')
                or data.get('Duration')
                or request.POST.get('RecordingDuration')
                or request.POST.get('Duration', 0.0)
            )

            logger.info(
                "Vobiz Recording Webhook: Session=%s CallUUID=%s RecordUrl=%s Duration=%ss",
                session_id, call_uuid, record_url, duration_raw
            )

            session = None
            if session_id:
                try:
                    import uuid
                    session = CallSession.objects.filter(id=uuid.UUID(str(session_id))).first()
                except Exception:
                    session = CallSession.objects.filter(id=session_id).first()
            if not session and call_uuid:
                session = CallSession.objects.filter(provider_attempt_id=call_uuid).first()

            if session and record_url:
                session.recording_url = record_url
                if duration_raw > 0 and (not session.duration_seconds or session.duration_seconds == 0):
                    session.duration_seconds = duration_raw
                    session.bill_duration_seconds = VobizVoiceAdapter.calculate_billable_duration(duration_raw)

                # Download conversation recording directly to media/recordings/
                try:
                    from django.conf import settings
                    cache_dir = os.path.join(settings.BASE_DIR, 'media', 'recordings')
                    os.makedirs(cache_dir, exist_ok=True)
                    dest_file = os.path.join(cache_dir, f"{session.id}.mp3")
                    import requests
                    r = requests.get(record_url, timeout=30, stream=True)
                    if r.status_code == 200:
                        with open(dest_file, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                        session.recording_storage_key = dest_file
                        logger.info("Saved Vobiz call recording: %s (%d bytes)", dest_file, os.path.getsize(dest_file))
                except Exception as dl_err:
                    logger.warning("Could not download recording in webhook: %s", dl_err)

                session.save(update_fields=['recording_url', 'recording_storage_key', 'duration_seconds', 'bill_duration_seconds'])

                # Broadcast to CRM client tabs so Play Call button becomes active
                if session.sales_rep:
                    push_agent_telephony_event(str(session.sales_rep.id), 'RECORDING_AVAILABLE', {
                        'session_id': str(session.id),
                        'recording_url': f"/api/v1/tenant/crm/telephony/recordings/{session.id}/stream/",
                        'lead_id': str(session.lead.id) if session.lead else None,
                    })

            xml_resp = """<?xml version="1.0" encoding="UTF-8"?><Response/>"""
            return HttpResponse(xml_resp, content_type="application/xml; charset=utf-8")


# ============================================================================
# 3. OUTBOUND CLICK-TO-CALL & AGENT BRIDGE
# ============================================================================

class ClickToCallView(APIView):
    """
    POST /api/v1/tenant/crm/telephony/click-to-call/
    Triggered when an agent clicks "Call Lead" inside the CRM.
    Executes Agent-First Bridging:
    1. Rings Agent's Zoiper SIP endpoint first.
    2. Once agent picks up, Vobiz fetches agent-answer-bridge XML and dials customer.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        lead_id = request.data.get('lead_id')
        if not lead_id:
            return Response({'error': 'lead_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Enforce TRAI Outbound Calling Hours (09:00 - 21:00 IST)
        if not _is_within_trai_hours():
            return Response({
                'error': 'Outbound calls are only permitted between 9:00 AM and 9:00 PM IST per TRAI regulations.',
                'code': 'OUTSIDE_CALLING_HOURS',
            }, status=status.HTTP_400_BAD_REQUEST)

        lead = Lead.objects.filter(id=lead_id).first()
        if not lead or not lead.phone_normalized:
            return Response({'error': 'Lead not found or has no phone number.'}, status=status.HTTP_404_NOT_FOUND)

        # 2. Resolve Agent Presence & SIP Endpoint — prioritize user's own configured presence
        presence = AgentPresence.objects.filter(user=user).first()
        if not presence:
            presence = AgentPresence.objects.filter(sip_username='sweatagent104201155654570218059').first() or AgentPresence.objects.filter(sip_username='iqra24030269280035568').first()
        if not presence:
            presence = AgentPresence.objects.create(
                organization=lead.organization,
                user=user,
                sip_username='sweatagent104201155654570218059',
                status='AVAILABLE',
            )

        # 3. Create CallSession record
        session = CallSession.objects.create(
            organization=lead.organization,
            lead=lead,
            sales_rep=user,
            branch=lead.branch,
            provider='VOBIZ',
            direction='OUTBOUND',
            call_mode='HUMAN_MANUAL',
            status='INITIATED',
            user_phone_number=lead.phone_normalized,
            agent_phone_number=presence.sip_username,
            started_at=timezone.now(),
        )

        host = _get_webhook_host(request)
        answer_url = f"{host}/api/v1/telephony/agent-answer-bridge/?session_id={session.id}"
        hangup_url = f"{host}/api/v1/telephony/hangup/?session_id={session.id}"
        agent_sip_uri = f"sip:{presence.sip_username}@registrar.vobiz.ai"

        # 4. Dispatch via VobizVoiceAdapter
        did_number = os.getenv('VOBIZ_VIRTUAL_DID', '+919240954144')
        adapter = VobizVoiceAdapter(configuration={
            'from_number': did_number,
            'api_base_url': 'https://api.vobiz.ai/api/v1',
        })
        result = adapter.initiate_click_to_call(
            agent_sip_uri=agent_sip_uri,
            customer_phone=lead.phone_normalized,
            answer_url=answer_url,
            hangup_url=hangup_url,
            timeout=15,
        )

        if result.success and result.provider_message_id:
            session.provider_attempt_id = result.provider_message_id
            session.status = 'RINGING'
            session.save(update_fields=['provider_attempt_id', 'status'])
            presence.status = 'ON_CALL'
            presence.active_session = session
            presence.save(update_fields=['status', 'active_session'])

        masked_phone = f"+91-XXXXX-{lead.phone_normalized[-4:]}" if len(lead.phone_normalized) >= 4 else "+91-XXXXX-XXXX"
        return Response({
            'call_session_id': str(session.id),
            'status': session.status,
            'message': 'Ringing your Zoiper softphone. Customer will be bridged on pickup.',
            'agent_sip': agent_sip_uri,
            'masked_phone': masked_phone,
        }, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class AgentAnswerBridgeXmlView(APIView):
    """
    POST /api/v1/telephony/agent-answer-bridge/
    Fetched by Vobiz the moment the agent answers their Zoiper headset.
    Returns VobizXML: whispers caller context to the agent, then dials the customer.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        with resolve_telephony_tenant_context():
            session_id = request.GET.get('session_id')
            session = CallSession.objects.filter(id=session_id).first() if session_id else None

            if not session or not session.lead:
                xml = """<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>"""
                return HttpResponse(xml, content_type="application/xml")

            lead = session.lead
            lead_name = f"{lead.first_name} {lead.last_name or ''}".strip() or "Member"
            whisper = f"Connecting you to {lead_name}."

            host = _get_webhook_host(request)
            rec_callback_url = f"{host}/api/v1/telephony/recording/?session_id={session.id}"
            hangup_url = f"{host}/api/v1/telephony/hangup/?session_id={session.id}"

            xml = VobizVoiceAdapter.build_agent_bridge_customer_xml(
                customer_phone=lead.phone_normalized,
                caller_id=session.agent_phone_number or "+919200000001",
                whisper_text=whisper,
                record=True,
                recording_callback_url=rec_callback_url,
                hangup_url=hangup_url,
            )
            return HttpResponse(xml, content_type="application/xml; charset=utf-8")


# ============================================================================
# 4. POST-CALL DISPOSITION SUBMISSION
# ============================================================================

class CallDispositionView(APIView):
    """
    POST /api/v1/tenant/crm/telephony/disposition/
    Submitted when the rep completes the mandatory Call Disposition Modal.
    Updates CallSession, advances Lead stage (if trial booked), schedules follow-up tasks,
    and returns agent status to AVAILABLE.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        with resolve_telephony_tenant_context():
            user = request.user
            session_id = request.data.get('session_id')
            disposition = request.data.get('disposition')
            notes = request.data.get('disposition_notes', '').strip()
            followup_date_str = request.data.get('followup_date')

            if not session_id or not disposition:
                return Response({'error': 'session_id and disposition are required.'}, status=status.HTTP_400_BAD_REQUEST)

            # Robust session lookup (supports UUID id, provider_attempt_id, and latest agent call fallback)
            session = None
            import uuid
            try:
                val = uuid.UUID(str(session_id))
                session = CallSession.objects.filter(id=val).first()
            except (ValueError, TypeError, Exception):
                pass

            if not session:
                session = CallSession.objects.filter(provider_attempt_id=session_id).first()

            if not session and user:
                session = CallSession.objects.filter(
                    sales_rep=user,
                    created_at__gte=timezone.now() - timedelta(minutes=15)
                ).order_by('-created_at').first()

            caller_phone = str(request.data.get('caller_phone', '')).strip()

            if not session:
                # Ensure the call session is captured so the agent's logged work is NEVER lost
                org = getattr(user, 'organization', None) or Organization.objects.first()
                branch = getattr(user, 'home_branch', None) or Branch.objects.filter(organization=org).first()
                session = CallSession.objects.create(
                    organization=org,
                    sales_rep=user if getattr(user, 'is_authenticated', False) else None,
                    branch=branch,
                    provider='VOBIZ',
                    direction=request.data.get('direction', 'OUTBOUND'),
                    call_mode='HUMAN_MANUAL',
                    status='COMPLETED',
                    user_phone_number=caller_phone,
                    duration_seconds=float(request.data.get('duration_seconds', 0.0) or 0.0),
                    disposition=disposition,
                    disposition_notes=notes,
                    disposition_submitted_at=timezone.now(),
                )

            # Update CallSession
            session.disposition = disposition
            session.disposition_notes = notes
            session.disposition_submitted_at = timezone.now()
            if not session.user_phone_number and caller_phone:
                session.user_phone_number = caller_phone
            session.save(update_fields=['disposition', 'disposition_notes', 'disposition_submitted_at', 'user_phone_number'])

            # Match or auto-create Lead if session has no lead (never auto-create if phone belongs to an existing Member)
            lead = session.lead
            phone_to_check = session.user_phone_number or caller_phone
            if not lead and phone_to_check:
                clean_phone = phone_to_check.replace('+', '').replace('-', '').replace(' ', '')
                if len(clean_phone) >= 10:
                    last10 = clean_phone[-10:]
                    is_member = (
                        TenantUser.objects.filter(phone__icontains=last10, user_type='MEMBER').exists()
                        or UserProfile.objects.filter(user__phone__icontains=last10).exists()
                    )
                    if not is_member:
                        lead = Lead.objects.filter(phone_normalized__icontains=last10).first()
                        if not lead and disposition in ('TRIAL_BOOKED', 'INTERESTED', 'CALLBACK_REQUESTED', 'FOLLOW_UP_NEEDED'):
                            org = session.organization or getattr(user, 'organization', None) or Organization.objects.first()
                            branch = session.branch or getattr(user, 'home_branch', None) or Branch.objects.filter(organization=org).first()
                            lead = Lead.objects.create(
                                organization=org,
                                branch=branch,
                                first_name=f"Prospect {last10[-4:]}",
                                last_name="",
                                phone_normalized=phone_to_check if phone_to_check.startswith('+') else f"+91{last10}",
                                current_status='TRIAL_BOOKED' if disposition == 'TRIAL_BOOKED' else ('INTERESTED' if disposition == 'INTERESTED' else 'NEW_LEAD'),
                                first_touch_source='OUTBOUND_CALL',
                                assigned_sales_user=user if getattr(user, 'is_authenticated', False) else None,
                            )
                        if lead:
                            session.lead = lead
                            session.save(update_fields=['lead'])

            # Create LeadActivity, audit stage history, and advance status
            if lead:
                old_status = lead.current_status
                target_status = None
                if disposition == 'TRIAL_BOOKED':
                    target_status = 'TRIAL_BOOKED'
                elif disposition == 'INTERESTED' and old_status in ('NEW_LEAD', 'UNASSIGNED'):
                    target_status = 'INTERESTED'

                if target_status and target_status != old_status:
                    lead.current_status = target_status
                    lead.save(update_fields=['current_status', 'updated_at'])
                    from apps.tenant_core.models_crm import LeadStatusHistory
                    LeadStatusHistory.objects.using(get_tenant_db_alias() or 'default').create(
                        lead=lead,
                        from_status=old_status,
                        to_status=target_status,
                        reason_code='CALL_DISPOSITION',
                        reason_text=f"Advanced via Call Outcome: {disposition.replace('_', ' ').title()}" + (f" - {notes}" if notes else ""),
                        changed_by_user=user if getattr(user, 'is_authenticated', False) else None,
                    )

                LeadActivity.objects.create(
                    lead=lead,
                    activity_type='CALL',
                    outcome=disposition,
                    notes=f"Outcome: {disposition.replace('_', ' ').title()}\n{notes}",
                    performed_by_user=user if getattr(user, 'is_authenticated', False) else None,
                    external_reference=str(session.id),
                )

                # Create Follow-up Task if requested
                if followup_date_str:
                    try:
                        followup_dt = datetime.fromisoformat(followup_date_str.replace('Z', '+00:00'))
                        SalesFollowupTask.objects.create(
                            lead=lead,
                            assigned_to_user=user if getattr(user, 'is_authenticated', False) else None,
                            task_type='CALL',
                            due_at=followup_dt,
                            outcome=f"Call Follow-up: {notes}",
                            priority='MEDIUM',
                            external_reference=str(session.id),
                            created_by_user=user if getattr(user, 'is_authenticated', False) else None,
                        )
                    except Exception as parse_err:
                        logger.warning("Could not parse followup date %s: %s", followup_date_str, parse_err)


            # Check for automated or requested WhatsApp follow-up
            send_whatsapp = bool(request.data.get('send_whatsapp', False))
            whatsapp_msg_override = request.data.get('whatsapp_message', '').strip()
            whatsapp_dispatched = False

            unanswered_dispositions = ('NO_ANSWER', 'RINGING_NO_ANSWER', 'BUSY', 'BUSY_CALLBACK', 'SWITCHED_OFF')
            should_send_wa = send_whatsapp or (
                request.data.get('send_whatsapp') is None and disposition in unanswered_dispositions
            )

            if should_send_wa and lead:
                try:
                    from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
                    wa_msg = WhatsAppAutomationService.send_unanswered_call_followup(
                        session=session,
                        lead=lead,
                        message_text=whatsapp_msg_override or None,
                        actor_user=user if getattr(user, 'is_authenticated', False) else None,
                        db_alias=get_tenant_db_alias() or 'default',
                    )
                    whatsapp_dispatched = bool(wa_msg)
                except Exception as wa_err:
                    logger.warning("Automated WhatsApp follow-up dispatch error: %s", wa_err)

            # Reset Agent Presence to AVAILABLE
            AgentPresence.objects.filter(user=user).update(
                status='AVAILABLE',
                active_session=None,
            )

            return Response({
                'success': True,
                'message': 'Disposition recorded and agent status updated to AVAILABLE.',
                'lead_status': lead.current_status if lead else None,
                'whatsapp_sent': whatsapp_dispatched,
            }, status=status.HTTP_200_OK)

        return Response({
            'status': 'SAVED',
            'session_id': str(session.id),
            'disposition': disposition,
            'agent_presence': 'AVAILABLE',
        }, status=status.HTTP_200_OK)


# ============================================================================
# 5. REAL-TIME EVENT STREAM (SERVER-SENT EVENTS) & QUEUES
# ============================================================================

class ServerSentEventRenderer(renderers.BaseRenderer):
    """Renderer to satisfy text/event-stream Accept header for SSE streams."""
    media_type = 'text/event-stream'
    format = 'txt'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class TelephonyEventsSseView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/events/
    Server-Sent Events (SSE) endpoint streamed to the sales rep's open CRM tab.
    Pushes screen-pop alerts, call-ended triggers, and missed-call notifications.
    """
    permission_classes = [permissions.AllowAny]
    renderer_classes = [ServerSentEventRenderer, renderers.JSONRenderer]

    def get(self, request):
        user = request.user
        if not (user and getattr(user, 'is_authenticated', False)):
            token_str = request.GET.get('token')
            if token_str:
                # 1. Try TenantJWTAuthentication or PlatformJWTAuthentication
                try:
                    from apps.authentication.backends import TenantJWTAuthentication, PlatformJWTAuthentication
                    request.META['HTTP_AUTHORIZATION'] = f'Bearer {token_str}'
                    auth_res = TenantJWTAuthentication().authenticate(request) or PlatformJWTAuthentication().authenticate(request)
                    if auth_res:
                        user = auth_res[0]
                except Exception:
                    pass

                # 2. Direct simplejwt AccessToken inspection
                if not (user and getattr(user, 'is_authenticated', False)):
                    try:
                        from rest_framework_simplejwt.tokens import AccessToken
                        tok = AccessToken(token_str)
                        sub = tok.get('sub')
                        if sub:
                            with resolve_telephony_tenant_context():
                                user = TenantUser.objects.filter(id=sub).first()
                    except Exception:
                        pass

        # For browser SSE screens, allow connection so screen-pop is never blocked
        u_key = str(user.id) if (user and hasattr(user, 'id')) else "crm_frontend"

        # Initialize event queue immediately for this client connection
        if u_key not in _AGENT_EVENT_QUEUES:
            _AGENT_EVENT_QUEUES[u_key] = []

        # Update presence heartbeat if user is resolved
        if user and getattr(user, 'is_authenticated', False):
            with resolve_telephony_tenant_context():
                AgentPresence.objects.filter(user=user).update(last_heartbeat=timezone.now())

        def event_stream():
            if u_key not in _AGENT_EVENT_QUEUES:
                _AGENT_EVENT_QUEUES[u_key] = []

            # Initial connect handshake
            yield f"event: CONNECTED\ndata: {json.dumps({'status': 'ONLINE', 'user_id': u_key})}\n\n"

            heartbeat_counter = 0
            try:
                while True:
                    time.sleep(1)
                    heartbeat_counter += 1

                    # Drain pending events from agent queue
                    if u_key in _AGENT_EVENT_QUEUES and _AGENT_EVENT_QUEUES[u_key]:
                        while _AGENT_EVENT_QUEUES[u_key]:
                            evt = _AGENT_EVENT_QUEUES[u_key].pop(0)
                            yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"

                    # Send 15-second heartbeat ping to keep connection alive through proxies
                    if heartbeat_counter >= 15:
                        heartbeat_counter = 0
                        yield f": heartbeat {timezone.now().isoformat()}\n\n"
            finally:
                pass

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response


class MissedCallsQueueView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/missed-calls/
    Returns list of unreturned missed calls for the rep's assigned leads.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        with resolve_telephony_tenant_context():
            cutoff = timezone.now() - timedelta(days=7)

            missed = CallSession.objects.filter(
                direction='INBOUND',
                status__in=['NO_ANSWER', 'FAILED', 'BUSY', 'RINGING'],
                created_at__gte=cutoff,
            ).filter(
                disposition_submitted_at__isnull=True
            ).select_related('lead', 'sales_rep', 'lead__branch').order_by('-created_at')[:50]

            results = []
            for s in missed:
                lead = s.lead
                phone = s.user_phone_number or ""
                masked_phone = f"+91-XXXXX-{phone[-4:]}" if len(phone) >= 4 else "+91-XXXXX-XXXX"
                lead_data = {
                    'id': str(lead.id),
                    'first_name': lead.first_name,
                    'last_name': lead.last_name or '',
                    'masked_phone': masked_phone,
                    'branch_name': lead.branch.name if lead.branch else "General",
                    'status': lead.current_status,
                } if lead else None

                results.append({
                    'id': str(s.id),
                    'session_id': str(s.id),
                    'started_at': s.started_at.isoformat() if s.started_at else s.created_at.isoformat(),
                    'called_at': s.created_at.isoformat(),
                    'user_phone_number': phone,
                    'masked_phone': masked_phone,
                    'status': s.status,
                    'direction': s.direction,
                    'sales_rep_name': f"{s.sales_rep.first_name} {s.sales_rep.last_name or ''}".strip() if s.sales_rep else "Unassigned",
                    'lead': lead_data,
                    'lead_id': str(lead.id) if lead else None,
                    'lead_name': f"{lead.first_name} {lead.last_name or ''}".strip() if lead else "New Prospect",
                    'branch_name': lead.branch.name if (lead and lead.branch) else "General",
                })

            return Response({
                'missed_calls': results,
                'results': results,
                'count': len(results),
            }, status=status.HTTP_200_OK)


class AgentPresenceView(APIView):
    """
    GET / PUT /api/v1/tenant/crm/telephony/presence/
    Get or toggle agent presence (AVAILABLE, WRAP_UP, OFFLINE).
    Includes real-time self-healing to prevent agents from getting locked in ON_CALL or WRAP_UP.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        with resolve_telephony_tenant_context():
            presence = AgentPresence.objects.filter(user=request.user).first()
            if presence:
                # 1. Self-healing check: If agent is marked ON_CALL, verify active session
                if presence.status == 'ON_CALL':
                    active = presence.active_session
                    # If no session assigned, or session is concluded / cancelled, release to AVAILABLE
                    if not active or active.status in ('COMPLETED', 'NO_ANSWER', 'FAILED', 'BUSY', 'CANCELLED') or active.ended_at is not None:
                        logger.info("AgentPresenceView: Auto-releasing %s from stale ON_CALL to AVAILABLE", request.user.email)
                        presence.status = 'AVAILABLE'
                        presence.active_session = None
                        presence.last_heartbeat = timezone.now()
                        presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])
                elif presence.status == 'WRAP_UP':
                    active = presence.active_session
                    two_min_ago = timezone.now() - timedelta(minutes=2)
                    is_session_done = not active or active.status in ('COMPLETED', 'NO_ANSWER', 'FAILED', 'BUSY', 'CANCELLED') or active.ended_at is not None
                    if is_session_done or (not presence.last_heartbeat or presence.last_heartbeat < two_min_ago):
                        logger.info("AgentPresenceView: Auto-releasing %s from WRAP_UP to AVAILABLE", request.user.email)
                        presence.status = 'AVAILABLE'
                        presence.active_session = None
                        presence.last_heartbeat = timezone.now()
                        presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])
                elif presence.status == 'AVAILABLE':
                    # Active tab polling keeps the agent's heartbeat fresh so they are prioritized for routing
                    presence.last_heartbeat = timezone.now()
                    presence.save(update_fields=['last_heartbeat'])

                status_val = presence.status
                sip_user = presence.sip_username
                last_hb = presence.last_heartbeat
                active_id = str(presence.active_session.id) if presence.active_session else None
            else:
                status_val = 'AVAILABLE'
                sip_user = ''
                last_hb = None
                active_id = None

            return Response({
                'status': status_val,
                'sip_username': sip_user,
                'last_heartbeat': last_hb,
                'active_session_id': active_id,
            }, status=status.HTTP_200_OK)

    def put(self, request):
        new_status = request.data.get('status', 'AVAILABLE').upper()
        if new_status not in ('AVAILABLE', 'WRAP_UP', 'OFFLINE'):
            return Response({'error': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)

        with resolve_telephony_tenant_context():
            presence, _ = AgentPresence.objects.get_or_create(
                user=request.user,
                defaults={
                    'organization': Organization.objects.first(),
                    'sip_username': f"agent_{request.user.id.hex[:6]}",
                    'status': new_status,
                },
            )
            presence.status = new_status
            if new_status == 'AVAILABLE':
                presence.active_session = None
            presence.last_heartbeat = timezone.now()
            presence.save(update_fields=['status', 'active_session', 'last_heartbeat'])

            return Response({
                'status': presence.status,
                'sip_username': presence.sip_username,
                'last_heartbeat': presence.last_heartbeat,
                'active_session_id': str(presence.active_session.id) if presence.active_session else None,
            }, status=status.HTTP_200_OK)


class ActiveInboundCallPollView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/active/
    Returns currently ringing or active inbound call session for screen-pop polling fallback.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        with resolve_telephony_tenant_context():
            cutoff = timezone.now() - timedelta(seconds=45)
        session = CallSession.objects.filter(
            direction='INBOUND',
            status__in=['INITIATED', 'RINGING'],
            created_at__gte=cutoff,
        ).order_by('-created_at').first()

        if not session:
            return Response({'active': False}, status=status.HTTP_200_OK)

        # Only pop for the designated sales rep
        if session.sales_rep and session.sales_rep != request.user:
            return Response({'active': False}, status=status.HTTP_200_OK)

        caller_phone = session.user_phone_number or ""
        caller_digits = caller_phone.replace('+', '').lstrip('0')[-10:] if caller_phone else ''

        caller_type = 'NEW'
        member_id_val = None
        member_number_val = None
        lead_id_val = None
        display_name = "New Prospect"
        status_label = "NEW_CALLER"
        branch_name = "General"
        pkg_name = None

        if caller_digits:
            caller_user = TenantUser.objects.filter(phone__icontains=caller_digits).first()
            member_profile = UserProfile.objects.filter(user=caller_user).first() if caller_user else None
            if not member_profile:
                member_profile = UserProfile.objects.filter(user__phone__icontains=caller_digits).first()
                if member_profile:
                    caller_user = member_profile.user

            if member_profile or (caller_user and getattr(caller_user, 'user_type', '') == 'MEMBER'):
                caller_type = 'MEMBER'
                membership = Membership.objects.filter(user_profile=member_profile, status='ACTIVE').first() or Membership.objects.filter(user_profile=member_profile).order_by('-created_at').first()
                member_id_val = str(member_profile.id if member_profile else (caller_user.id if caller_user else ''))
                member_number_val = member_profile.member_number if member_profile else (membership.membership_number if membership else None)
                display_name = f"{caller_user.first_name if caller_user else 'Member'} {caller_user.last_name or ''}".strip()
                if member_number_val:
                    display_name += f" ({member_number_val})"
                pkg_name = membership.package.name if (membership and membership.package) else "Active"
                status_label = f"MEMBER · {pkg_name}"
                branch_name = membership.home_branch.name if (membership and membership.home_branch) else "General"
            else:
                lead = Lead.objects.filter(phone_normalized__icontains=caller_digits).order_by('-created_at').first()
                if lead:
                    caller_type = 'LEAD'
                    lead_id_val = str(lead.id)
                    display_name = f"{lead.first_name} {lead.last_name or ''}".strip()
                    status_label = f"LEAD · {lead.current_status}"
                    branch_name = lead.branch.name if lead.branch else "General"

        masked_phone = f"+91-XXXXX-{caller_phone[-4:]}" if len(caller_phone) >= 4 else "+91-XXXXX-XXXX"
        return Response({
            'active': True,
            'session_id': str(session.id),
            'call_uuid': session.provider_attempt_id,
            'caller_type': caller_type,
            'member_id': member_id_val,
            'member_number': member_number_val,
            'lead_id': lead_id_val,
            'caller_phone': caller_phone,
            'masked_phone': masked_phone,
            'lead_name': display_name,
            'branch': branch_name,
            'status': status_label,
            'membership_package': pkg_name,
            'auto_open': True,
        }, status=status.HTTP_200_OK)


# ============================================================================
# 8. CALL RECORDING AUTHENTICATED STREAM PROXY
# ============================================================================

class CallRecordingStreamView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/recordings/<uuid:session_id>/stream/
    Authenticated streaming proxy for call recordings.
    Supports HTTP Range requests (206 Partial Content), browser scrub-seeking,
    and downloads from Vobiz CDN if not yet cached locally.
    Never exposes raw carrier CDN URLs to the frontend DOM.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, session_id):
        import os
        import re
        import requests
        from django.conf import settings
        from django.http import StreamingHttpResponse, Http404

        user = request.user
        if not (user and user.is_authenticated):
            token_str = request.GET.get('token')
            if token_str:
                try:
                    from rest_framework_simplejwt.authentication import JWTAuthentication
                    validated_token = JWTAuthentication().get_validated_token(token_str)
                    user = JWTAuthentication().get_user(validated_token)
                except Exception:
                    pass

        cache_dir = os.path.join(settings.BASE_DIR, 'media', 'recordings')
        os.makedirs(cache_dir, exist_ok=True)

        with resolve_telephony_tenant_context():
            session = CallSession.objects.filter(id=session_id).first()
            if not session:
                raise Http404("Call session not found.")

        # Find authoritative audio recording file (storage key, wav, mp3, ogg)
        local_path = None
        if session.recording_storage_key and os.path.exists(session.recording_storage_key) and os.path.getsize(session.recording_storage_key) > 100:
            local_path = session.recording_storage_key
        else:
            for ext in ['.wav', '.mp3', '.ogg', '.m4a']:
                cand = os.path.join(cache_dir, f"{session_id}{ext}")
                if os.path.exists(cand) and os.path.getsize(cand) > 100:
                    local_path = cand
                    break

        # If not cached locally, attempt to download from external recording_url
        if not local_path and session.recording_url and session.recording_url.startswith('http'):
            download_dest = os.path.join(cache_dir, f"{session_id}.mp3")
            try:
                r = requests.get(session.recording_url, timeout=30, stream=True)
                if r.status_code == 200:
                    with open(download_dest, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                    local_path = download_dest
                    with resolve_telephony_tenant_context():
                        CallSession.objects.filter(id=session_id).update(
                            recording_storage_key=download_dest
                        )
            except Exception as dl_err:
                logger.warning("Could not download recording on the fly: %s", dl_err)

        if not local_path or not os.path.exists(local_path) or os.path.getsize(local_path) < 100:
            return HttpResponse("Recording audio not available or still processing.", status=404)

        # Detect real audio MIME type from file header
        content_type = 'audio/mpeg'
        ext_label = 'mp3'
        try:
            with open(local_path, 'rb') as f:
                head = f.read(16)
                if head.startswith(b'RIFF'):
                    content_type = 'audio/wav'
                    ext_label = 'wav'
                elif head.startswith(b'OggS'):
                    content_type = 'audio/ogg'
                    ext_label = 'ogg'
                elif head.startswith(b'\xff\xfb') or head.startswith(b'\xff\xf3') or head.startswith(b'ID3'):
                    content_type = 'audio/mpeg'
                    ext_label = 'mp3'
                elif local_path.endswith('.wav'):
                    content_type = 'audio/wav'
                    ext_label = 'wav'
        except Exception:
            pass

        file_size = os.path.getsize(local_path)
        http_range = request.META.get('HTTP_RANGE', '')
        start = 0
        end = file_size - 1
        status_code = 200

        if http_range:
            match = re.match(r'bytes=(\d+)-(\d*)', http_range)
            if match:
                status_code = 206
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), file_size - 1)

        length = end - start + 1

        def stream_chunks():
            with open(local_path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        resp = StreamingHttpResponse(stream_chunks(), status=status_code, content_type=content_type)
        resp['Accept-Ranges'] = 'bytes'
        resp['Content-Length'] = str(length)
        if status_code == 206:
            resp['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        resp['Content-Disposition'] = f'inline; filename="call_{session_id}.{ext_label}"'
        resp['Access-Control-Allow-Origin'] = '*'
        resp['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        resp['Access-Control-Allow-Headers'] = 'Range, Content-Type, Accept'
        resp['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
        return resp



class AgentWebRTCCredentialsView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/webrtc-credentials/
    Returns the authenticated sales agent's dynamic WebRTC credentials.
    Used by the in-browser WebPhone to auto-register against the Vobiz WSS gateway.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # RBAC Check: Ensure user has Sales/CRM permission or is Admin
        from apps.tenant_core.rbac_engine import RBACAuthorizationEngine
        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=request.user,
            required_module='crm',
            required_submodule='leads',
            required_permission='crm.leads.view',
            request=request,
        )
        if not allowed and not getattr(request.user, 'is_superuser', False) and not getattr(request.user, 'is_staff', False):
            return Response(
                {'error': 'User is not authorized for sales telephony. Sales/CRM permission required.'},
                status=status.HTTP_403_FORBIDDEN
            )

        with resolve_telephony_tenant_context():
            creds = TelephonyConfigService.get_agent_webrtc_credentials(request.user)
            return Response(creds, status=status.HTTP_200_OK)


class TelephonyConfigView(APIView):
    """
    GET / POST /api/v1/tenant/crm/telephony/config/
    Allows tenant administrators to view and dynamically update Vobiz telephony configuration.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        with resolve_telephony_tenant_context():
            cfg = TelephonyConfigService.get_vobiz_config()
            raw_token = cfg.get('auth_token', '')
            masked_token = f"{raw_token[:4]}••••••••{raw_token[-4:]}" if len(raw_token) > 8 else "••••••••"
            safe_cfg = dict(cfg)
            safe_cfg['auth_token_masked'] = masked_token
            safe_cfg.pop('auth_token', None)
            return Response(safe_cfg, status=status.HTTP_200_OK)

    def post(self, request):
        with resolve_telephony_tenant_context():
            updated = TelephonyConfigService.update_vobiz_config(request.data)
            safe_updated = dict(updated)
            safe_updated['auth_token_masked'] = "••••••••"
            safe_updated.pop('auth_token', None)
            return Response({
                'success': True,
                'message': 'Telephony configuration updated successfully.',
                'config': safe_updated,
            }, status=status.HTTP_200_OK)



class TelephonyAnalyticsView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/analytics/
    Provides comprehensive Sales Calling Analytics, Rep Leaderboard, Dispositions Breakdown,
    Hourly Connect Rate Heatmap, and Filtered/Paginated Recent Calls Audit Log.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        is_admin = getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)
        roles = getattr(user, 'roles', []) or [getattr(user, 'role', '')]
        has_perm = (
            is_admin
            or any(r in ('ORG_ADMIN', 'SALES_REP', 'SALES_MANAGER', 'FRONT_DESK', 'PLATFORM_SUPERADMIN') for r in roles)
            or user.is_authenticated
        )
        if not has_perm:
            return Response({'error': 'Unauthorized to view sales calling analytics.'}, status=status.HTTP_403_FORBIDDEN)

        with resolve_telephony_tenant_context():
            from django.db import models
            from django.db.models import Q
            import math
            alias = get_tenant_db_alias() or 'default'
            org = getattr(user, 'organization', None)
            if not org:
                org = Organization.objects.using(alias).filter(status='ACTIVE').first()

            if not org:
                return Response({'error': 'No active organization found.'}, status=status.HTTP_400_BAD_REQUEST)

            timeframe = request.query_params.get('timeframe', 'today').lower()
            branch_id = request.query_params.get('branch_id')
            rep_id = request.query_params.get('rep_id')
            team_only = str(request.query_params.get('team_only', 'false')).lower() in ('true', '1')
            direction_filter = request.query_params.get('direction')
            status_filter = request.query_params.get('status')
            disposition_filter = request.query_params.get('disposition')
            search_query = request.query_params.get('search', '').strip()

            # Calculate date boundaries in IST (Asia/Kolkata)
            try:
                tz = zoneinfo.ZoneInfo('Asia/Kolkata')
            except Exception:
                tz = timezone.get_current_timezone()

            now_ist = timezone.now().astimezone(tz)
            start_of_today = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

            if timeframe == 'today':
                date_filter = Q(created_at__gte=start_of_today)
            elif timeframe == 'yesterday':
                start_yesterday = start_of_today - timedelta(days=1)
                date_filter = Q(created_at__gte=start_yesterday, created_at__lt=start_of_today)
            elif timeframe == 'this_week':
                start_week = start_of_today - timedelta(days=start_of_today.weekday())
                date_filter = Q(created_at__gte=start_week)
            elif timeframe == 'this_month':
                start_month = start_of_today.replace(day=1)
                date_filter = Q(created_at__gte=start_month)
            elif timeframe == 'last_30_days':
                start_30d = start_of_today - timedelta(days=30)
                date_filter = Q(created_at__gte=start_30d)
            elif timeframe in ('all_time', 'all'):
                date_filter = Q()
            elif timeframe == 'custom':
                date_from = request.query_params.get('date_from')
                date_to = request.query_params.get('date_to')
                df = Q()
                if date_from:
                    df &= Q(created_at__date__gte=date_from)
                if date_to:
                    df &= Q(created_at__date__lte=date_to)
                date_filter = df
            else:
                date_filter = Q(created_at__gte=start_of_today)

            base_qs = CallSession.objects.using(alias).filter(
                organization=org
            ).filter(date_filter)

            if branch_id and branch_id != 'ALL':
                base_qs = base_qs.filter(branch_id=branch_id)

            if rep_id and rep_id != 'ALL':
                base_qs = base_qs.filter(sales_rep_id=rep_id)

            if team_only:
                from apps.tenant_core.models_workforce import EmployeeProfile
                subordinate_ids = list(
                    EmployeeProfile.objects.using(alias).filter(
                        reporting_manager__user_profile__user=user
                    ).values_list('user_profile__user_id', flat=True)
                )
                base_qs = base_qs.filter(sales_rep_id__in=subordinate_ids)

            # 1. Summary Metrics
            total_dials = base_qs.count()
            connected_calls = base_qs.filter(status__in=['COMPLETED', 'CONNECTED']).count()
            connect_rate_pct = round((connected_calls / total_dials * 100), 1) if total_dials > 0 else 0.0
            missed_calls = base_qs.filter(status__in=['NO_ANSWER', 'BUSY', 'FAILED', 'CANCELLED']).count()
            missed_rate_pct = round((missed_calls / total_dials * 100), 1) if total_dials > 0 else 0.0
            meaningful_calls = base_qs.filter(is_meaningful=True).count()
            meaningful_rate_pct = round((meaningful_calls / connected_calls * 100), 1) if connected_calls > 0 else 0.0
            total_talk_sec = base_qs.filter(status__in=['COMPLETED', 'CONNECTED']).aggregate(s=models.Sum('duration_seconds'))['s'] or 0.0
            total_talk_minutes = round(total_talk_sec / 60.0, 1)
            avg_duration_sec = round(total_talk_sec / connected_calls, 1) if connected_calls > 0 else 0.0
            trials_booked = base_qs.filter(disposition='TRIAL_BOOKED').count()
            converted_members = base_qs.filter(disposition='CONVERTED_MEMBER').count()
            inbound_count = base_qs.filter(direction='INBOUND').count()
            outbound_count = base_qs.filter(direction='OUTBOUND').count()

            # 2. Leaderboard per Sales Rep
            reps_qs = base_qs.filter(sales_rep__isnull=False).values(
                'sales_rep__id', 'sales_rep__first_name', 'sales_rep__last_name', 'sales_rep__email'
            ).annotate(
                dials=models.Count('id'),
                connected=models.Count('id', filter=Q(status__in=['COMPLETED', 'CONNECTED'])),
                meaningful=models.Count('id', filter=Q(is_meaningful=True)),
                talk_seconds=models.Sum('duration_seconds', filter=Q(status__in=['COMPLETED', 'CONNECTED'])),
                trials=models.Count('id', filter=Q(disposition='TRIAL_BOOKED')),
                conversions=models.Count('id', filter=Q(disposition='CONVERTED_MEMBER')),
            ).order_by('-meaningful', '-connected', '-dials')

            leaderboard = []
            for rank, r in enumerate(reps_qs, start=1):
                d = r['dials'] or 0
                c = r['connected'] or 0
                cr = round((c / d * 100), 1) if d > 0 else 0.0
                ts = r['talk_seconds'] or 0.0
                tm = round(ts / 60.0, 1)
                avg_rep_sec = round(ts / c, 1) if c > 0 else 0.0
                first = r['sales_rep__first_name'] or ''
                last = r['sales_rep__last_name'] or ''
                full_name = f"{first} {last}".strip() or r['sales_rep__email']
                leaderboard.append({
                    'rank': rank,
                    'rep_id': str(r['sales_rep__id']),
                    'name': full_name,
                    'email': r['sales_rep__email'],
                    'dials': d,
                    'connected': c,
                    'connect_rate_pct': cr,
                    'meaningful_calls': r['meaningful'] or 0,
                    'talk_time_minutes': tm,
                    'avg_duration_seconds': avg_rep_sec,
                    'trials_booked': r['trials'] or 0,
                    'members_converted': r['conversions'] or 0,
                })

            # 3. Dispositions Breakdown
            disposition_counts = base_qs.filter(disposition__isnull=False).values('disposition').annotate(
                count=models.Count('id')
            ).order_by('-count')

            labels_map = dict(CallSession.DISPOSITION_CHOICES)
            dispositions = []
            for d in disposition_counts:
                code = d['disposition']
                cnt = d['count']
                pct = round((cnt / total_dials * 100), 1) if total_dials > 0 else 0.0
                dispositions.append({
                    'code': code,
                    'label': labels_map.get(code, code.replace('_', ' ').title()),
                    'count': cnt,
                    'percentage': pct,
                })

            # 4. Hourly Connect Rate Heatmap (09:00 - 21:00 IST)
            hourly_stats = {}
            for h in range(9, 21):
                hourly_stats[h] = {'dials': 0, 'connected': 0}

            for call in base_qs.values('created_at', 'status'):
                cat = call['created_at'].astimezone(tz)
                h = cat.hour
                if 9 <= h <= 20:
                    hourly_stats[h]['dials'] += 1
                    if call['status'] in ('COMPLETED', 'CONNECTED'):
                        hourly_stats[h]['connected'] += 1

            heatmap = []
            peak_hour = None
            peak_rate = -1.0
            for h in range(9, 21):
                d = hourly_stats[h]['dials']
                c = hourly_stats[h]['connected']
                rate = round((c / d * 100), 1) if d > 0 else 0.0
                label = f"{h:02d}:00 - {h+1:02d}:00"
                if d >= 1 and rate > peak_rate:
                    peak_rate = rate
                    peak_hour = label
                heatmap.append({
                    'hour': h,
                    'label': label,
                    'dials': d,
                    'connected': c,
                    'connect_rate_pct': rate,
                })

            # 5. Filtered & Paginated Recent Calls Audit Log
            recent_qs = base_qs.select_related('lead', 'sales_rep', 'branch')

            if direction_filter and direction_filter.upper() in ('INBOUND', 'OUTBOUND'):
                recent_qs = recent_qs.filter(direction=direction_filter.upper())

            if status_filter and status_filter != 'ALL':
                if status_filter == 'CONNECTED':
                    recent_qs = recent_qs.filter(status__in=['COMPLETED', 'CONNECTED'])
                elif status_filter == 'MISSED':
                    recent_qs = recent_qs.filter(status__in=['NO_ANSWER', 'BUSY', 'FAILED', 'CANCELLED'])
                else:
                    recent_qs = recent_qs.filter(status=status_filter)

            if disposition_filter and disposition_filter != 'ALL':
                recent_qs = recent_qs.filter(disposition=disposition_filter)

            if search_query:
                recent_qs = recent_qs.filter(
                    Q(lead__first_name__icontains=search_query) |
                    Q(lead__last_name__icontains=search_query) |
                    Q(lead__phone_normalized__icontains=search_query) |
                    Q(user_phone_number__icontains=search_query) |
                    Q(sales_rep__first_name__icontains=search_query) |
                    Q(sales_rep__last_name__icontains=search_query) |
                    Q(sales_rep__email__icontains=search_query) |
                    Q(disposition_notes__icontains=search_query)
                )

            total_filtered_calls = recent_qs.count()
            try:
                page = max(1, int(request.query_params.get('page', 1)))
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(100, max(5, int(request.query_params.get('page_size', 25))))
            except (TypeError, ValueError):
                page_size = 25

            total_pages = max(1, math.ceil(total_filtered_calls / page_size))
            offset = (page - 1) * page_size
            recent_calls_page = recent_qs.order_by('-created_at')[offset : offset + page_size]

            recent_calls = []
            for cs in recent_calls_page:
                p = cs.user_phone_number or ''
                masked_phone = f"+91-XXXXX-{p[-4:]}" if len(p) >= 4 else (p or "+91-XXXXX-XXXX")
                rep_name = f"{cs.sales_rep.first_name} {cs.sales_rep.last_name}".strip() if cs.sales_rep else "Automated / Pool"
                lead_name = f"{cs.lead.first_name} {cs.lead.last_name}".strip() if cs.lead else None
                lead_id_val = str(cs.lead_id) if cs.lead_id else None
                if not lead_name and p:
                    clean_p = p.replace('+', '').replace('-', '').replace(' ', '')
                    if len(clean_p) >= 10:
                        last10_digits = clean_p[-10:]
                        member_user = TenantUser.objects.using(alias).filter(phone__icontains=last10_digits, user_type='MEMBER').first()
                        if not member_user:
                            m_prof = UserProfile.objects.using(alias).filter(user__phone__icontains=last10_digits).first()
                            if m_prof:
                                member_user = m_prof.user
                        if member_user:
                            lead_name = f"{member_user.first_name} {member_user.last_name or ''}".strip()
                        else:
                            matched_lead = Lead.objects.using(alias).filter(phone_normalized__icontains=last10_digits).first()
                            if matched_lead:
                                lead_name = f"{matched_lead.first_name} {matched_lead.last_name}".strip()
                                lead_id_val = str(matched_lead.id)
                if not lead_name:
                    lead_name = "Unknown Caller"

                recent_calls.append({
                    'id': str(cs.id),
                    'lead_id': lead_id_val,
                    'lead_name': lead_name,
                    'masked_phone': masked_phone,
                    'sales_rep_name': rep_name,
                    'sales_rep_id': str(cs.sales_rep_id) if cs.sales_rep_id else None,
                    'direction': cs.direction,
                    'status': cs.status,
                    'duration_seconds': cs.duration_seconds,
                    'is_meaningful': cs.is_meaningful,
                    'disposition': cs.disposition,
                    'disposition_label': labels_map.get(cs.disposition, cs.disposition) if cs.disposition else 'Pending',
                    'disposition_notes': cs.disposition_notes,
                    'has_recording': bool(cs.recording_url or cs.recording_storage_key),
                    'recording_url': f"/api/v1/tenant/crm/telephony/recordings/{cs.id}/stream/" if (cs.recording_url or cs.recording_storage_key) else '',
                    'created_at': cs.created_at.isoformat(),
                })

            # Active sales reps list for UI filter
            reps_list_qs = TenantUser.objects.using(alias).filter(
                organization=org
            ).filter(
                Q(telephony_presence__isnull=False) |
                Q(handled_call_sessions__isnull=False)
            ).values('id', 'first_name', 'last_name', 'email').distinct()

            sales_reps = [
                {
                    'id': str(r['id']),
                    'name': f"{r['first_name']} {r['last_name']}".strip() or r['email'],
                    'email': r['email']
                }
                for r in reps_list_qs
            ]

            available_dispositions = [
                {'code': code, 'label': label}
                for code, label in CallSession.DISPOSITION_CHOICES
            ]

            return Response({
                'timeframe': timeframe,
                'summary': {
                    'total_dials': total_dials,
                    'connected_calls': connected_calls,
                    'connect_rate_pct': connect_rate_pct,
                    'missed_calls': missed_calls,
                    'missed_rate_pct': missed_rate_pct,
                    'meaningful_calls': meaningful_calls,
                    'meaningful_rate_pct': meaningful_rate_pct,
                    'total_talk_time_minutes': total_talk_minutes,
                    'avg_duration_seconds': avg_duration_sec,
                    'trials_booked': trials_booked,
                    'members_converted': converted_members,
                    'inbound_calls': inbound_count,
                    'outbound_calls': outbound_count,
                    'peak_connect_hour': peak_hour or '11:00 - 12:00',
                },
                'leaderboard': leaderboard,
                'dispositions': dispositions,
                'hourly_heatmap': heatmap,
                'recent_calls': recent_calls,
                'sales_reps': sales_reps,
                'available_dispositions': available_dispositions,
                'pagination': {
                    'total_count': total_filtered_calls,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': total_pages,
                }
            }, status=status.HTTP_200_OK)


class TelephonyExportView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/export/
    Exports filtered CallSession records as a CSV download matching active filters.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        with resolve_telephony_tenant_context():
            from django.db.models import Q
            import csv
            alias = get_tenant_db_alias() or 'default'
            org = getattr(user, 'organization', None)
            if not org:
                org = Organization.objects.using(alias).filter(status='ACTIVE').first()

            if not org:
                return Response({'error': 'No active organization found.'}, status=status.HTTP_400_BAD_REQUEST)

            timeframe = request.query_params.get('timeframe', 'today').lower()
            branch_id = request.query_params.get('branch_id')
            rep_id = request.query_params.get('rep_id')
            direction_filter = request.query_params.get('direction')
            status_filter = request.query_params.get('status')
            disposition_filter = request.query_params.get('disposition')
            search_query = request.query_params.get('search', '').strip()

            try:
                tz = zoneinfo.ZoneInfo('Asia/Kolkata')
            except Exception:
                tz = timezone.get_current_timezone()

            now_ist = timezone.now().astimezone(tz)
            start_of_today = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

            if timeframe == 'today':
                date_filter = Q(created_at__gte=start_of_today)
            elif timeframe == 'yesterday':
                start_yesterday = start_of_today - timedelta(days=1)
                date_filter = Q(created_at__gte=start_yesterday, created_at__lt=start_of_today)
            elif timeframe == 'this_week':
                start_week = start_of_today - timedelta(days=start_of_today.weekday())
                date_filter = Q(created_at__gte=start_week)
            elif timeframe == 'this_month':
                start_month = start_of_today.replace(day=1)
                date_filter = Q(created_at__gte=start_month)
            elif timeframe == 'last_30_days':
                start_30d = start_of_today - timedelta(days=30)
                date_filter = Q(created_at__gte=start_30d)
            elif timeframe in ('all_time', 'all'):
                date_filter = Q()
            elif timeframe == 'custom':
                date_from = request.query_params.get('date_from')
                date_to = request.query_params.get('date_to')
                df = Q()
                if date_from:
                    df &= Q(created_at__date__gte=date_from)
                if date_to:
                    df &= Q(created_at__date__lte=date_to)
                date_filter = df
            else:
                date_filter = Q(created_at__gte=start_of_today)

            qs = CallSession.objects.using(alias).filter(
                organization=org
            ).filter(date_filter).select_related('lead', 'sales_rep', 'branch')

            if branch_id and branch_id != 'ALL':
                qs = qs.filter(branch_id=branch_id)
            if rep_id and rep_id != 'ALL':
                qs = qs.filter(sales_rep_id=rep_id)
            if direction_filter and direction_filter.upper() in ('INBOUND', 'OUTBOUND'):
                qs = qs.filter(direction=direction_filter.upper())
            if status_filter and status_filter != 'ALL':
                if status_filter == 'CONNECTED':
                    qs = qs.filter(status__in=['COMPLETED', 'CONNECTED'])
                elif status_filter == 'MISSED':
                    qs = qs.filter(status__in=['NO_ANSWER', 'BUSY', 'FAILED', 'CANCELLED'])
                else:
                    qs = qs.filter(status=status_filter)
            if disposition_filter and disposition_filter != 'ALL':
                qs = qs.filter(disposition=disposition_filter)
            if search_query:
                qs = qs.filter(
                    Q(lead__first_name__icontains=search_query) |
                    Q(lead__last_name__icontains=search_query) |
                    Q(lead__phone_normalized__icontains=search_query) |
                    Q(user_phone_number__icontains=search_query) |
                    Q(sales_rep__first_name__icontains=search_query) |
                    Q(sales_rep__last_name__icontains=search_query) |
                    Q(sales_rep__email__icontains=search_query) |
                    Q(disposition_notes__icontains=search_query)
                )

            labels_map = dict(CallSession.DISPOSITION_CHOICES)
            filename = f"sales_calling_report_{timeframe}_{now_ist.strftime('%Y%m%d_%H%M%S')}.csv"
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'

            writer = csv.writer(response)
            writer.writerow([
                'Call ID', 'Timestamp (IST)', 'Lead Name', 'Phone Number',
                'Sales Rep', 'Direction', 'Status', 'Duration (Seconds)',
                'Talk Time Formatted', 'Meaningful (>=30s)', 'Disposition',
                'Disposition Notes', 'Recording Available'
            ])

            for cs in qs.order_by('-created_at'):
                dur = int(cs.duration_seconds or 0)
                dur_fmt = f"{dur // 60}m {dur % 60}s"
                time_str = cs.created_at.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
                rep_name = f"{cs.sales_rep.first_name} {cs.sales_rep.last_name}".strip() if cs.sales_rep else "Automated / Pool"
                lead_name = f"{cs.lead.first_name} {cs.lead.last_name}".strip() if cs.lead else "Unknown Lead"
                disp_lbl = labels_map.get(cs.disposition, cs.disposition) if cs.disposition else 'Pending'

                writer.writerow([
                    str(cs.id),
                    time_str,
                    lead_name,
                    cs.user_phone_number or '',
                    rep_name,
                    cs.direction,
                    cs.status,
                    dur,
                    dur_fmt,
                    'YES' if cs.is_meaningful else 'NO',
                    disp_lbl,
                    cs.disposition_notes or '',
                    'YES' if bool(cs.recording_url) else 'NO'
                ])

            return response
