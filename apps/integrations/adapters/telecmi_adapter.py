"""
TeleCMI Telephony & Call Logging Adapter implementation.
Connects CRM with outbound calling APIs and ingests call outcome webhooks.
"""

import os
import uuid
from typing import Dict, Any
from .base import BaseTelephonyAdapter

class TeleCMIAdapter(BaseTelephonyAdapter):
    def __init__(self, app_id: str = None, app_secret: str = None):
        self.app_id = app_id or os.getenv('TELECMI_APP_ID', 'telecmi_test_app')
        self.app_secret = app_secret or os.getenv('TELECMI_SECRET', 'telecmi_test_secret')

    def initiate_outbound_call(self, agent_phone: str, customer_phone: str, custom_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Initiates a click-to-call bridge between gym staff/AI voice bot and prospective lead.
        """
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        return {
            'status': 'initiated',
            'call_id': call_id,
            'from': agent_phone,
            'to': customer_phone,
            'custom_data': custom_data or {},
            'provider': 'TeleCMI',
        }

    def parse_call_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Standardizes TeleCMI webhook payload into platform call activity dict.
        """
        return {
            'call_id': payload.get('call_id') or payload.get('id', 'unknown'),
            'duration_seconds': int(payload.get('duration', 0)),
            'status': payload.get('status', 'completed'),
            'recording_url': payload.get('recording_url', ''),
            'customer_phone': payload.get('customer_number') or payload.get('to', ''),
        }
