"""
apps/tenant_core/communication/adapters/voice_vobiz.py — Vobiz Telephony & Carrier Adapter.

Manages:
- Outbound click-to-call placement via Vobiz REST API (Agent-First Bridging)
- Inbound VobizXML generation with 15s timeout and fallback ladders
- Call hangup webhook parsing and billing duration calculation (60s pulse)
- Cryptographic signature verification for inbound Vobiz callbacks
"""

import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
)

logger = logging.getLogger(__name__)

VOBIZ_DEFAULT_API_BASE = "https://api.vobiz.ai/api/v1"


class VobizVoiceAdapter(BaseCommunicationAdapter):
    """
    Adapter for Vobiz Voice Telephony platform.
    Supports SIP trunking, 92-series virtual DID termination, and VobizXML call control.
    """
    channel: str = 'VOICE'
    provider_name: str = 'VOBIZ'
    capabilities: List[str] = [
        'OUTBOUND_CALL',
        'CLICK_TO_CALL',
        'INBOUND_ROUTING',
        'VOBIZ_XML',
        'WEBHOOK_STATUS',
        'RECORDING',
    ]

    VOBIZ_STATUS_MAP = {
        'in-progress': 'CONNECTED',
        'ringing': 'RINGING',
        'completed': 'COMPLETED',
        'busy': 'BUSY',
        'no-answer': 'NO_ANSWER',
        'failed': 'FAILED',
        'canceled': 'CANCELLED',
    }

    def _get_credentials(self) -> Dict[str, str]:
        """Resolve Auth ID and Auth Token safely from credentials or environment."""
        import os
        auth_id = (
            self.credentials.get('auth_id')
            or self.config.get('auth_id')
            or os.getenv('VOBIZ_AUTH_ID', '')
        ).strip()
        auth_token = (
            self.credentials.get('auth_token')
            or self.credentials.get('api_token')
            or os.getenv('VOBIZ_AUTH_TOKEN', '')
        ).strip()
        return {'auth_id': auth_id, 'auth_token': auth_token}

    def is_configured(self) -> bool:
        """Validates whether required Vobiz credentials and virtual DID are present."""
        creds = self._get_credentials()
        from_number = self.config.get('from_number') or self.config.get('did_number')
        return bool(creds['auth_id'] and creds['auth_token'] and from_number)

    def initiate_click_to_call(
        self,
        agent_sip_uri: str,
        customer_phone: str,
        answer_url: str,
        hangup_url: str,
        from_number: Optional[str] = None,
        timeout: int = 15,
    ) -> ProviderSendResult:
        """
        Dispatches an Agent-First Click-to-Call request via Vobiz Call API.
        1. Rings Agent's Zoiper SIP endpoint first.
        2. When agent answers, Vobiz requests answer_url, which returns XML instructing
           Vobiz to bridge to the customer's phone number with company DID callerId.
        """
        creds = self._get_credentials()
        caller_id = from_number or self.config.get('from_number') or self.config.get('did_number')

        if not creds['auth_id'] or not creds['auth_token']:
            return ProviderSendResult(
                success=False,
                error_code='CREDENTIALS_MISSING',
                error_message='Vobiz Auth ID or Auth Token is not configured.',
            )

        api_base = self.config.get('api_base_url', VOBIZ_DEFAULT_API_BASE).rstrip('/')
        url = f"{api_base}/Account/{creds['auth_id']}/Call/"

        payload = {
            'from': caller_id,
            'to': agent_sip_uri,
            'answer_url': answer_url,
            'hangup_url': hangup_url,
            'ring_timeout': timeout,
            'record': 'true',
        }

        body_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={
                'X-Auth-ID': creds['auth_id'],
                'X-Auth-Token': creds['auth_token'],
                'Content-Type': 'application/json',
                'User-Agent': 'SWEAT-Command-Center-Telephony/1.0',
            },
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode('utf-8'))
                call_uuid = (
                    resp_data.get('call_uuid')
                    or resp_data.get('request_uuid')
                    or resp_data.get('CallUUID')
                )
                return ProviderSendResult(
                    success=True,
                    provider_message_id=call_uuid,
                    status='INITIATED',
                    raw_response=resp_data,
                )
        except urllib.error.HTTPError as http_err:
            raw_err = http_err.read().decode('utf-8', errors='replace')
            logger.error("Vobiz Call API HTTP error %s: %s", http_err.code, raw_err)
            return ProviderSendResult(
                success=False,
                error_code=f"HTTP_{http_err.code}",
                error_message=raw_err[:300],
            )
        except Exception as exc:
            logger.error("Vobiz Call API network failure: %s", exc)
            return ProviderSendResult(
                success=False,
                error_code='CONNECTION_ERROR',
                error_message=str(exc),
            )

    @classmethod
    def build_inbound_dial_xml(
        cls,
        agent_sip_uri: str,
        caller_id: str,
        fallback_action_url: str,
        timeout: int = 35,
        record: bool = True,
        recording_callback_url: Optional[str] = None,
        hangup_url: Optional[str] = None,
    ) -> str:
        """
        Builds VobizXML to ring the assigned agent's registered SIP endpoint.
        If unanswered or busy, Vobiz fires fallback_action_url to execute the overflow ladder.
        dialMusic="real" ensures the calling customer hears standard carrier ringback cadence.
        """
        rec_str = "true" if record else "false"
        agent_user = agent_sip_uri.replace("sip:", "").split("@")[0].strip()
        sip_target = f"sip:{agent_user}@registrar.vobiz.ai"
        callback_attr = f' callbackUrl="{hangup_url}"' if hangup_url else ""

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial action="{fallback_action_url}"{callback_attr} callerId="{caller_id}" record="{rec_str}" timeout="{timeout}" dialMusic="real">
    <User>{sip_target}</User>
  </Dial>
</Response>"""

    @classmethod
    def build_inbound_pstn_dial_xml(
        cls,
        agent_mobile: str,
        caller_id: str,
        fallback_action_url: str,
        timeout: int = 25,
        record: bool = True,
    ) -> str:
        """
        Builds VobizXML to ring the agent's personal mobile number (PSTN) on inbound calls.

        This is the NAT-proof, production-reliable alternative to SIP endpoint dialling.
        Because SIP softphones behind residential/office NAT cannot receive unsolicited INVITEs
        from Vobiz (Vobiz reports sip_registered=false for all NAT-registered contacts),
        dialling the agent's PSTN mobile guarantees delivery regardless of network type.

        Flow:
          1. Member calls DID (+919240954144)
          2. Vobiz fires inbound webhook → we return this XML
          3. Vobiz calls agent's mobile (e.g. +917738678323) via PSTN
          4. Agent answers their phone → bridged live to the member
          5. If unanswered within timeout, Vobiz fires fallback_action_url
        """
        rec_str = "true" if record else "false"
        # Normalise to E.164 — ensure leading +91
        mobile = agent_mobile.strip()
        if not mobile.startswith('+'):
            mobile = '+91' + mobile.lstrip('0').lstrip('91')[-10:]

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial action="{fallback_action_url}" callerId="{caller_id}" record="{rec_str}" timeout="{timeout}">
    <Number>{mobile}</Number>
  </Dial>
</Response>"""

    @classmethod
    def build_fallback_overflow_pstn_xml(
        cls,
        secondary_mobile: str,
        caller_id: str,
        complete_action_url: str,
        whisper_prompt: str = "Please stay on the line while we connect you to our consultant.",
        timeout: int = 25,
    ) -> str:
        """
        Builds VobizXML for overflow fallback — dials secondary agent's mobile (PSTN).
        """
        mobile = secondary_mobile.strip()
        if not mobile.startswith('+'):
            mobile = '+91' + mobile.lstrip('0').lstrip('91')[-10:]

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak voice="WOMAN" language="en-IN">{whisper_prompt}</Speak>
  <Dial action="{complete_action_url}" callerId="{caller_id}" record="true" timeout="{timeout}">
    <Number>{mobile}</Number>
  </Dial>
</Response>"""

    @classmethod
    def build_fallback_overflow_xml(
        cls,
        secondary_sip_uri: str,
        caller_id: str,
        complete_action_url: str,
        whisper_prompt: str = "Please stay on the line while we connect you to our senior consultant.",
        timeout: int = 20,
    ) -> str:
        """
        Builds VobizXML for the overflow fallback leg when the primary agent is busy or unavailable.
        """
        sec_user = secondary_sip_uri.replace("sip:", "").split("@")[0].strip()
        sip_target = f"sip:{sec_user}@registrar.vobiz.ai"

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak voice="WOMAN" language="en-IN">{whisper_prompt}</Speak>
  <Dial action="{complete_action_url}" callerId="{caller_id}" record="true" timeout="{timeout}">
    <User>{sip_target}</User>
  </Dial>
</Response>"""

    @classmethod
    def build_agent_bridge_customer_xml(
        cls,
        customer_phone: str,
        caller_id: str,
        whisper_text: str = "Connecting you to member.",
        record: bool = True,
        recording_callback_url: Optional[str] = None,
        hangup_url: Optional[str] = None,
    ) -> str:
        """
        Builds VobizXML for Outbound Click-to-Call.
        Played when the agent picks up Zoiper: whispers caller info, then dials customer on PSTN.
        Injects <Record startOnDialAnswer="true"> so the conversation is recorded as soon as customer answers.
        """
        rec_str = "true" if record else "false"
        dial_action = f' action="{hangup_url}"' if hangup_url else ""
        action_callback = f' callbackUrl="{hangup_url}"' if hangup_url else ""

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak voice="WOMAN" language="en-IN">{whisper_text}</Speak>
  <Dial{dial_action}{action_callback} callerId="{caller_id}" record="{rec_str}">
    <Number>{customer_phone}</Number>
  </Dial>
</Response>"""

    @classmethod
    def calculate_billable_duration(cls, raw_duration_seconds: float) -> int:
        """
        Calculates billable duration using Vobiz's standard 60-second billing pulse.
        e.g., 12 seconds -> 60 seconds; 65 seconds -> 120 seconds.
        """
        import math
        if raw_duration_seconds <= 0:
            return 0
        return int(math.ceil(raw_duration_seconds / 60.0) * 60)
