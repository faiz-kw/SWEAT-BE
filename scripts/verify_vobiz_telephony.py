"""
backend/scripts/verify_vobiz_telephony.py — Live Verification Script for Vobiz Telephony & CRM Calling.
"""

import os
import sys
from pathlib import Path

# Setup Django Environment
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.utils import timezone
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, CallSession, AgentPresence, LeadActivity
from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
from apps.tenant_core.views_telephony import (
    VobizInboundWebhookView,
    VobizHangupWebhookView,
    CallDispositionView,
)
from rest_framework.test import APIRequestFactory, force_authenticate


def run_verification():
    print("================================================================")
    print("SWEAT CRM — VOBIZ TELEPHONY & SALES CALLING LIVE VERIFICATION")
    print("================================================================")

    # 1. Billing Pulse Math
    print("\n[1/6] Verifying 60-Second Billing Pulse Calculation...")
    assert VobizVoiceAdapter.calculate_billable_duration(0) == 0
    assert VobizVoiceAdapter.calculate_billable_duration(12) == 60
    assert VobizVoiceAdapter.calculate_billable_duration(60) == 60
    assert VobizVoiceAdapter.calculate_billable_duration(61) == 120
    assert VobizVoiceAdapter.calculate_billable_duration(184) == 240
    print("[OK] Pulse Billing: PASS (12s->60s, 61s->120s, 184s->240s)")

    # 2. VobizXML Generation
    print("\n[2/6] Verifying VobizXML Generation...")
    inbound_xml = VobizVoiceAdapter.build_inbound_dial_xml(
        agent_sip_uri="sip:agent_priya@registrar.vobiz.ai",
        caller_id="+919200000001",
        fallback_action_url="https://crm.sweatelite.in/api/v1/telephony/dial-fallback/",
        timeout=15,
    )
    assert '<Dial action=' in inbound_xml
    assert 'timeout="15"' in inbound_xml
    assert 'sip:agent_priya@registrar.vobiz.ai' in inbound_xml
    print("[OK] Inbound VobizXML: PASS (15s timeout & fallback ladder verified)")

    bridge_xml = VobizVoiceAdapter.build_agent_bridge_customer_xml(
        customer_phone="+919876543210",
        caller_id="+919200000001",
        whisper_text="Connecting to Rahul Sharma.",
    )
    assert '<Speak voice="WOMAN" language="en-IN">Connecting to Rahul Sharma.</Speak>' in bridge_xml or '<Speak voice="woman" language="en-IN">Connecting to Rahul Sharma.</Speak>' in bridge_xml
    assert '<Number>+919876543210</Number>' in bridge_xml
    print("[OK] Agent-First Bridge XML: PASS (Whisper tone + PSTN dial verified)")

    # 3. Setup Test Entities in Database
    print("\n[3/6] Setting Up Database Fixtures...")
    from apps.master.models_tenant import Tenant
    from apps.tenant_core.context import tenant_database_context

    tenant = Tenant.objects.using('default').first()
    if not tenant:
        print("No tenant in master DB, creating one...")
        tenant = Tenant.objects.using('default').create(
            name="SWEAT Elite",
            slug="sweat-elite",
        )

    with tenant_database_context(str(tenant.id)):
        org = Organization.objects.first()
        if not org:
            org = Organization.objects.create(name="SWEAT Elite Central", slug="sweat-central", code="SWEAT_C")

        from apps.tenant_core.models_org import Location
        location = Location.objects.filter(organization=org).first()
        if not location:
            location = Location.objects.create(organization=org, name="Mumbai Central", code="MUM")

        branch = Branch.objects.filter(organization=org).first()
        if not branch:
            branch = Branch.objects.create(organization=org, location=location, name="Andheri West Branch", code="ANDHERI")

        rep = TenantUser.objects.filter(email="telephony_rep@sweatelite.in").first()
        if not rep:
            rep = TenantUser.objects.create(
                email="telephony_rep@sweatelite.in",
                first_name="Priya",
                last_name="Sales",
                organization=org,
            )


        presence, _ = AgentPresence.objects.get_or_create(
            user=rep,
            defaults={
                'organization': org,
                'sip_username': 'agent_priya_test',
                'status': 'AVAILABLE',
            },
        )
        presence.status = 'AVAILABLE'
        presence.save()

        lead = Lead.objects.filter(phone_normalized="+919876543210").first()
        if not lead:
            lead = Lead.objects.create(
                organization=org,
                branch=branch,
                first_name="Vikram",
                last_name="Lead",
                phone_normalized="+919876543210",
                current_status="NEW_LEAD",
                assigned_sales_user=rep,
            )
        print("[OK] Fixtures: Org=%s, Rep=%s, Lead=%s" % (org.name, rep.email, lead.phone_normalized))

        # 4. Inbound Webhook Execution
        print("\n[4/6] Testing Inbound Webhook Execution...")
        CallSession.objects.filter(provider_attempt_id='uat-vobiz-uuid-001').delete()
        factory = APIRequestFactory()
        inbound_req = factory.post('/api/v1/telephony/inbound/', {
            'CallUUID': 'uat-vobiz-uuid-001',
            'From': '+919876543210',
            'To': '+919200000001',
        }, format='json')
        inbound_view = VobizInboundWebhookView.as_view()
        inbound_res = inbound_view(inbound_req)
        assert inbound_res.status_code == 200
        assert 'sip:agent_priya_test@registrar.vobiz.ai' in inbound_res.content.decode('utf-8')

        session = CallSession.objects.filter(provider_attempt_id='uat-vobiz-uuid-001').first()
        assert session is not None
        assert session.lead == lead
        assert session.direction == 'INBOUND'
        assert session.call_mode == 'HUMAN_MANUAL'
        presence.refresh_from_db()
        assert presence.status == 'ON_CALL'
        print("[OK] Inbound Flow: PASS (CallSession created, agent set to ON_CALL, XML returned)")

        # 5. Hangup Webhook Execution
        print("\n[5/6] Testing Hangup Webhook Execution...")
        hangup_req = factory.post('/api/v1/telephony/hangup/', {
            'CallUUID': 'uat-vobiz-uuid-001',
            'CallStatus': 'completed',
            'Duration': '184',
            'RecordingUrl': 'https://media.vobiz.ai/recordings/uat_rec_001.mp3',
            'HangupCause': 'NORMAL_CLEARING',
        }, format='json')
        hangup_view = VobizHangupWebhookView.as_view()
        hangup_res = hangup_view(hangup_req)
        assert hangup_res.status_code == 200

        session.refresh_from_db()
        assert session.duration_seconds == 184.0
        assert session.bill_duration_seconds == 240
        assert session.is_meaningful is True
        assert session.recording_url == 'https://media.vobiz.ai/recordings/uat_rec_001.mp3'

        presence.refresh_from_db()
        assert presence.status == 'WRAP_UP'
        print("[OK] Hangup Flow: PASS (Bill duration=240s, is_meaningful=True, agent set to WRAP_UP)")

        # 6. Post-Call Disposition Submission
        print("\n[6/6] Testing Mandatory Disposition Submission...")
        disp_req = factory.post('/api/v1/tenant/crm/telephony/disposition/', {
            'session_id': str(session.id),
            'disposition': 'TRIAL_BOOKED',
            'disposition_notes': 'Trial confirmed for tomorrow 7:00 AM at Andheri West.',
            'followup_date': '2026-10-28T07:00:00+05:30',
        }, format='json')
        force_authenticate(disp_req, user=rep)
        disp_view = CallDispositionView.as_view()
        disp_res = disp_view(disp_req)
        assert disp_res.status_code == 200

        session.refresh_from_db()
        assert session.disposition == 'TRIAL_BOOKED'
        lead.refresh_from_db()
        assert lead.current_status == 'TRIAL_SCHEDULED'
        presence.refresh_from_db()
        assert presence.status == 'AVAILABLE'
        assert presence.active_session is None

        activity = LeadActivity.objects.filter(lead=lead, external_reference=str(session.id)).first()
        assert activity is not None
        print("[OK] Disposition Flow: PASS (Lead advanced to TRIAL_SCHEDULED, Rep reset to AVAILABLE, Activity logged)")

        # 7. Authenticated Call Recording Stream Proxy
        print("\n[7/7] Testing Authenticated Audio Recording Proxy...")
        from apps.tenant_core.views_telephony import CallRecordingStreamView
        import os
        from django.conf import settings

        # Create dummy mp3 file in media/recordings to test proxy streaming
        cache_dir = os.path.join(settings.BASE_DIR, 'media', 'recordings')
        os.makedirs(cache_dir, exist_ok=True)
        dummy_file = os.path.join(cache_dir, f"{session.id}.mp3")
        with open(dummy_file, 'wb') as f:
            f.write(b'ID3\x03\x00\x00\x00\x00\x00\x00' + b'\x00' * 2048)

        stream_req = factory.get(f'/api/v1/tenant/crm/telephony/recordings/{session.id}/stream/')
        force_authenticate(stream_req, user=rep)
        stream_view = CallRecordingStreamView.as_view()
        stream_res = stream_view(stream_req, session_id=session.id)
        assert stream_res.status_code == 200
        assert stream_res.get('Content-Type') == 'audio/mpeg'
        assert stream_res.get('Accept-Ranges') == 'bytes'
        print("[OK] Audio Stream Proxy: PASS (Status=200, Content-Type=audio/mpeg, Range-seeking enabled)")

    print("\n================================================================")
    print("ALL 7 TELEPHONY & CALLING VERIFICATION CHECKS PASSED PERFECTLY!")
    print("================================================================")


if __name__ == '__main__':
    run_verification()
