from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User, RoleDefinition, PermissionDefinition, RolePermission


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0
    autocomplete_fields = ('permission',)
    fields = ('permission', 'granted')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        'id', 'user_badge', 'role_badge', 'tenant_badge', 
        'active_loc_display', 'status_badge', 'is_active', 'is_staff', 'date_joined'
    )
    list_filter = ('role', 'status', 'is_active', 'is_staff', 'is_superuser', 'tenant', 'date_joined')
    search_fields = ('id', 'email', 'first_name', 'last_name', 'phone', 'tenant__name')
    ordering = ('email',)
    readonly_fields = ('date_joined', 'last_login')

    fieldsets = (
        ('Account Credentials', {
            'fields': ('id', 'email', 'password')
        }),
        ('Personal Profile', {
            'fields': ('first_name', 'last_name', 'phone', 'avatar_url', 'emergency_contact')
        }),
        ('Tenancy & RBAC Roles', {
            'fields': (
                'tenant', 'role', 'role_definition', 'allowed_locations', 
                'active_location', 'status', 'is_active', 'is_staff', 'is_superuser'
            )
        }),
        ('Session & Activity Timestamps', {
            'fields': ('last_login', 'date_joined'),
            'classes': ('collapse',)
        }),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('id', 'email', 'password', 'tenant', 'role', 'is_staff', 'is_superuser'),
        }),
    )

    def user_badge(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip() or obj.email
        return format_html(f'<b>{name}</b><br><span style="color: #64748b; font-size: 11px;">{obj.email}</span>')
    user_badge.short_description = 'User Profile'

    def role_badge(self, obj):
        colors = {
            'Super Admin': 'background: #dc2626; color: white;',
            'Studio Owner / Admin': 'background: #7c3aed; color: white;',
            'Studio Manager': 'background: #2563eb; color: white;',
            'Sales': 'background: #059669; color: white;',
            'Personal Trainer / Coach': 'background: #0891b2; color: white;',
            'Front Desk': 'background: #d97706; color: white;',
            'Finance Officer': 'background: #475569; color: white;',
        }
        style = colors.get(obj.role, 'background: #4b5563; color: white;')
        return format_html(f'<span style="{style} padding: 2px 7px; border-radius: 6px; font-weight: 600; font-size: 11px;">{obj.role}</span>')
    role_badge.short_description = 'Role'

    def tenant_badge(self, obj):
        if not obj.tenant:
            return format_html('<span style="color: #94a3b8; font-style: italic;">(Platform Super Admin)</span>')
        return format_html(f'<b>{obj.tenant.name}</b>')
    tenant_badge.short_description = 'Tenant'

    def active_loc_display(self, obj):
        return obj.active_location.name if obj.active_location else '-'
    active_loc_display.short_description = 'Active Location'

    def status_badge(self, obj):
        color = '#059669' if obj.status == 'Active' else '#dc2626'
        return format_html(f'<span style="color: {color}; font-weight: bold; font-size: 11px;">● {obj.status}</span>')
    status_badge.short_description = 'Status'


@admin.register(RoleDefinition)
class RoleDefinitionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'system_badge', 'tenant', 'perms_count', 'created_at')
    list_filter = ('is_system', 'tenant', 'created_at')
    search_fields = ('id', 'name', 'code', 'description')
    ordering = ('name',)
    inlines = [RolePermissionInline]

    def system_badge(self, obj):
        if obj.is_system:
            return format_html('<span style="background: #e0e7ff; color: #3730a3; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">SYSTEM TEMPLATE</span>')
        return format_html('<span style="background: #f1f5f9; color: #475569; padding: 2px 6px; border-radius: 4px; font-size: 11px;">TENANT CUSTOM</span>')
    system_badge.short_description = 'Scope'

    def perms_count(self, obj):
        count = obj.permissions.filter(granted=True).count()
        return f"{count} Granted"
    perms_count.short_description = 'Permissions'


@admin.register(PermissionDefinition)
class PermissionDefinitionAdmin(admin.ModelAdmin):
    list_display = ('id', 'module_badge', 'action_badge', 'label', 'description')
    list_filter = ('module', 'action')
    search_fields = ('id', 'module', 'action', 'label', 'description')
    ordering = ('module', 'action', 'id')

    def module_badge(self, obj):
        colors = {
            'CRM': 'background: #fef3c7; color: #92400e;',
            'Members': 'background: #dbeafe; color: #1e40af;',
            'Operations': 'background: #e0e7ff; color: #3730a3;',
            'Finance': 'background: #d1fae5; color: #065f46;',
            'Administration': 'background: #f3e8ff; color: #6b21a8;',
            'Platform': 'background: #fee2e2; color: #991b1b;',
        }
        style = colors.get(obj.module, 'background: #f1f5f9; color: #475569;')
        return format_html(f'<span style="{style} padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.module}</span>')
    module_badge.short_description = 'Module'

    def action_badge(self, obj):
        return format_html(f'<code style="font-size: 11px; background: #f8fafc; padding: 1px 4px; border: 1px solid #e2e8f0; border-radius: 3px;">{obj.action}</code>')
    action_badge.short_description = 'Action'


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    list_display = ('id', 'role', 'permission_display', 'granted_badge')
    list_filter = ('granted', 'role', 'permission__module')
    search_fields = ('role__name', 'permission__label', 'permission__id')
    autocomplete_fields = ('role', 'permission')

    def permission_display(self, obj):
        return f"{obj.permission.module} • {obj.permission.label} ({obj.permission.id})"
    permission_display.short_description = 'Permission'

    def granted_badge(self, obj):
        if obj.granted:
            return format_html('<span style="color: #059669; font-weight: bold;">✔ GRANTED</span>')
        return format_html('<span style="color: #94a3b8;">✖ DENIED</span>')
    granted_badge.short_description = 'Status'
