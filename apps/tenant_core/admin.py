from django.contrib import admin
from .models_org import Organization, CompanyEntity, Location, Branch
from .models_users import TenantUser, Department, UserBranch, UserDepartment
from .models_rbac import Role, RoleAssignment, ModuleCatalog, Permission
from .models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from .models_privacy import ProcessingPurpose, ConsentRecord, PrivacyRequest, TenantAuditEvent
from .models_infra import File, Integration, LegacyEntityMap


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'status', 'country', 'created_at']
    list_filter = ['status', 'country']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'area', 'status']
    list_filter = ['status']


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ['name', 'location', 'status', 'capacity']
    list_filter = ['status']


@admin.register(TenantUser)
class TenantUserAdmin(admin.ModelAdmin):
    list_display = ['email', 'first_name', 'last_name', 'status', 'organization']
    list_filter = ['status']
    search_fields = ['email', 'first_name', 'last_name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'scope', 'is_system', 'is_active']
    list_filter = ['scope', 'is_system', 'is_active']


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'branch', 'is_active', 'assigned_at']
    list_filter = ['is_active']
    readonly_fields = ['id', 'assigned_at']


@admin.register(TenantAuditEvent)
class TenantAuditEventAdmin(admin.ModelAdmin):
    list_display = ['action', 'resource_type', 'resource_id', 'actor_email', 'created_at']
    list_filter = ['action']
    readonly_fields = ['id', 'created_at']
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


admin.site.register(CompanyEntity)
admin.site.register(Department)
admin.site.register(UserBranch)
admin.site.register(UserDepartment)
admin.site.register(ModuleCatalog)
admin.site.register(Permission)
admin.site.register(OrganizationSettings)
admin.site.register(BranchSettings)
admin.site.register(NotificationTemplate)
admin.site.register(ProcessingPurpose)
admin.site.register(ConsentRecord)
admin.site.register(PrivacyRequest)
admin.site.register(Integration)
admin.site.register(LegacyEntityMap)
