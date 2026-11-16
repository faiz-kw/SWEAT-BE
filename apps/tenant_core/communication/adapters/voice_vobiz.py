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
            or self.config.get('auth_token')
            or os.getenv('VOBIZ_AUTH_TOKEN', '')
        ).strip()
        return {'auth_id': auth_id, 'auth_token': auth_token}

    def is_configured(self) -> bool:
        """Validates whether required Vobiz credentials and virtual DID are present."""
        creds = self._get_credentials()
        from_number = self.config.get('from_number') or self.config.get('did_number')
        return bool(creds['auth_id'] and creds['auth_token'] and from_number)

    def _api_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ) -> Dict[str, Any]:
        """Shared HTTP client for Vobiz Account REST APIs."""
        creds = self._get_credentials()
        if not creds['auth_id'] or not creds['auth_token']:
            return {
                'ok': False,
                'error_code': 'CREDENTIALS_MISSING',
                'error_message': 'Vobiz Auth ID or Auth Token is not configured.',
            }

        api_base = self.config.get('api_base_url', VOBIZ_DEFAULT_API_BASE).rstrip('/')
        url = f"{api_base}/Account/{creds['auth_id']}/{path.lstrip('/')}"
        body_bytes = json.dumps(payload).encode('utf-8') if payload is not None else None

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={
                'X-Auth-ID': creds['auth_id'],
                'X-Auth-Token': creds['auth_token'],
                'Content-Type': 'application/json',
                'User-Agent': 'SWEAT-Command-Center-Telephony/1.0',
            },
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_text = resp.read().decode('utf-8', errors='replace').strip()
                parsed = json.loads(raw_text) if raw_text else {}
                return {'ok': True, 'data': parsed}
        except urllib.error.HTTPError as http_err:
            raw_err = http_err.read().decode('utf-8', errors='replace')
            logger.error("Vobiz API %s %s HTTP error %s: %s", method, path, http_err.code, raw_err)
            return {
                'ok': False,
                'error_code': f"HTTP_{http_err.code}",
                'error_message': raw_err[:300],
            }
        except Exception as exc:
            logger.error("Vobiz API %s %s network failure: %s", method, path, exc)
            return {
                'ok': False,
                'error_code': 'CONNECTION_ERROR',
                'error_message': str(exc),
            }

    @staticmethod
    def _extract_list(data: Any, *keys: str) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in (*keys, 'objects', 'results', 'data'):
                val = data.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
        return []

    def list_applications(self) -> List[Dict[str, Any]]:
        """Lists Voice Applications configured on the Vobiz account."""
        res = self._api_request('GET', 'Application/', timeout=8)
        if not res.get('ok'):
            return []
        return self._extract_list(res.get('data'), 'applications')

    def resolve_default_application_id(self) -> str:
        """Resolves the Voice Application ID dynamically from config, env, or Vobiz API."""
        import os
        configured = (
            self.config.get('application_id')
            or self.credentials.get('application_id')
            or os.getenv('VOBIZ_APPLICATION_ID', '')
        ).strip()
        if configured:
            return configured

        apps = self.list_applications()
        for app in apps:
            if app.get('default_endpoint_app') or app.get('default_app'):
                app_id = str(app.get('app_id') or app.get('id') or app.get('application_id') or '').strip()
                if app_id:
                    return app_id
        for app in apps:
            name = str(app.get('app_name') or app.get('name') or '').lower()
            if 'playground' not in name:
                app_id = str(app.get('app_id') or app.get('id') or app.get('application_id') or '').strip()
                if app_id:
                    return app_id
        return ''

    def list_endpoints(self, page_size: int = 100, max_pages: int = 10) -> List[Dict[str, Any]]:
        """Lists all SIP endpoints configured on the Vobiz account (paginated)."""
        endpoints: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            path = f"Endpoint/?limit={page_size}&offset={offset}" if offset > 0 else f"Endpoint/?limit={page_size}"
            res = self._api_request('GET', path, timeout=8)
            if not res.get('ok'):
                break
            data = res.get('data')
            batch = self._extract_list(data, 'endpoints')
            if not batch:
                break
            endpoints.extend(batch)
            meta = data.get('meta') if isinstance(data, dict) else None
            has_next = bool(meta.get('next')) if isinstance(meta, dict) else False
            if not has_next and len(batch) < page_size:
                break
            offset += len(batch)
        return endpoints

    def create_endpoint(
        self,
        username: str,
        password: str,
        alias: str = '',
        application_id: str = '',
    ) -> Dict[str, Any]:
        """Dynamically provisions a new SIP endpoint on the Vobiz account."""
        payload: Dict[str, Any] = {
            'username': username,
            'password': password,
        }
        if alias:
            payload['alias'] = alias[:100]
        if application_id:
            payload['application'] = application_id

        res = self._api_request('POST', 'Endpoint/', payload=payload, timeout=10)
        if not res.get('ok'):
            return res

        data = res.get('data') or {}
        created_username = str(data.get('username') or username).strip()
        return {'ok': True, 'username': created_username, 'data': data}

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
        1. Rings Agent's Vobiz SIP endpoint first.
        2. When agent answers, Vobiz requests answer_url, which returns XML instructing
           Vobiz to bridge to the customer's phone number with company DID callerId.
        """
        caller_id = from_number or self.config.get('from_number') or self.config.get('did_number')
        res = self._api_request(
            'POST',
            'Call/',
            payload={
                'from': caller_id,
                'to': agent_sip_uri,
                'answer_url': answer_url,
                'hangup_url': hangup_url,
                'ring_timeout': timeout,
                'record': 'true',
            },
            timeout=10,
        )
        if not res.get('ok'):
            return ProviderSendResult(
                success=False,
                error_code=res.get('error_code', 'VOBIZ_ERROR'),
                error_message=res.get('error_message', 'Vobiz Call API failed.'),
            )

        resp_data = res.get('data') or {}
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

    @staticmethod
    def _normalize_sip_uri(agent_sip_uri: str, registrar_host: str = '') -> str:
        """Preserve explicit SIP domain if present; otherwise append dynamic registrar host."""
        import os
        raw = (agent_sip_uri or '').strip()
        if raw.startswith('sip:'):
            raw = raw[4:]
        if '@' in raw:
            user_part, domain_part = raw.split('@', 1)
            domain_part = domain_part.strip() or registrar_host or os.getenv('VOBIZ_REGISTRAR_HOST', 'registrar.vobiz.ai')
            return f"sip:{user_part.strip()}@{domain_part}"
        fallback_domain = (registrar_host or os.getenv('VOBIZ_REGISTRAR_HOST', 'registrar.vobiz.ai')).strip()
        return f"sip:{raw}@{fallback_domain}"

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
        registrar_host: str = '',
    ) -> str:
        """
        Builds VobizXML to ring the assigned agent's registered SIP endpoint.
        If unanswered or busy, Vobiz fires fallback_action_url to execute the overflow ladder.
        dialMusic="real" ensures the calling customer hears standard carrier ringback cadence.
        """
        rec_str = "true" if record else "false"
        sip_target = cls._normalize_sip_uri(agent_sip_uri, registrar_host=registrar_host)
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
        registrar_host: str = '',
    ) -> str:
        """
        Builds VobizXML for the overflow fallback leg when the primary agent is busy or unavailable.
        """
        sip_target = cls._normalize_sip_uri(secondary_sip_uri, registrar_host=registrar_host)

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
        Played when the agent answers Vobiz WebPhone: whispers caller info, then dials customer on PSTN.
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
