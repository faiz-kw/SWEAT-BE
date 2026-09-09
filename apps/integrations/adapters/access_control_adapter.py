"""
Access Control Hardware Adapter (Door Turnstile & QR Entry).
Implements the proven flow: booking exists -> time-boxed QR generated -> scanned at door -> attendance logged.
"""

import json
import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Dict, Any
from .base import BaseAccessControlAdapter

class AccessControlAdapter(BaseAccessControlAdapter):
    def __init__(self, secret_key: str = None):
        self.secret_key = secret_key or os.getenv('ACCESS_CONTROL_SECRET', 'door_controller_secret_key_2026')

    def generate_qr_credential(self, member_id: str, booking_id: str, valid_from: str, valid_until: str) -> str:
        """
        Generates a cryptographically signed, time-boxed QR payload string.
        Format: base64(payload).signature
        """
        payload = {
            'mid': member_id,
            'bid': booking_id,
            'v_from': valid_from,
            'v_until': valid_until,
        }
        json_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        b64_payload = base64.urlsafe_b64encode(json_bytes).decode('utf-8')

        sig = hmac.new(
            self.secret_key.encode('utf-8'),
            b64_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()[:16]

        return f"QR_{b64_payload}.{sig}"

    def validate_door_scan(self, qr_token: str, device_id: str = "DOOR-01") -> Dict[str, Any]:
        """
        Validates the scanned QR token:
        1. Checks HMAC signature integrity.
        2. Validates current time against [valid_from, valid_until] window.
        Returns access decision dictionary.
        """
        if not qr_token or not qr_token.startswith("QR_") or "." not in qr_token:
            return {'access_granted': False, 'reason': 'Invalid QR format'}

        try:
            token_body = qr_token[3:]
            b64_payload, received_sig = token_body.split(".", 1)

            # 1. Verify Signature
            expected_sig = hmac.new(
                self.secret_key.encode('utf-8'),
                b64_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()[:16]

            if not hmac.compare_digest(expected_sig, received_sig):
                return {'access_granted': False, 'reason': 'Cryptographic signature mismatch'}

            # 2. Decode Payload
            json_bytes = base64.urlsafe_b64decode(b64_payload.encode('utf-8'))
            payload = json.loads(json_bytes.decode('utf-8'))

            member_id = payload['mid']
            booking_id = payload['bid']
            valid_from = datetime.fromisoformat(payload['v_from'])
            valid_until = datetime.fromisoformat(payload['v_until'])

            now = datetime.now(timezone.utc)

            # Ensure valid_from/until are timezone-aware for comparison
            if valid_from.tzinfo is None:
                valid_from = valid_from.replace(tzinfo=timezone.utc)
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)

            # 3. Check Timing Window
            if now < valid_from:
                return {
                    'access_granted': False,
                    'reason': f"Access not open yet (opens at {valid_from.strftime('%H:%M')})",
                    'member_id': member_id,
                    'booking_id': booking_id,
                }

            if now > valid_until:
                return {
                    'access_granted': False,
                    'reason': f"QR code expired (expired at {valid_until.strftime('%H:%M')})",
                    'member_id': member_id,
                    'booking_id': booking_id,
                }

            return {
                'access_granted': True,
                'member_id': member_id,
                'booking_id': booking_id,
                'device_id': device_id,
                'scanned_at': now.isoformat(),
            }

        except Exception as e:
            return {'access_granted': False, 'reason': f"Malformed QR payload: {str(e)}"}
