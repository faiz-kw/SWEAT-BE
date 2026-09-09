from django.contrib import admin
from django.utils.html import format_html
from .models import Service, TenantSettings, CustomForm, ApiKey, AuditLog, SecurityPolicy


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category_badge', 'price_display', 'duration_display', 'capacity_display', 'location', 'tenant', 'is_active')
    list_filter = ('category', 'is_active', 'tenant', 'location')
    search_fields = ('id', 'name', 'category', 'description', 'tenant__name')
    ordering = ('category', 'name')

    def category_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.category}</span>')
    category_badge.short_description = 'Category'

    def price_display(self, obj):
        return f"₹{obj.price:,.2f}"
    price_display.short_description = 'Price'

    def duration_display(self, obj):
        return f"{obj.duration_minutes} mins"
    duration_display.short_description = 'Duration'

    def capacity_display(self, obj):
        return f"{obj.capacity} Clients"
    capacity_display.short_description = 'Capacity'


@admin.register(TenantSettings)
class TenantSettingsAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'currency', 'tax_rate_display', 'tax_id_number', 'operating_hours_display', 'booking_window_display')
    search_fields = ('tenant__name', 'tax_id_number')

    def tax_rate_display(self, obj):
        return f"{obj.tax_rate_gst}% GST"
    tax_rate_display.short_description = 'GST Rate'

    def operating_hours_display(self, obj):
        return f"{obj.business_open_time} - {obj.business_close_time}"
    operating_hours_display.short_description = 'Facility Hours'

    def booking_window_display(self, obj):
        return f"{obj.booking_cancellation_window_hours}h Cancellation Window"
    booking_window_display.short_description = 'Policy'


@admin.register(CustomForm)
class CustomFormAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'code', 'mandatory_badge', 'fields_count', 'tenant', 'is_active')
    list_filter = ('is_mandatory', 'is_active', 'tenant')
    search_fields = ('id', 'title', 'code', 'description')

    def mandatory_badge(self, obj):
        if obj.is_mandatory:
            return format_html('<span style="color: #dc2626; font-weight: bold;">MANDATORY</span>')
        return format_html('<span style="color: #64748b;">Optional</span>')
    mandatory_badge.short_description = 'Requirement'

    def fields_count(self, obj):
        count = len(obj.fields_schema) if isinstance(obj.fields_schema, list) else 0
        return f"{count} Fields"
    fields_count.short_description = 'Schema'


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'prefix_display', 'tenant', 'status_display', 'last_used_at', 'created_at')
    list_filter = ('is_active', 'tenant')
    search_fields = ('id', 'name', 'key_prefix', 'tenant__name')
    readonly_fields = ('id', 'created_at')

    def prefix_display(self, obj):
        return format_html(f'<code style="font-size: 11px;">{obj.key_prefix}...</code>')
    prefix_display.short_description = 'Prefix'

    def status_display(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #059669; font-weight: bold;">ACTIVE</span>')
        return format_html('<span style="color: #dc2626; font-weight: bold;">REVOKED</span>')
    status_display.short_description = 'Status'


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'user_email', 'action_badge', 'module_badge', 'entity_display', 'ip_address')
    list_filter = ('action', 'module', 'created_at')
    search_fields = ('user_email', 'entity_id', 'entity_type', 'description', 'ip_address')
    ordering = ('-created_at',)
    readonly_fields = ('id', 'created_at')

    def action_badge(self, obj):
        colors = {
            'CREATE': 'background: #d1fae5; color: #065f46;',
            'UPDATE': 'background: #dbeafe; color: #1e40af;',
            'DELETE': 'background: #fee2e2; color: #991b1b;',
            'LOGIN': 'background: #fef3c7; color: #92400e;',
            'EXPORT': 'background: #f3e8ff; color: #6b21a8;',
        }
        style = colors.get(obj.action.upper(), 'background: #f1f5f9; color: #475569;')
        return format_html(f'<span style="{style} padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.action}</span>')
    action_badge.short_description = 'Action'

    def module_badge(self, obj):
        return format_html(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 5px; border-radius: 3px; font-size: 11px;">{obj.module}</span>')
    module_badge.short_description = 'Module'

    def entity_display(self, obj):
        return f"{obj.entity_type} ({obj.entity_id})"
    entity_display.short_description = 'Entity'


@admin.register(SecurityPolicy)
class SecurityPolicyAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'mfa_badge', 'session_timeout_minutes', 'password_min_length', 'max_failed_attempts_lockout')
    search_fields = ('tenant__name',)

    def mfa_badge(self, obj):
        if obj.enforce_mfa:
            return format_html('<span style="color: #059669; font-weight: bold;">✔ Enforced</span>')
        return format_html('<span style="color: #64748b;">Optional</span>')
    mfa_badge.short_description = '2FA / MFA'
