import logging
import uuid
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from apps.tenant_core.context import tenant_database_context
from config.routers import TenantRoutingError

# Models
from .models_org import Organization, CompanyEntity, Location, Branch
from .models_users import TenantUser, Department, UserBranch, UserDepartment
from .models_workforce import UserProfile
from .models_rbac import Role, RoleAssignment, ModuleCatalog, Permission
from .models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from .models_privacy import ProcessingPurpose, ConsentRecord, PrivacyRequest, TenantAuditEvent
from .models_infra import File, Integration, LegacyEntityMap
from .models_catalog import (
    ProgramCategory,
    ProgramType,
    Program,
    Package,
    PackageVersion,
    PackagePrice,
    PackageBranchAvailability,
    TermsDocument,
    TermsDocumentVersion,
)
from .models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassPrice,
    ClassBranchAvailability,
    ClassScheduleRule,
    ClassOccurrence,
    ClassOccurrenceTrainer,
    ClassContentItem,
    ClassScheduleImportBatch,
)
from .models_bookings import Booking
from .models_crm import (
    LeadSource,
    Lead,
    LeadAttribution,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    TrialBooking,
    TrialStatusHistory,
    LeadConversion,
    SalesFollowupTask,
    LeadCommercialProfile,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
    CRMAgentAssignmentConfig,
    IntakeForm,
    IntakeSubmission,
)
from .models_attention import CRMAttentionPolicy
from .models_communication import CommunicationMessage, CommunicationStatusEvent
from .models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)
from .models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRedemption,
)
from .models_commerce import (
    Order,
    OrderItem,
    PaymentTransaction,
    Refund,
    MemberInvoice,
    PaymentLink,
    PaymentLinkEvent,
)
from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
)
from .models_audit_outbox import (
    BusinessAuditEvent,
    DomainOutboxEvent,
    IdempotencyRecord,
    ReasonCode,
)

logger = logging.getLogger(__name__)


def get_admin_active_tenant(request):
    """
    Resolves the active tenant for Django Admin session.
    1. Query param: ?tenant=<uuid_or_slug>
    2. Session key: request.session['admin_tenant_id']
    3. Fallback: First active Tenant in Master DB with active data source
    """
    from apps.master.models_tenant import Tenant

    tenant_param = request.GET.get('tenant') if hasattr(request, 'GET') else None
    if tenant_param:
        try:
            tenant_uuid = uuid.UUID(str(tenant_param).strip())
            tenant = Tenant.objects.using('default').filter(id=tenant_uuid, status='ACTIVE').first()
        except (ValueError, AttributeError):
            tenant = Tenant.objects.using('default').filter(slug=tenant_param, status='ACTIVE').first()
        if tenant:
            if hasattr(request, 'session'):
                request.session['admin_tenant_id'] = str(tenant.id)
            return tenant

    if hasattr(request, 'session'):
        session_tid = request.session.get('admin_tenant_id')
        if session_tid:
            try:
                tenant = Tenant.objects.using('default').filter(id=session_tid, status='ACTIVE').first()
                if tenant:
                    return tenant
            except Exception:
                pass

    tenant = Tenant.objects.using('default').filter(
        status='ACTIVE',
        data_source__status='ACTIVE'
    ).first()
    if tenant and hasattr(request, 'session'):
        request.session['admin_tenant_id'] = str(tenant.id)
    return tenant


class TenantModelAdmin(admin.ModelAdmin):
    """
    Base ModelAdmin for all tenant-scoped models in apps.tenant_core.
    Guarantees that changelist, changeform, delete, history, and autocomplete queries
    execute within the active tenant's isolated database context to avoid fail-closed 403.
    """

    def _run_in_tenant_context(self, request, target_func, *args, **kwargs):
        tenant = get_admin_active_tenant(request)
        if not tenant:
            messages.warning(
                request,
                "No active tenant database context found. Please ensure at least one tenant is ACTIVE in the Master database."
            )
            return HttpResponseRedirect(reverse('admin:index'))
        try:
            with tenant_database_context(tenant.id):
                response = target_func(*args, **kwargs)
                if hasattr(response, 'render') and callable(response.render):
                    response.render()
                return response
        except TenantRoutingError as exc:
            messages.error(request, f"Tenant database routing error: {exc}")
            return HttpResponseRedirect(reverse('admin:index'))

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        tenant = get_admin_active_tenant(request)
        if tenant:
            extra_context['active_tenant'] = tenant
            extra_context['title'] = f"{self.model._meta.verbose_name_plural.capitalize()} (Tenant: {tenant.name})"
        return self._run_in_tenant_context(
            request, super().changelist_view, request, extra_context=extra_context
        )

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        tenant = get_admin_active_tenant(request)
        if tenant:
            extra_context['active_tenant'] = tenant
        return self._run_in_tenant_context(
            request, super().changeform_view, request, object_id=object_id, form_url=form_url, extra_context=extra_context
        )

    def delete_view(self, request, object_id, extra_context=None):
        return self._run_in_tenant_context(
            request, super().delete_view, request, object_id=object_id, extra_context=extra_context
        )

    def history_view(self, request, object_id, extra_context=None):
        return self._run_in_tenant_context(
            request, super().history_view, request, object_id=object_id, extra_context=extra_context
        )

    def autocomplete_view(self, request):
        return self._run_in_tenant_context(
            request, super().autocomplete_view, request
        )


class ReadOnlyVerificationAdmin(TenantModelAdmin):
    """
    Read-only verification ModelAdmin for historical, commercial,
    and audit records. Prevents manual addition and deletion, and marks all
    concrete fields as readonly in the detail view so data can be inspected
    safely without risking accidental mutations.
    """
    list_per_page = 25

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # Allow viewing the changeform detail page
        return True

    def get_readonly_fields(self, request, obj=None):
        fields = [f.name for f in self.model._meta.fields]
        for ro in self.readonly_fields:
            if ro not in fields:
                fields.append(ro)
        return fields

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['show_save'] = False
        extra_context['show_save_and_continue'] = False
        extra_context['show_save_and_add_another'] = False
        return super().changeform_view(request, object_id=object_id, form_url=form_url, extra_context=extra_context)

    def save_model(self, request, obj, form, change):
        # Fail-safe to ensure no accidental mutations occur via admin
        pass


# ============================================================================
# Core Organization & Users
# ============================================================================

@admin.register(Organization)
class OrganizationAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'status', 'country', 'created_at']
    list_filter = ['status', 'country']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Location)
class LocationAdmin(TenantModelAdmin):
    list_display = ['name', 'city', 'area', 'status']
    list_filter = ['status']


@admin.register(Branch)
class BranchAdmin(TenantModelAdmin):
    list_display = ['name', 'location', 'status', 'capacity']
    list_filter = ['status']


@admin.register(TenantUser)
class TenantUserAdmin(TenantModelAdmin):
    list_display = ['email', 'first_name', 'last_name', 'status', 'organization']
    list_filter = ['status']
    search_fields = ['email', 'first_name', 'last_name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Role)
class RoleAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'scope', 'is_system', 'is_active']
    list_filter = ['scope', 'is_system', 'is_active']


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(TenantModelAdmin):
    list_display = ['user', 'role', 'branch', 'is_active', 'assigned_at']
    list_filter = ['is_active']
    readonly_fields = ['id', 'assigned_at']


@admin.register(TenantAuditEvent)
class TenantAuditEventAdmin(TenantModelAdmin):
    list_display = ['action', 'resource_type', 'resource_id', 'actor_email', 'created_at']
    list_filter = ['action']
    readonly_fields = ['id', 'created_at']

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================================
# Catalog & Programs (Module D & C)
# ============================================================================

@admin.register(ProgramCategory)
class ProgramCategoryAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'status', 'display_order']
    list_filter = ['status']
    search_fields = ['name', 'code']


@admin.register(ProgramType)
class ProgramTypeAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'status', 'display_order']
    list_filter = ['status']
    search_fields = ['name', 'code']


@admin.register(Program)
class ProgramAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'category', 'program_type', 'trial_allowed', 'status']
    list_filter = ['status', 'trial_allowed']
    search_fields = ['name', 'code']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Package)
class PackageAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'program', 'status', 'created_at']
    list_filter = ['status']
    search_fields = ['name', 'code']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(PackageVersion)
class PackageVersionAdmin(TenantModelAdmin):
    list_display = ['package', 'version_number', 'status', 'total_days', 'is_trial_package', 'effective_from', 'published_at']
    list_filter = ['status', 'is_trial_package']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(PackagePrice)
class PackagePriceAdmin(TenantModelAdmin):
    list_display = ['package_version', 'branch', 'base_price', 'display_price', 'currency', 'status', 'effective_from']
    list_filter = ['status', 'currency']


@admin.register(TermsDocument)
class TermsDocumentAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'document_type', 'status', 'created_at']
    list_filter = ['status', 'document_type']


@admin.register(TermsDocumentVersion)
class TermsDocumentVersionAdmin(TenantModelAdmin):
    list_display = ['terms_document', 'version_number', 'status', 'effective_from', 'effective_until']
    list_filter = ['status']


# ============================================================================
# Classes, Schedules & Slots (Module E)
# ============================================================================

@admin.register(ClassCategory)
class ClassCategoryAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'status', 'display_order', 'created_at']
    list_filter = ['status']
    search_fields = ['name', 'code']


@admin.register(ClassTemplate)
class ClassTemplateAdmin(TenantModelAdmin):
    list_display = ['name', 'code', 'category', 'default_duration_minutes', 'default_capacity', 'default_delivery_mode', 'status']
    list_filter = ['status', 'default_delivery_mode']
    search_fields = ['name', 'code']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(ClassPrice)
class ClassPriceAdmin(TenantModelAdmin):
    list_display = ['class_template', 'branch', 'version_number', 'price', 'currency', 'status', 'effective_from']
    list_filter = ['status', 'currency']


@admin.register(ClassBranchAvailability)
class ClassBranchAvailabilityAdmin(TenantModelAdmin):
    list_display = ['class_template', 'branch', 'capacity_override', 'status']
    list_filter = ['status']


@admin.register(ClassScheduleRule)
class ClassScheduleRuleAdmin(TenantModelAdmin):
    list_display = ['class_template', 'branch', 'recurrence_type', 'start_time', 'end_time', 'valid_from', 'valid_until', 'status']
    list_filter = ['status', 'recurrence_type']
    search_fields = ['class_template__name', 'branch__name']


@admin.register(ClassOccurrence)
class ClassOccurrenceAdmin(TenantModelAdmin):
    list_display = ['class_template', 'branch', 'occurrence_date', 'start_at', 'end_at', 'capacity', 'status']
    list_filter = ['status', 'occurrence_date', 'branch']
    search_fields = ['class_template__name', 'branch__name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(ClassOccurrenceTrainer)
class ClassOccurrenceTrainerAdmin(TenantModelAdmin):
    list_display = ['occurrence', 'trainer_profile', 'trainer_role', 'status', 'assigned_at']
    list_filter = ['status', 'trainer_role']


@admin.register(ClassContentItem)
class ClassContentItemAdmin(TenantModelAdmin):
    list_display = ['title', 'content_type', 'status', 'created_at']
    list_filter = ['status', 'content_type']


@admin.register(ClassScheduleImportBatch)
class ClassScheduleImportBatchAdmin(TenantModelAdmin):
    list_display = ['branch', 'import_source', 'status', 'total_rows', 'valid_rows', 'invalid_rows', 'imported_rows', 'created_at']
    list_filter = ['status', 'import_source']


# ============================================================================
# Bookings & Memberships
# ============================================================================

@admin.register(Booking)
class BookingAdmin(TenantModelAdmin):
    list_display = ['booking_number', 'occurrence', 'user_profile', 'branch', 'booking_type', 'status', 'booked_at']
    list_filter = ['status', 'booking_type']
    search_fields = ['booking_number']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Membership)
class MembershipAdmin(TenantModelAdmin):
    list_display = [
        'membership_number', 'user_profile', 'program', 'package',
        'package_version', 'home_branch', 'status', 'start_date', 'end_date', 'created_at'
    ]
    list_filter = ['status', 'home_branch', 'program']
    search_fields = ['membership_number', 'user_profile__user__first_name', 'user_profile__user__last_name']
    list_select_related = ['user_profile', 'program', 'package', 'package_version', 'home_branch']
    readonly_fields = [
        'id', 'created_at', 'updated_at', 'membership_number', 'start_date', 'end_date',
        'purchase_branch', 'source_order', 'source_order_item'
    ]
    list_per_page = 25

    def has_add_permission(self, request):
        # Prevent manual creation; memberships must be created via domain service
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================================
# Section A — CRM Core Admin
# ============================================================================

@admin.register(Lead)
class LeadAdmin(TenantModelAdmin):
    list_display = [
        'id', 'full_name', 'phone_normalized', 'email_normalized',
        'branch', 'lead_source', 'assigned_sales_user', 'current_status',
        'interested_program', 'created_at'
    ]
    list_filter = ['current_status', 'branch', 'lead_source', 'assigned_sales_user', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone_normalized', 'email_normalized']
    list_select_related = ['branch', 'lead_source', 'assigned_sales_user', 'interested_program']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25

    @admin.display(description='Full Name')
    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()


@admin.register(LeadSource)
class LeadSourceAdmin(TenantModelAdmin):
    list_display = ['code', 'name', 'source_type', 'status', 'created_at']
    list_filter = ['source_type', 'status']
    search_fields = ['name', 'code']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(LeadCommercialProfile)
class LeadCommercialProfileAdmin(TenantModelAdmin):
    list_display = ['lead', 'billing_name', 'gst_number', 'pan_number', 'created_at']
    search_fields = ['billing_name', 'gst_number', 'pan_number', 'lead__first_name', 'lead__last_name']
    list_select_related = ['lead']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


# ============================================================================
# Section B — Lead History, Notes & Activities
# ============================================================================

@admin.register(LeadAttribution)
class LeadAttributionAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'lead', 'touch_type', 'platform', 'campaign_name',
        'utm_source', 'utm_medium', 'external_lead_id', 'captured_at'
    ]
    list_filter = ['touch_type', 'platform', 'captured_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'campaign_name', 'utm_source', 'external_lead_id']
    list_select_related = ['lead', 'lead_source']


@admin.register(LeadStatusHistory)
class LeadStatusHistoryAdmin(ReadOnlyVerificationAdmin):
    list_display = ['lead', 'from_status', 'to_status', 'reason_code', 'changed_by_user', 'changed_at']
    list_filter = ['to_status', 'changed_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'reason_code']
    list_select_related = ['lead', 'changed_by_user']


@admin.register(LeadAssignment)
class LeadAssignmentAdmin(TenantModelAdmin):
    list_display = ['lead', 'assigned_to_user', 'assigned_by_user', 'assignment_type', 'status', 'assigned_at']
    list_filter = ['assignment_type', 'status', 'assigned_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'assigned_to_user__first_name', 'assigned_to_user__email']
    list_select_related = ['lead', 'assigned_to_user', 'assigned_by_user']
    readonly_fields = ['id', 'assigned_at', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(LeadNote)
class LeadNoteAdmin(TenantModelAdmin):
    list_display = ['lead', 'note_type', 'note_preview', 'created_by_user', 'created_at']
    list_filter = ['note_type', 'created_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'note_text']
    list_select_related = ['lead', 'created_by_user']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25

    @admin.display(description='Note')
    def note_preview(self, obj):
        return (obj.note_text[:60] + '...') if len(obj.note_text) > 60 else obj.note_text


@admin.register(LeadActivity)
class LeadActivityAdmin(TenantModelAdmin):
    list_display = ['lead', 'activity_type', 'outcome', 'performed_by_user', 'activity_at', 'created_at']
    list_filter = ['activity_type', 'activity_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'outcome', 'notes']
    list_select_related = ['lead', 'performed_by_user']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(SalesFollowupTask)
class SalesFollowupTaskAdmin(TenantModelAdmin):
    list_display = ['lead', 'task_type', 'priority', 'status', 'assigned_to_user', 'due_at', 'outcome', 'created_at']
    list_filter = ['task_type', 'priority', 'status', 'due_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'outcome', 'external_reference']
    list_select_related = ['lead', 'assigned_to_user']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(IntakeForm)
class IntakeFormAdmin(TenantModelAdmin):
    list_display = ['name', 'form_type', 'version_number', 'status', 'effective_from', 'created_at']
    list_filter = ['status', 'form_type']
    search_fields = ['name', 'form_type']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(IntakeSubmission)
class IntakeSubmissionAdmin(ReadOnlyVerificationAdmin):
    list_display = ['intake_form', 'lead', 'user_profile', 'submitted_by_user', 'submitted_at']
    list_filter = ['submitted_at']
    search_fields = ['lead__first_name', 'lead__last_name', 'intake_form__name']
    list_select_related = ['intake_form', 'lead', 'user_profile']


# ============================================================================
# Section C — Trial Management
# ============================================================================

@admin.register(TrialBooking)
class TrialBookingAdmin(TenantModelAdmin):
    list_display = [
        'lead', 'branch', 'trial_type', 'scheduled_start', 'scheduled_end',
        'status', 'confirmation_status', 'confirmed_at', 'created_at'
    ]
    list_filter = ['status', 'confirmation_status', 'branch', 'scheduled_start']
    search_fields = ['lead__first_name', 'lead__last_name', 'lead__phone_normalized', 'notes']
    list_select_related = ['lead', 'branch', 'assigned_trainer_profile']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(TrialStatusHistory)
class TrialStatusHistoryAdmin(ReadOnlyVerificationAdmin):
    list_display = ['trial_booking', 'from_status', 'to_status', 'reason_code', 'changed_by_user', 'changed_at']
    list_filter = ['to_status', 'changed_at']
    search_fields = ['trial_booking__lead__first_name', 'trial_booking__lead__last_name', 'reason_code']
    list_select_related = ['trial_booking', 'changed_by_user']


@admin.register(CRMTrialReminderPolicy)
class CRMTrialReminderPolicyAdmin(TenantModelAdmin):
    list_display = ['organization', 'immediate_whatsapp', 'immediate_email', 'immediate_sms', 'ask_attendance_confirmation', 'created_at']
    list_select_related = ['organization']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


# ============================================================================
# Section D — Communication Verification
# ============================================================================

@admin.register(CommunicationMessage)
class CommunicationMessageAdmin(TenantModelAdmin):
    list_display = [
        'lead', 'channel', 'direction', 'purpose', 'recipient',
        'provider', 'status', 'trigger_type', 'sent_at', 'delivered_at', 'created_at'
    ]
    list_filter = ['channel', 'direction', 'status', 'provider', 'trigger_type', 'created_at']
    search_fields = ['recipient', 'sender_identifier', 'provider_message_id', 'lead__first_name', 'lead__last_name', 'subject']
    list_select_related = ['lead', 'organization']
    readonly_fields = ['id', 'created_at', 'updated_at', 'body_snapshot', 'raw_sender_data']
    list_per_page = 25

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CommunicationStatusEvent)
class CommunicationStatusEventAdmin(ReadOnlyVerificationAdmin):
    list_display = ['message', 'from_status', 'to_status', 'provider_event_id', 'occurred_at', 'created_at']
    list_filter = ['to_status', 'occurred_at']
    search_fields = ['provider_event_id', 'message__recipient']
    list_select_related = ['message']


# ============================================================================
# Section E — Automation
# ============================================================================

@admin.register(AutomationWorkflow)
class AutomationWorkflowAdmin(TenantModelAdmin):
    list_display = ['name', 'domain', 'status', 'current_version', 'created_at', 'updated_at']
    list_filter = ['status', 'domain']
    search_fields = ['name', 'description']
    list_select_related = ['organization', 'current_version']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(AutomationWorkflowVersion)
class AutomationWorkflowVersionAdmin(TenantModelAdmin):
    list_display = ['workflow', 'version_number', 'status', 'trigger_type', 'published_at', 'created_at']
    list_filter = ['status', 'trigger_type']
    search_fields = ['workflow__name', 'trigger_type']
    list_select_related = ['workflow']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(AutomationExecution)
class AutomationExecutionAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'workflow', 'workflow_version', 'trigger_event_type',
        'aggregate_type', 'aggregate_id', 'status', 'started_at', 'completed_at'
    ]
    list_filter = ['status', 'aggregate_type', 'started_at']
    search_fields = ['trigger_event_type', 'aggregate_type', 'workflow__name']
    list_select_related = ['workflow', 'workflow_version']


@admin.register(AutomationStepExecution)
class AutomationStepExecutionAdmin(ReadOnlyVerificationAdmin):
    list_display = ['execution', 'step_id', 'step_type', 'status', 'attempt_count', 'started_at', 'completed_at']
    list_filter = ['status', 'step_type']
    search_fields = ['step_id', 'execution__workflow__name']
    list_select_related = ['execution']


# ============================================================================
# Section F — Attention / SLA Policies
# ============================================================================

@admin.register(CRMAttentionPolicy)
class CRMAttentionPolicyAdmin(TenantModelAdmin):
    list_display = [
        'organization', 'is_enabled', 'sla_breach_attention_enabled',
        'no_followup_attention_enabled', 'overdue_followup_attention_enabled',
        'trial_no_show_attention_enabled', 'unassigned_lead_attention_enabled', 'updated_at'
    ]
    list_filter = ['is_enabled']
    list_select_related = ['organization']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(CRMStageSlaPolicy)
class CRMStageSlaPolicyAdmin(TenantModelAdmin):
    list_display = [
        'organization', 'canonical_stage', 'display_label',
        'response_target_value', 'response_target_unit',
        'is_enabled', 'escalation_enabled'
    ]
    list_filter = ['canonical_stage', 'is_enabled', 'escalation_enabled']
    search_fields = ['display_label']
    list_select_related = ['organization']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(CRMAgentAssignmentConfig)
class CRMAgentAssignmentConfigAdmin(TenantModelAdmin):
    list_display = ['organization', 'allow_all_staff_fallback', 'require_branch_match', 'created_at', 'updated_at']
    list_select_related = ['organization']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


# ============================================================================
# Section G — Discounts, Coupons & Redemptions
# ============================================================================

@admin.register(DiscountCampaign)
class DiscountCampaignAdmin(TenantModelAdmin):
    list_display = [
        'name', 'discount_type', 'discount_value', 'status',
        'valid_from', 'valid_until', 'usage_limit', 'per_user_limit', 'created_at'
    ]
    list_filter = ['status', 'discount_type']
    search_fields = ['name', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(DiscountCode)
class DiscountCodeAdmin(TenantModelAdmin):
    list_display = ['code', 'campaign', 'branch', 'package', 'status', 'created_at']
    list_filter = ['status', 'branch']
    search_fields = ['code', 'campaign__name']
    list_select_related = ['campaign', 'branch', 'package']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(DiscountEligibilityRule)
class DiscountEligibilityRuleAdmin(TenantModelAdmin):
    list_display = [
        'name', 'rule_type', 'priority', 'evaluation_mode',
        'source_package', 'target_package', 'branch', 'status', 'valid_from', 'valid_until'
    ]
    list_filter = ['rule_type', 'status', 'evaluation_mode']
    search_fields = ['name', 'description']
    list_select_related = ['source_package', 'target_package', 'branch']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


@admin.register(DiscountRedemption)
class DiscountRedemptionAdmin(ReadOnlyVerificationAdmin):
    list_display = ['discount_code', 'campaign', 'user_profile', 'order', 'discount_amount', 'redeemed_at']
    list_filter = ['redeemed_at', 'campaign']
    search_fields = ['discount_code__code', 'campaign__name', 'order__order_number']
    list_select_related = ['discount_code', 'campaign', 'user_profile', 'order']


# ============================================================================
# Section H — Lead Conversions
# ============================================================================

@admin.register(LeadConversion)
class LeadConversionAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'lead', 'user_profile', 'order_id', 'membership_id',
        'package_id', 'conversion_source', 'converted_by_user', 'converted_at'
    ]
    list_filter = ['converted_at', 'conversion_source']
    search_fields = ['lead__first_name', 'lead__last_name', 'lead__phone_normalized', 'conversion_source']
    list_select_related = ['lead', 'user_profile', 'converted_by_user']


# ============================================================================
# Section I — Commerce Verification
# ============================================================================

@admin.register(Order)
class OrderAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'order_number', 'lead', 'user_profile', 'branch', 'order_type',
        'status', 'subtotal', 'discount_amount', 'tax_amount', 'total_amount', 'currency', 'created_at'
    ]
    list_filter = ['status', 'order_type', 'source', 'branch', 'created_at']
    search_fields = ['order_number', 'lead__first_name', 'lead__last_name']
    list_select_related = ['lead', 'user_profile', 'branch']


@admin.register(OrderItem)
class OrderItemAdmin(ReadOnlyVerificationAdmin):
    list_display = ['order', 'item_type', 'item_name_snapshot', 'quantity', 'unit_price_snapshot', 'total_amount', 'created_at']
    list_filter = ['item_type', 'created_at']
    search_fields = ['item_name_snapshot', 'order__order_number']
    list_select_related = ['order']


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'id', 'order', 'provider', 'amount', 'currency', 'status',
        'payment_method', 'provider_transaction_id', 'paid_at', 'created_at'
    ]
    list_filter = ['status', 'provider', 'created_at']
    search_fields = ['provider_transaction_id', 'order__order_number', 'idempotency_key']
    list_select_related = ['order', 'user_profile']


@admin.register(Refund)
class RefundAdmin(ReadOnlyVerificationAdmin):
    list_display = ['id', 'order', 'payment_transaction', 'amount', 'status', 'reason_code', 'provider_reference', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['order__order_number', 'provider_reference', 'reason_code']
    list_select_related = ['order', 'payment_transaction']


@admin.register(MemberInvoice)
class MemberInvoiceAdmin(ReadOnlyVerificationAdmin):
    list_display = ['invoice_number', 'order', 'user_profile', 'branch', 'total_amount', 'currency', 'status', 'issued_at', 'created_at']
    list_filter = ['status', 'issued_at', 'branch']
    search_fields = ['invoice_number', 'order__order_number']
    list_select_related = ['order', 'user_profile', 'branch']


@admin.register(PaymentLink)
class PaymentLinkAdmin(ReadOnlyVerificationAdmin):
    list_display = ['id', 'order', 'lead', 'user_profile', 'amount', 'currency', 'status', 'provider', 'expires_at', 'created_at']
    list_filter = ['status', 'provider']
    search_fields = ['order__order_number', 'external_reference']
    list_select_related = ['order', 'lead', 'user_profile']


@admin.register(PaymentLinkEvent)
class PaymentLinkEventAdmin(ReadOnlyVerificationAdmin):
    list_display = ['payment_link', 'event_type', 'provider_reference', 'event_at', 'created_at']
    list_filter = ['event_type', 'event_at']
    search_fields = ['provider_reference', 'payment_link__external_reference']
    list_select_related = ['payment_link']


# ============================================================================
# Section J — Membership Verification
# ============================================================================

@admin.register(MembershipContractSnapshot)
class MembershipContractSnapshotAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'membership', 'package_name_snapshot', 'purchase_price',
        'discount_amount', 'final_amount', 'currency', 'start_date', 'end_date', 'created_at'
    ]
    search_fields = ['membership__membership_number', 'package_name_snapshot']
    list_select_related = ['membership', 'package', 'purchase_branch']


@admin.register(MembershipEntitlement)
class MembershipEntitlementAdmin(ReadOnlyVerificationAdmin):
    list_display = [
        'membership', 'entitlement_type', 'allocated_units', 'consumed_units',
        'is_unlimited', 'status', 'valid_from', 'valid_until'
    ]
    list_filter = ['status', 'entitlement_type', 'is_unlimited']
    search_fields = ['membership__membership_number', 'entitlement_type']
    list_select_related = ['membership']


@admin.register(MembershipEntitlementLedger)
class MembershipEntitlementLedgerAdmin(ReadOnlyVerificationAdmin):
    list_display = ['membership_entitlement', 'transaction_type', 'units', 'balance_after', 'reason_code', 'created_at']
    list_filter = ['transaction_type', 'created_at']
    search_fields = ['membership_entitlement__membership__membership_number', 'reason_code']
    list_select_related = ['membership_entitlement']


@admin.register(MembershipBranchHistory)
class MembershipBranchHistoryAdmin(ReadOnlyVerificationAdmin):
    list_display = ['membership', 'from_branch', 'to_branch', 'change_type', 'effective_at', 'created_at']
    list_filter = ['change_type', 'effective_at']
    search_fields = ['membership__membership_number']
    list_select_related = ['membership', 'from_branch', 'to_branch']


@admin.register(MembershipStatusHistory)
class MembershipStatusHistoryAdmin(ReadOnlyVerificationAdmin):
    list_display = ['membership', 'from_status', 'to_status', 'reason_code', 'changed_at']
    list_filter = ['to_status', 'changed_at']
    search_fields = ['membership__membership_number', 'reason_code']
    list_select_related = ['membership']


# ============================================================================
# Section K — Audit & Outbox Verification
# ============================================================================

@admin.register(BusinessAuditEvent)
class BusinessAuditEventAdmin(ReadOnlyVerificationAdmin):
    list_display = ['action_code', 'module', 'entity_type', 'entity_id', 'actor_type', 'actor_user', 'source_channel', 'occurred_at']
    list_filter = ['module', 'actor_type', 'source_channel', 'occurred_at']
    search_fields = ['action_code', 'entity_type']
    list_select_related = ['branch', 'actor_user']


@admin.register(DomainOutboxEvent)
class DomainOutboxEventAdmin(ReadOnlyVerificationAdmin):
    list_display = ['event_type', 'aggregate_type', 'aggregate_id', 'status', 'attempt_count', 'next_attempt_at', 'published_at', 'created_at']
    list_filter = ['status', 'aggregate_type', 'created_at']
    search_fields = ['event_type', 'aggregate_type']
    list_select_related = ['organization']


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(ReadOnlyVerificationAdmin):
    list_display = ['idempotency_key', 'operation_type', 'resource_type', 'resource_id', 'status', 'expires_at', 'created_at']
    list_filter = ['status', 'operation_type', 'created_at']
    search_fields = ['idempotency_key', 'operation_type', 'resource_type']
    list_select_related = ['actor_user']


@admin.register(ReasonCode)
class ReasonCodeAdmin(TenantModelAdmin):
    list_display = ['code', 'label', 'module', 'action_type', 'status']
    list_filter = ['module', 'action_type', 'status']
    search_fields = ['code', 'label']
    list_select_related = ['organization']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 25


# ============================================================================
# Workforce Profiles (Verification of converted users)
# ============================================================================

@admin.register(UserProfile)
class UserProfileAdmin(TenantModelAdmin):
    list_display = ['member_number', 'user', 'first_name_snapshot', 'last_name_snapshot', 'gender', 'date_of_birth']
    search_fields = ['member_number', 'first_name_snapshot', 'last_name_snapshot', 'user__email']
    list_select_related = ['user']
    readonly_fields = ['id']
    list_per_page = 25


# ============================================================================
# Governance, Settings & Infra (Registered with TenantModelAdmin)
# ============================================================================

admin.site.register(CompanyEntity, TenantModelAdmin)
admin.site.register(Department, TenantModelAdmin)
admin.site.register(UserBranch, TenantModelAdmin)
admin.site.register(UserDepartment, TenantModelAdmin)
admin.site.register(ModuleCatalog, TenantModelAdmin)
admin.site.register(Permission, TenantModelAdmin)
admin.site.register(OrganizationSettings, TenantModelAdmin)
admin.site.register(BranchSettings, TenantModelAdmin)
admin.site.register(NotificationTemplate, TenantModelAdmin)
admin.site.register(ProcessingPurpose, TenantModelAdmin)
admin.site.register(ConsentRecord, TenantModelAdmin)
admin.site.register(PrivacyRequest, TenantModelAdmin)
admin.site.register(Integration, TenantModelAdmin)
admin.site.register(LegacyEntityMap, TenantModelAdmin)
admin.site.register(PackageBranchAvailability, TenantModelAdmin)
