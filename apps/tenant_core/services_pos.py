"""
apps/tenant_core/services_pos.py — Central POS Payment Orchestrator Service

Provides the authoritative entry point for all physical card machine transactions in the CRM.
Features:
- Provider-independent routing (delegates to RazorpayPOSAdapter, PineLabsAdapter, etc.)
- Atomic order locking (select_for_update) to prevent double charges
- Guaranteed idempotency via external_ref_id
- Seamless completion: calls CommerceService.record_payment() to finalize orders and issue invoices.
"""

import uuid
import logging
from decimal import Decimal
from typing import Tuple, Optional, Dict, Any
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_org import Branch
from .models_commerce import Order, PaymentTransaction
from .models_users import TenantUser
from .models_pos import POSTerminal, POSTransaction, POSProviderConfig
from .services_pos_base import BasePOSAdapter, POSResponse
from .services_pos_razorpay import RazorpayPOSAdapter
from .services_commerce import CommerceService

logger = logging.getLogger(__name__)


class POSAdapterFactory:
    """
    Factory resolving the appropriate hardware adapter based on provider_code.
    Enables adding Pine Labs or Paytm in the future with zero changes to caller code.
    """
    @classmethod
    def get_adapter(cls, provider_config: POSProviderConfig) -> BasePOSAdapter:
        code = (provider_config.provider_code or "").upper()
        if code == "RAZORPAY":
            return RazorpayPOSAdapter(provider_config)
        # Future providers plug in here:
        # elif code == "PINELABS":
        #     return PineLabsPOSAdapter(provider_config)
        # elif code == "PAYTM":
        #     return PaytmPOSAdapter(provider_config)
        else:
            raise ValidationError(f"Unsupported POS provider: {code}")


class POSOrchestratorService:
    """
    Authoritative backend orchestrator for all in-person front-desk card & UPI payments.
    """

    @classmethod
    def initiate_pos_payment(
        cls,
        order_id: str,
        terminal_id: Optional[str] = None,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> POSTransaction:
        """
        Dispatches a payment prompt to the physical POS machine assigned to the counter/branch.
        Enforces atomic locking to prevent race conditions or duplicate machine pushes.
        """
        alias = db_alias or "default"

        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).select_for_update().get(id=order_id)

            if order.status in ["PAID", "CANCELLED", "REFUNDED"]:
                raise ValidationError(f"Cannot accept payment for order with status {order.status}.")

            # 1. Concurrency Check: Ensure no active POS session is already in-flight for this order
            active_tx = POSTransaction.objects.using(alias).filter(
                order=order,
                status__in=["INITIATED", "PENDING"]
            ).first()
            if active_tx:
                # If transaction was initiated less than 90 seconds ago, reject duplicate push
                age_seconds = (timezone.now() - active_tx.created_at).total_seconds()
                if age_seconds < 90:
                    raise ValidationError(
                        f"A payment is already active on terminal {active_tx.terminal.name}. "
                        f"Please wait for customer to finish or cancel that transaction first."
                    )
                else:
                    # Mark expired session as timed out
                    active_tx.status = "TIMEOUT"
                    active_tx.save(using=alias)

            # 2. Resolve Physical Terminal
            terminal = None
            if terminal_id:
                terminal = POSTerminal.objects.using(alias).filter(
                    id=terminal_id, status="ACTIVE"
                ).first()
                if not terminal:
                    terminal = POSTerminal.objects.using(alias).filter(
                        terminal_id=terminal_id, status="ACTIVE"
                    ).first()

            if not terminal:
                # Fall back to branch default or first active terminal at order branch
                terminal = POSTerminal.objects.using(alias).filter(
                    branch=order.branch, status="ACTIVE"
                ).order_by("-is_default_for_branch", "created_at").first()

            if not terminal:
                raise ValidationError(
                    f"No active POS card machine configured for branch '{order.branch.name}'. "
                    f"Please register a POS terminal in Settings."
                )

            # 3. Generate Cryptographically Unique Reference ID (Enforces Idempotency)
            external_ref_id = f"POS-{order.order_number}-{uuid.uuid4().hex[:8].upper()}"

            # 4. Create Initial POSTransaction record
            pos_tx = POSTransaction(
                organization=order.branch.organization,
                branch=order.branch,
                terminal=terminal,
                order=order,
                external_ref_id=external_ref_id,
                amount=order.total_amount,
                currency=order.currency,
                status="INITIATED",
                initiated_by_user=actor,
            )
            pos_tx.save(using=alias)

        # 5. Outside the order lock: Push request to the Cloud Adapter
        customer_phone = ""
        if order.user_profile and order.user_profile.phone_number:
            customer_phone = order.user_profile.phone_number
        elif order.lead and order.lead.phone_number:
            customer_phone = order.lead.phone_number

        adapter = POSAdapterFactory.get_adapter(terminal.provider_config)
        response: POSResponse = adapter.push_payment(
            terminal=terminal,
            amount=pos_tx.amount,
            external_ref_id=pos_tx.external_ref_id,
            customer_mobile=customer_phone,
            options={"print_receipt": True},
        )

        # 6. Update POSTransaction with push results
        with transaction.atomic(using=alias):
            pos_tx = POSTransaction.objects.using(alias).select_for_update().get(id=pos_tx.id)
            pos_tx.raw_response = response.raw_payload
            if response.success:
                pos_tx.status = "PENDING"
                pos_tx.provider_reference_id = response.provider_txn_id
            else:
                pos_tx.status = "FAILED"
                pos_tx.error_code = response.error_code
                pos_tx.error_message = response.error_message
            pos_tx.save(using=alias)

        return pos_tx

    @classmethod
    def poll_and_finalize_status(
        cls,
        external_ref_id: str,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[POSTransaction, Optional[PaymentTransaction]]:
        """
        Polls the POS terminal cloud status. If payment has succeeded, atomically completes
        the order and issues the invoice via CommerceService.
        """
        alias = db_alias or "default"

        pos_tx = POSTransaction.objects.using(alias).filter(external_ref_id=external_ref_id).first()
        if not pos_tx:
            raise ValidationError(f"POS Transaction '{external_ref_id}' not found.")

        # If already finalized, return immediately
        if pos_tx.status in ["SUCCESS", "VOIDED"]:
            return pos_tx, pos_tx.payment_transaction

        # 1. Query Provider Cloud Adapter
        adapter = POSAdapterFactory.get_adapter(pos_tx.terminal.provider_config)
        resp: POSResponse = adapter.check_status(
            terminal=pos_tx.terminal,
            external_ref_id=pos_tx.external_ref_id,
            provider_txn_id=pos_tx.provider_reference_id,
        )

        if resp.status == pos_tx.status:
            return pos_tx, None

        # 2. Status Changed: Update POSTransaction
        with transaction.atomic(using=alias):
            pos_tx = POSTransaction.objects.using(alias).select_for_update().get(id=pos_tx.id)
            pos_tx.status = resp.status
            pos_tx.raw_response = resp.raw_payload

            if resp.provider_txn_id:
                pos_tx.provider_reference_id = resp.provider_txn_id
            if resp.rrn:
                pos_tx.rrn = resp.rrn
            if resp.auth_code:
                pos_tx.auth_code = resp.auth_code
            if resp.payment_mode:
                pos_tx.payment_mode = resp.payment_mode
            if resp.card_network:
                pos_tx.card_network = resp.card_network
            if resp.card_type:
                pos_tx.card_type = resp.card_type
            if resp.card_last4:
                pos_tx.card_last4 = resp.card_last4
            if resp.error_code:
                pos_tx.error_code = resp.error_code
            if resp.error_message:
                pos_tx.error_message = resp.error_message

            pay_txn = None
            if resp.status == "SUCCESS":
                # 3. Finalize Commerce Order & Issue Invoice
                pay_txn, _ = CommerceService.record_payment(
                    order_id=str(pos_tx.order_id),
                    amount=pos_tx.amount,
                    provider=pos_tx.terminal.provider_config.provider_code,
                    payment_method=pos_tx.payment_mode or "CARD_POS",
                    provider_transaction_id=pos_tx.rrn or pos_tx.provider_reference_id,
                    idempotency_key=pos_tx.external_ref_id,
                    metadata={
                        "source": "POS_MACHINE",
                        "terminal_id": pos_tx.terminal.terminal_id,
                        "device_serial_no": pos_tx.terminal.device_serial_no,
                        "rrn": pos_tx.rrn,
                        "auth_code": pos_tx.auth_code,
                        "card_last4": pos_tx.card_last4,
                        "card_network": pos_tx.card_network,
                    },
                    actor=actor or pos_tx.initiated_by_user,
                    db_alias=alias,
                )
                pos_tx.payment_transaction = pay_txn

            pos_tx.save(using=alias)

        return pos_tx, pay_txn

    @classmethod
    def cancel_payment(
        cls,
        external_ref_id: str,
        reason: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> POSTransaction:
        """
        Cancels an active payment session on the physical machine.
        """
        alias = db_alias or "default"
        pos_tx = POSTransaction.objects.using(alias).filter(external_ref_id=external_ref_id).first()
        if not pos_tx:
            raise ValidationError(f"POS Transaction '{external_ref_id}' not found.")

        if pos_tx.status in ["SUCCESS", "VOIDED"]:
            raise ValidationError(f"Cannot cancel a transaction that is already {pos_tx.status}.")

        adapter = POSAdapterFactory.get_adapter(pos_tx.terminal.provider_config)
        adapter.cancel_payment(
            terminal=pos_tx.terminal,
            external_ref_id=pos_tx.external_ref_id,
            reason=reason,
        )

        pos_tx.status = "CANCELLED"
        pos_tx.error_message = reason or "Cancelled by user"
        pos_tx.save(using=alias)
        return pos_tx

