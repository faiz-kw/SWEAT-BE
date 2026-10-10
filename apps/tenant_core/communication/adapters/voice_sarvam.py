"""
apps/tenant_core/communication/adapters/voice_sarvam.py — Sarvam AI Voice Agent Adapter.

Official Instant Outbound API Client for Sarvam Voice Agents using Sarvam-rented telephony.
Endpoint: POST https://apps.sarvam.ai/api/outbounds/v1/orgs/{org_id}/workspaces/{workspace_id}/outbounds
Authentication: X-API-Key header.
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

SARVAM_OUTBOUND_BASE_URL = "https://apps.sarvam.ai/api/outbounds/v1"


class SarvamVoiceAdapter(BaseCommunicationAdapter):
    """
    Adapter for Sarvam Voice Agents Instant Outbound calling.
    Uses rented phone number and connection_config for carrier termination.
    """
    channel: str = 'VOICE'
    provider_name: str = 'SARVAM'
    capabilities: List[str] = ['OUTBOUND_CALL', 'WEBHOOK_STATUS', 'TRANSCRIPT', 'AGENT_VARIABLES']

    SARVAM_STATUS_MAP = {
        'connected': 'COMPLETED',
        'in_progress': 'CONNECTED',
        'ringing': 'RINGING',
        'no_answer': 'NO_ANSWER',
        'busy': 'BUSY',
        'failed': 'FAILED',
    }

    def _get_api_key(self) -> str:
        """Resolve API key safely from credentials or environment."""
        import os
        return (
            self.credentials.get('api_key')
            or self.credentials.get('api_token')
            or os.getenv('SARVAM_API_KEY', '')
        ).strip()

    def is_configured(self) -> bool:
        """
        Validates whether all required workspace identifiers and API credentials exist.
        """
        api_key = self._get_api_key()
        org_id = self.config.get('org_id')
        workspace_id = self.config.get('workspace_id')
        app_id = self.config.get('app_id')
        connection_id = self.config.get('connection_id')
        agent_phone = self.config.get('agent_phone_number')

        return bool(api_key and org_id and workspace_id and app_id and connection_id and agent_phone)

    def create_instant_outbound_call(
        self,
        recipient_phone: str,
        webhook_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> ProviderSendResult:
        """
        Submits an individual outbound call to Sarvam Instant Outbound API.
        Does not log API key or PII.
        """
        if not self.is_configured():
            missing = []
            if not self._get_api_key(): missing.append('api_key')
            if not self.config.get('org_id'): missing.append('org_id')
            if not self.config.get('workspace_id'): missing.append('workspace_id')
            if not self.config.get('app_id'): missing.append('app_id')
            if not self.config.get('connection_id'): missing.append('connection_id')
            if not self.config.get('agent_phone_number'): missing.append('agent_phone_number')
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='UNCONFIGURED',
                error_message=f"Sarvam Voice adapter is missing required configuration: {', '.join(missing)}",
            )

        org_id = self.config['org_id']
        workspace_id = self.config['workspace_id']
        app_id = self.config['app_id']
        app_version = self.config.get('app_version', 1)
        connection_id = self.config['connection_id']
        agent_phone = self.config['agent_phone_number']

        endpoint_url = f"{SARVAM_OUTBOUND_BASE_URL}/orgs/{org_id}/workspaces/{workspace_id}/outbounds"

        payload = {
            "app_config": {
                "app_id": str(app_id),
                "app_version": int(app_version) if str(app_version).isdigit() else app_version,
                "connection_config": {
                    "connection_id": str(connection_id),
                    "agent_phone_number": str(agent_phone),
                },
            },
            "user_config": {
                "user_phone_number": str(recipient_phone),
            },
        }

        if webhook_url:
            payload["webhook_config"] = {
                "url": webhook_url,
                "metadata": metadata or {},
            }

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._get_api_key(),
            "User-Agent": "SWEAT-CRM-VoiceEngine/1.0",
        }

        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(endpoint_url, data=data_bytes, headers=headers, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.getcode()
                resp_body = resp.read().decode('utf-8')
                resp_json = json.loads(resp_body) if resp_body else {}

                if 200 <= status_code < 300:
                    attempt_id = resp_json.get('attempt_id')
                    logger.info("Sarvam Instant Outbound call submitted successfully. attempt_id=%s", attempt_id)
                    return ProviderSendResult(
                        success=True,
                        provider_message_id=attempt_id,
                        status='INITIATED',
                        raw_response=resp_json,
                    )
                else:
                    return ProviderSendResult(
                        success=False,
                        status='FAILED',
                        error_code=f"HTTP_{status_code}",
                        error_message=f"Sarvam returned non-200 status code: {status_code}",
                        raw_response=resp_json,
                    )

        except urllib.error.HTTPError as http_err:
            err_body = ''
            try:
                err_body = http_err.read().decode('utf-8')
            except Exception:
                pass
            logger.error("Sarvam Instant Outbound HTTPError %s: %s", http_err.code, err_body[:200])
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code=f"HTTP_{http_err.code}",
                error_message=f"Sarvam HTTP {http_err.code}: {err_body[:200]}",
                raw_response={'status_code': http_err.code, 'body': err_body[:500]},
            )

        except urllib.error.URLError as url_err:
            logger.error("Sarvam Instant Outbound network URLError: %s", url_err.reason)
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='NETWORK_ERROR',
                error_message=f"Network error connecting to Sarvam: {url_err.reason}",
            )

        except TimeoutError:
            logger.error("Sarvam Instant Outbound request timed out after %s seconds", timeout)
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='TIMEOUT',
                error_message=f"Request to Sarvam timed out after {timeout}s",
            )

        except Exception as exc:
            logger.exception("Unexpected exception dispatching Sarvam Instant Outbound call")
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='UNEXPECTED_ERROR',
                error_message=str(exc),
            )

    def send(
        self,
        recipient: str,
        body: str = '',
        subject: str = '',
        template_ref: str = '',
        template_vars: Optional[Dict[str, Any]] = None,
        sender_id: str = '',
        extra_headers: Optional[Dict[str, Any]] = None,
    ) -> ProviderSendResult:
        """
        Implementation of BaseCommunicationAdapter.send interface.
        """
        metadata = template_vars or {}
        webhook_url = metadata.get('webhook_url') or self.config.get('webhook_url')
        return self.create_instant_outbound_call(
            recipient_phone=recipient,
            webhook_url=webhook_url,
            metadata=metadata,
        )

    def verify_webhook_signature(self, payload_bytes: bytes, headers: Dict[str, Any]) -> bool:
        """
        Verifies incoming webhook authentication.
        If a shared webhook token is configured in integration configuration,
        verifies that 'x-webhook-token' or 'authorization' matches.
        """
        expected_token = (
            self.config.get('webhook_token')
            or self.config.get('verify_token')
            or self.credentials.get('webhook_token')
        )
        if not expected_token:
            return True

        provided_token = (
            headers.get('HTTP_X_WEBHOOK_TOKEN')
            or headers.get('X-Webhook-Token')
            or headers.get('HTTP_AUTHORIZATION', '').replace('Bearer ', '').strip()
        )
        if not provided_token and 'QUERY_STRING' in headers:
            import urllib.parse
            qp = urllib.parse.parse_qs(headers.get('QUERY_STRING', ''))
            provided_token = (qp.get('token') or qp.get('verify_token') or [''])[0]

        return provided_token == expected_token

    def parse_status_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, Any]] = None,
    ) -> Optional[ProviderStatusEvent]:
        """
        Extracts call attempt status, duration, transcripts, and variables from callback.
        """
        attempt_id = payload.get('attempt_id')
        raw_status = (payload.get('status') or '').strip().lower()

        if not attempt_id:
            return None

        normalized_status = self.SARVAM_STATUS_MAP.get(raw_status, 'COMPLETED' if raw_status == 'connected' else 'FAILED')

        return ProviderStatusEvent(
            provider_message_id=attempt_id,
            status=normalized_status,
            provider_event_id=payload.get('interaction_id') or attempt_id,
            occurred_at=timezone.now(),
            raw_metadata={
                'duration': payload.get('duration', 0.0),
                'interaction_transcript': payload.get('interaction_transcript', []),
                'final_agent_variables': payload.get('final_agent_variables') or {},
                'metadata': payload.get('metadata') or {},
                'start_datetime': payload.get('start_datetime'),
                'end_datetime': payload.get('end_datetime'),
            },
        )
