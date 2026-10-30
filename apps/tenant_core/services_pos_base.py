"""
apps/tenant_core/services_pos_base.py — Common POS Hardware Adapter Interface

Defines the provider-agnostic interface for Point of Sale (POS) hardware devices.
Any physical terminal provider (Razorpay, Pine Labs, Paytm) must inherit from BasePOSAdapter.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from decimal import Decimal


class POSResponse:
    """
    Normalized response structure returned by any POS provider adapter.
    Translates provider-specific JSON responses into a single common format for the CRM.
    """
    def __init__(
        self,
        success: bool,
        status: str,  # INITIATED, PENDING, SUCCESS, FAILED, CANCELLED, TIMEOUT, VOIDED
        external_ref_id: str,
        provider_txn_id: Optional[str] = None,
        rrn: Optional[str] = None,
        auth_code: Optional[str] = None,
        payment_mode: Optional[str] = None,  # CARD_CHIP, CARD_TAP, CARD_SWIPE, UPI_QR
        card_network: Optional[str] = None,
        card_type: Optional[str] = None,
        card_last4: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
    ):
        self.success = success
        self.status = status
        self.external_ref_id = external_ref_id
        self.provider_txn_id = provider_txn_id
        self.rrn = rrn
        self.auth_code = auth_code
        self.payment_mode = payment_mode
        self.card_network = card_network
        self.card_type = card_type
        self.card_last4 = card_last4
        self.error_code = error_code
        self.error_message = error_message
        self.raw_payload = raw_payload or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "external_ref_id": self.external_ref_id,
            "provider_txn_id": self.provider_txn_id,
            "rrn": self.rrn,
            "auth_code": self.auth_code,
            "payment_mode": self.payment_mode,
            "card_network": self.card_network,
            "card_type": self.card_type,
            "card_last4": self.card_last4,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class BasePOSAdapter(ABC):
    """
    Abstract Base Class for all POS Providers.
    Ensures that adding a new provider (Pine Labs, Paytm) requires ZERO changes to core CRM billing logic.
    """

    def __init__(self, provider_config):
        self.config = provider_config

    @abstractmethod
    def push_payment(
        self,
        terminal,
        amount: Decimal,
        external_ref_id: str,
        customer_mobile: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> POSResponse:
        """
        Sends an in-person charge request to wake up the physical card machine.
        """
        pass

    @abstractmethod
    def check_status(
        self,
        terminal,
        external_ref_id: str,
        provider_txn_id: Optional[str] = None,
    ) -> POSResponse:
        """
        Queries the current status of an in-flight or completed transaction on the terminal.
        """
        pass

    @abstractmethod
    def cancel_payment(
        self,
        terminal,
        external_ref_id: str,
        reason: Optional[str] = None,
    ) -> POSResponse:
        """
        Aborts an active payment prompt on the terminal screen.
        """
        pass

    @abstractmethod
    def void_transaction(
        self,
        terminal,
        external_ref_id: str,
        rrn: str,
        reason: Optional[str] = None,
    ) -> POSResponse:
        """
        Performs a same-day pre-settlement void on the terminal (before midnight settlement).
        """
        pass

