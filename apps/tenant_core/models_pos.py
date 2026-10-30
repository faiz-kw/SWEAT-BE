"""
apps/tenant_core/models_pos.py — Provider-Independent Point of Sale (POS) Architecture

Provides domain models for hardware point of sale terminals and in-person card/UPI transactions:
1. POSProviderConfig: Configures payment providers (Razorpay, Pine Labs, Paytm) securely per organization.
2. POSTerminal: Tracks physical hardware units (TID, S/N) deployed across branches (e.g. Malad West).
3. POSTransaction: Comprehensive audit ledger for all in-person payment sessions, RRNs, and statuses.

Security & Compliance:
- Strictly compliant with PCI-DSS & RBI guidelines.
- ZERO storage of raw card PAN, CVV, or PIN blocks.
- Stores only tokenized provider IDs, 12-digit bank RRNs, masked last 4 card digits, and auth codes.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_commerce import Order, PaymentTransaction


class POSProviderConfig(models.Model):
    """
    Organization-level configuration and credentials for physical POS & payment providers.
    Supports multi-provider architectures (Razorpay, Pine Labs, Paytm) without code changes.
    """
    PROVIDER_CHOICES = [
        ('RAZORPAY', 'Razorpay POS'),
        ('PINELABS', 'Pine Labs Plutus'),
        ('PAYTM', 'Paytm EDC / Soundbox'),
        ('OTHER', 'Other Provider'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='pos_provider_configs'
    )
    provider_code = models.CharField(
        max_length=30, choices=PROVIDER_CHOICES, default='RAZORPAY', db_index=True
    )
    display_name = models.CharField(
        max_length=100, help_text="Friendly label, e.g. 'Razorpay Malad & Andheri Fleet'"
    )
    
    # Provider Gateway Credentials
    api_key_id = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Public Key / Key ID (e.g. rzp_live_...)"
    )
    api_key_secret = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Secret Key for API authentication (never exposed to frontend)"
    )
    merchant_id = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Merchant ID (MID) registered with the acquiring bank/gateway"
    )
    
    # POS Cloud Bridge / Push Credentials (Ezetap / Razorpay POS specific)
    app_key = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Cloud POS Application Key provided by Razorpay/Ezetap"
    )
    username = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="POS service username for cloud adapter dispatch"
    )
    webhook_secret = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Secret for HMAC-SHA256 signature verification on webhooks"
    )
    api_base_url = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Optional custom endpoint URL for demo/sandbox or dedicated proxy"
    )
    
    is_test_mode = models.BooleanField(
        default=False,
        help_text="If True, routes transactions to provider sandbox / simulator"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Controls whether this provider can be selected for new transactions"
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        help_text="Provider-specific extra attributes"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'pos_provider_configs'
        indexes = [
            models.Index(fields=['organization', 'provider_code', 'is_active'], name='idx_pos_prov_org_active'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'provider_code', 'is_test_mode'],
                name='uq_pos_prov_org_code_mode'
            )
        ]

    def __str__(self):
        mode_str = "TEST" if self.is_test_mode else "LIVE"
        return f"{self.display_name} ({self.provider_code} - {mode_str})"


class POSTerminal(models.Model):
    """
    Physical POS terminal hardware inventory assigned to a specific branch counter.
    Maps Bank Terminal ID (TID) and Hardware Serial (S/N) to a gym front desk.
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active / Ready'),
        ('INACTIVE', 'Inactive / Disabled'),
        ('MAINTENANCE', 'Under Maintenance / Hardware Issue'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='pos_terminals'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='pos_terminals'
    )
    provider_config = models.ForeignKey(
        POSProviderConfig, on_delete=models.PROTECT, related_name='terminals',
        help_text="Which payment provider powers this physical terminal"
    )
    name = models.CharField(
        max_length=100, help_text="Cashier-facing label, e.g. 'Malad Front Desk 1'"
    )
    terminal_id = models.CharField(
        max_length=64, db_index=True,
        help_text="Bank-assigned Terminal ID (TID) printed on paper slips"
    )
    device_serial_no = models.CharField(
        max_length=100, db_index=True,
        help_text="Manufacturer Serial Number (S/N) on device barcode sticker"
    )
    model_name = models.CharField(
        max_length=64, blank=True, null=True,
        help_text="e.g. PAX A920, Sunmi P2, PAX D180"
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True
    )
    is_default_for_branch = models.BooleanField(
        default=False,
        help_text="Automatically pre-selected for front-desk staff at this branch"
    )
    last_heartbeat_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of last successful ping or transaction"
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        help_text="Hardware details such as battery level, firmware version, SIM carrier"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'pos_terminals'
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_pos_term_br_status'),
            models.Index(fields=['terminal_id'], name='idx_pos_term_tid'),
            models.Index(fields=['device_serial_no'], name='idx_pos_term_sn'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'terminal_id'],
                name='uq_pos_term_org_tid'
            ),
        ]

    def __str__(self):
        return f"{self.name} (TID: {self.terminal_id}) [{self.branch.name}]"


class POSTransaction(models.Model):
    """
    Audit ledger tracking every physical POS transaction attempt, live status, and settlement details.
    """
    STATUS_CHOICES = [
        ('INITIATED', 'Initiated (Push Sent to Terminal)'),
        ('PENDING', 'Pending (Customer Interaction in Progress)'),
        ('SUCCESS', 'Success (Payment Authorized & Completed)'),
        ('FAILED', 'Failed (Bank Decline / Invalid PIN)'),
        ('CANCELLED', 'Cancelled (Cashier or Member Aborted)'),
        ('TIMEOUT', 'Timed Out (No Customer Response)'),
        ('VOIDED', 'Voided (Same-Day Pre-Settlement Reversal)'),
    ]
    PAYMENT_MODE_CHOICES = [
        ('CARD_CHIP', 'Card Dip (Chip & PIN)'),
        ('CARD_TAP', 'Card Tap (Contactless NFC)'),
        ('CARD_SWIPE', 'Card Swipe (Magnetic Stripe)'),
        ('UPI_QR', 'Dynamic UPI QR on Terminal Screen'),
        ('WALLET', 'Mobile Wallet'),
        ('OTHER', 'Other Offline Mode'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='pos_transactions'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='pos_transactions'
    )
    terminal = models.ForeignKey(
        POSTerminal, on_delete=models.PROTECT, related_name='transactions'
    )
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='pos_transactions'
    )
    payment_transaction = models.ForeignKey(
        PaymentTransaction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pos_transactions',
        help_text="Authoritative commerce record created once payment succeeds"
    )
    external_ref_id = models.CharField(
        max_length=100, unique=True, db_index=True,
        help_text="Unique CRM reference ID sent to the POS to enforce idempotency"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default='INITIATED', db_index=True
    )
    payment_mode = models.CharField(
        max_length=30, choices=PAYMENT_MODE_CHOICES, blank=True, null=True
    )

    # Settlement & Reconciliation Identifiers
    provider_reference_id = models.CharField(
        max_length=100, blank=True, null=True, db_index=True,
        help_text="Gateway transaction ID (e.g. Razorpay pay_... or Ezetap txnId)"
    )
    rrn = models.CharField(
        max_length=64, blank=True, null=True, db_index=True,
        help_text="12-digit Retrieval Reference Number from bank paper slip"
    )
    auth_code = models.CharField(
        max_length=64, blank=True, null=True,
        help_text="Bank approval/auth code"
    )
    card_network = models.CharField(
        max_length=32, blank=True, null=True,
        help_text="VISA, MASTERCARD, RUPAY, AMEX"
    )
    card_type = models.CharField(
        max_length=32, blank=True, null=True,
        help_text="DEBIT, CREDIT"
    )
    card_last4 = models.CharField(
        max_length=4, blank=True, null=True,
        help_text="Masked card last 4 digits only (PCI-DSS compliant)"
    )

    error_code = models.CharField(max_length=64, blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    initiated_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='initiated_pos_transactions'
    )
    raw_request = models.JSONField(
        default=dict, blank=True,
        help_text="Payload sent to the POS provider"
    )
    raw_response = models.JSONField(
        default=dict, blank=True,
        help_text="Last response payload received from the POS provider/webhook"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'pos_transactions'
        indexes = [
            models.Index(fields=['order', 'status'], name='idx_pos_tx_ord_st'),
            models.Index(fields=['external_ref_id'], name='idx_pos_tx_ext_ref'),
            models.Index(fields=['terminal', 'created_at'], name='idx_pos_tx_term_dt'),
            models.Index(fields=['rrn'], name='idx_pos_tx_rrn'),
        ]

    def __str__(self):
        return f"POS Txn {self.external_ref_id} ({self.amount} {self.currency}) [{self.status}]"

