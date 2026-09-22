"""
apps/tenant_core/models_attention.py — Layer 2 Module B Phase 7: Stuck Lead & Next Best Action Policy

Stores tenant-configurable flags and threshold parameters governing
attention reason evaluation. Designed with neutral defaults to prevent
unconfigured tenants from triggering false-positive alerts.
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Organization


class CRMAttentionPolicy(models.Model):
    """
    Tenant-configurable settings for the CRM Stuck Lead & Next Best Action Engine.
    Operates strictly within the tenant database schema.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name='crm_attention_policy',
        db_column='organization_id',
    )

    # Master switch
    is_enabled = models.BooleanField(
        default=False,
        help_text="Master switch to activate attention reason calculations for this organization.",
    )

    # Specific attention rule toggles (default False for neutral setup)
    sla_breach_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag leads whose stage SLA has breached according to CRMStageSlaPolicy.",
    )
    no_followup_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag leads with no scheduled follow-up after the applicable stage SLA deadline.",
    )
    overdue_followup_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag leads whose pending follow-up task has passed its due_at deadline.",
    )
    no_response_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag leads with outbound communications awaiting inbound response beyond the configured window.",
    )
    no_response_wait_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Hours to wait after outbound communication before flagging no response. Nullable until configured.",
    )
    trial_not_booked_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag trial-eligible leads without a booked trial after stage SLA deadline.",
    )
    trial_confirmation_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag trials in pending confirmation beyond the configured wait duration.",
    )
    trial_no_show_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag trial no-shows without a completed recovery follow-up.",
    )
    post_trial_followup_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag attended trials without a post-trial sales consultation follow-up.",
    )
    unassigned_lead_attention_enabled = models.BooleanField(
        default=False,
        help_text="Flag leads that have no active assigned sales agent.",
    )
    unassigned_wait_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Minutes after intake before an unassigned lead is flagged. Nullable until configured.",
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'crm_attention_policies'
        verbose_name = 'CRM Attention Policy'
        verbose_name_plural = 'CRM Attention Policies'

    def __str__(self):
        return f"CRMAttentionPolicy(Org={self.organization_id}, Enabled={self.is_enabled})"
