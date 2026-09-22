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
from .models_memberships import Membership

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
    list_display = ['membership_number', 'program', 'package', 'user_profile', 'home_branch', 'status', 'start_date', 'end_date']
    list_filter = ['status', 'home_branch']
    search_fields = ['membership_number']
    readonly_fields = ['id', 'created_at', 'updated_at']


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
