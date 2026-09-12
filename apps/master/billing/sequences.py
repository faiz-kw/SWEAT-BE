"""
Master DB — Concurrency-Safe Invoice Numbering.
Phase 1 Layer 1 — Commercial Billing & Subscription Lifecycle.

Guarantees gapless, concurrency-safe sequential invoice allocation per tenant per calendar year.
Uses SELECT ... FOR UPDATE on tenant_invoice_sequences within an atomic database transaction.
Format is a documented business convention: INV-{YEAR}-{TENANT_CODE_OR_SLUG}-{SEQUENCE:05d}.
"""

import re
from datetime import datetime
from typing import Optional
from django.db import transaction
from django.utils import timezone


def allocate_next_invoice_number(tenant, year: Optional[int] = None) -> str:
    """
    Allocates the next sequential invoice number for a tenant in the given calendar year.
    Thread-safe and multi-worker safe via database row-level locking.
    Must be called inside or will establish an atomic transaction on Master DB ('default').
    """
    from apps.master.models_saas import TenantInvoiceSequence

    current_year = year or timezone.now().year

    with transaction.atomic(using='default'):
        # Acquire row lock
        seq_obj, created = TenantInvoiceSequence.objects.using('default').select_for_update().get_or_create(
            tenant=tenant,
            year=current_year,
            defaults={'last_sequence': 0}
        )

        seq_obj.last_sequence += 1
        seq_obj.save(using='default', update_fields=['last_sequence', 'updated_at'])

        invoice_number = f"INV-{current_year}-{seq_obj.last_sequence:05d}"
        return invoice_number
