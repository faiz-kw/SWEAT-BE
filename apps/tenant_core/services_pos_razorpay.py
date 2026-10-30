"""
apps/tenant_core/services_pos_razorpay.py — Razorpay POS (Ezetap Cloud Push) Adapter

Implements the BasePOSAdapter for Razorpay's Android Smart POS / mPOS fleet.
Translates CRM commands to Razorpay's Cloud Push REST API (p2padapter).
"""

import requests
import logging
from decimal import Decimal
from typing import Dict, Any, Optional
from .services_pos_base import BasePOSAdapter, POSResponse

logger = logging.getLogger(__name__)


class RazorpayPOSAdapter(BasePOSAdapter):
    """
    Adapter communicating with Razorpay POS Cloud Infrastructure.
    Uses push-to-device remote routing over 4G/Wi-Fi to trigger payments on physical terminals.
    """

    DEFAULT_PROD_URL = "https://www.ezetap.com/api/3.0/p2padapter"
    DEFAULT_DEMO_URL = "https://demo.ezetap.com/api/3.0/p2padapter"

    def __init__(self, provider_config):
        super().__init__(provider_config)
        if provider_config.api_base_url:
            self.base_url = provider_config.api_base_url.rstrip("/")
        elif provider_config.is_test_mode:
            self.base_url = self.DEFAULT_DEMO_URL
        else:
            self.base_url = self.DEFAULT_PROD_URL

        self.username = provider_config.username or ""
        self.app_key = provider_config.app_key or ""
        self.api_key_id = provider_config.api_key_id or ""
        self.api_key_secret = provider_config.api_key_secret or ""

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return headers

    def push_payment(
        self,
        terminal,
        amount: Decimal,
        external_ref_id: str,
        customer_mobile: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> POSResponse:
        """
        Sends an HTTP POST to Razorpay POS Cloud to wake up the terminal with the amount.
        """
        opts = options or {}
        payload = {
            "username": self.username,
            "appKey": self.app_key,
            "terminalId": terminal.terminal_id,
            "deviceSerialNo": terminal.device_serial_no,
            "amount": float(amount),
            "externalRefId": external_ref_id,
            "customerMobile": customer_mobile or "",
            "paymentMode": opts.get("payment_mode", "ALL"),
            "options": {
                "printReceipt": opts.get("print_receipt", True),
                "allowTip": False,
            },
        }

        url = f"{self.base_url}/pay"
        try:
            logger.info("Pushing POS payment to terminal %s (Ref: %s, Amount: %s)", terminal.terminal_id, external_ref_id, amount)
            response = requests.post(url, json=payload, headers=self._get_headers(), timeout=12)
            data = response.json() if response.text else {}

            if response.status_code == 200 and (data.get("success") is True or data.get("status") in ["SUCCESS", "INITIATED"]):
                return POSResponse(
                    success=True,
                    status="INITIATED",
                    external_ref_id=external_ref_id,
                    provider_txn_id=str(data.get("txnId") or data.get("requestId") or ""),
                    raw_payload=data,
                )
            else:
                err_msg = data.get("errorMessage") or data.get("error_description") or f"HTTP {response.status_code}: {response.text[:200]}"
                err_code = data.get("errorCode") or str(response.status_code)
                logger.warning("POS push rejected by Razorpay: %s (%s)", err_msg, err_code)
                return POSResponse(
                    success=False,
                    status="FAILED",
                    external_ref_id=external_ref_id,
                    error_code=err_code,
                    error_message=err_msg,
                    raw_payload=data,
                )

        except requests.exceptions.Timeout:
            logger.error("Timeout pushing payment to POS terminal %s", terminal.terminal_id)
            return POSResponse(
                success=False,
                status="TIMEOUT",
                external_ref_id=external_ref_id,
                error_code="GATEWAY_TIMEOUT",
                error_message="Razorpay POS cloud gateway did not respond within 12 seconds.",
            )
        except Exception as e:
            logger.exception("Unexpected error pushing payment to POS terminal: %s", e)
            return POSResponse(
                success=False,
                status="FAILED",
                external_ref_id=external_ref_id,
                error_code="ADAPTER_EXCEPTION",
                error_message=str(e),
            )

    def check_status(
        self,
        terminal,
        external_ref_id: str,
        provider_txn_id: Optional[str] = None,
    ) -> POSResponse:
        """
        Polls the status of the transaction from Razorpay POS Cloud.
        """
        url = f"{self.base_url}/status"
        params = {
            "username": self.username,
            "appKey": self.app_key,
            "externalRefId": external_ref_id,
        }
        if provider_txn_id:
            params["txnId"] = provider_txn_id

        try:
            response = requests.get(url, params=params, headers=self._get_headers(), timeout=10)
            data = response.json() if response.text else {}

            res_block = data.get("result") or data
            raw_status = (res_block.get("status") or "").upper()

            # Normalize status to CRM standard
            if raw_status in ["SUCCESS", "CAPTURED", "PAID"]:
                norm_status = "SUCCESS"
            elif raw_status in ["PENDING", "INITIATED", "IN_PROGRESS", "AUTHORIZED"]:
                norm_status = "PENDING"
            elif raw_status in ["CANCELLED", "ABORTED"]:
                norm_status = "CANCELLED"
            elif raw_status in ["TIMEOUT", "TIMED_OUT"]:
                norm_status = "TIMEOUT"
            else:
                norm_status = "FAILED"

            # Parse payment mode
            raw_mode = (res_block.get("paymentMode") or "").upper()
            if "UPI" in raw_mode or "QR" in raw_mode:
                norm_mode = "UPI_QR"
            elif "TAP" in raw_mode or "NFC" in raw_mode or "CONTACTLESS" in raw_mode:
                norm_mode = "CARD_TAP"
            elif "DIP" in raw_mode or "CHIP" in raw_mode:
                norm_mode = "CARD_CHIP"
            elif "SWIPE" in raw_mode or "MAG" in raw_mode:
                norm_mode = "CARD_SWIPE"
            else:
                norm_mode = "CARD_CHIP" if "CARD" in raw_mode else "OTHER"

            # Masked card last 4 digits
            card_no = res_block.get("maskedCardNumber") or res_block.get("cardNumber") or ""
            card_last4 = card_no[-4:] if len(card_no) >= 4 else None

            return POSResponse(
                success=(norm_status == "SUCCESS"),
                status=norm_status,
                external_ref_id=external_ref_id,
                provider_txn_id=str(res_block.get("txnId") or res_block.get("id") or ""),
                rrn=res_block.get("rrn"),
                auth_code=res_block.get("approvalCode") or res_block.get("authCode"),
                payment_mode=norm_mode,
                card_network=res_block.get("cardBrand"),
                card_type=res_block.get("cardType"),
                card_last4=card_last4,
                error_code=data.get("errorCode"),
                error_message=data.get("errorMessage"),
                raw_payload=data,
            )

        except Exception as e:
            logger.error("Error querying POS status for ref %s: %s", external_ref_id, e)
            return POSResponse(
                success=False,
                status="PENDING",  # Keep pending on temporary communication error
                external_ref_id=external_ref_id,
                error_code="QUERY_ERROR",
                error_message=str(e),
            )

    def cancel_payment(
        self,
        terminal,
        external_ref_id: str,
        reason: Optional[str] = None,
    ) -> POSResponse:
        """
        Cancels an active payment session on the physical terminal.
        """
        url = f"{self.base_url}/cancel"
        payload = {
            "username": self.username,
            "appKey": self.app_key,
            "externalRefId": external_ref_id,
            "terminalId": terminal.terminal_id,
            "reason": reason or "Cashier aborted payment from CRM",
        }
        try:
            response = requests.post(url, json=payload, headers=self._get_headers(), timeout=10)
            data = response.json() if response.text else {}
            return POSResponse(
                success=data.get("success", False),
                status="CANCELLED",
                external_ref_id=external_ref_id,
                raw_payload=data,
            )
        except Exception as e:
            return POSResponse(
                success=False,
                status="FAILED",
                external_ref_id=external_ref_id,
                error_message=str(e),
            )

    def void_transaction(
        self,
        terminal,
        external_ref_id: str,
        rrn: str,
        reason: Optional[str] = None,
    ) -> POSResponse:
        """
        Reverses a same-day pre-settlement authorization before midnight.
        """
        url = f"{self.base_url}/cancel"
        payload = {
            "username": self.username,
            "appKey": self.app_key,
            "externalRefId": external_ref_id,
            "rrn": rrn,
            "terminalId": terminal.terminal_id,
            "reason": reason or "Customer requested immediate cancellation",
        }
        try:
            response = requests.post(url, json=payload, headers=self._get_headers(), timeout=10)
            data = response.json() if response.text else {}
            return POSResponse(
                success=data.get("success", False),
                status="VOIDED" if data.get("success") else "FAILED",
                external_ref_id=external_ref_id,
                rrn=rrn,
                raw_payload=data,
            )
        except Exception as e:
            return POSResponse(
                success=False,
                status="FAILED",
                external_ref_id=external_ref_id,
                error_message=str(e),
            )

