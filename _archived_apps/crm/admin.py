from django.contrib import admin
from django.utils.html import format_html
from .models import Lead, LeadActivity


class LeadActivityInline(admin.TabularInline):
    model = LeadActivity
    extra = 0
    fields = ('activity_type', 'summary', 'performed_by', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'lead_info', 'phone', 'location', 'stage_badge', 
        'status_badge', 'score_display', 'assigned_to', 'tenant', 'created_at'
    )
    list_filter = ('stage', 'status', 'source', 'location', 'tenant', 'created_at')
    search_fields = ('id', 'name', 'phone', 'email', 'tenant__name')
    inlines = [LeadActivityInline]

    def lead_info(self, obj):
        return format_html(f'<b>{obj.name}</b><br><span style="color: #64748b; font-size: 11px;">{obj.email}</span>')
    lead_info.short_description = 'Lead'

    def stage_badge(self, obj):
        colors = {
            'New': 'background: #dbeafe; color: #1e40af;',
            'Contacted': 'background: #fef3c7; color: #92400e;',
            'Trial Booked': 'background: #e0e7ff; color: #3730a3;',
            'Trial Completed': 'background: #f3e8ff; color: #6b21a8;',
            'Negotiation': 'background: #ffedd5; color: #c2410c;',
            'Won': 'background: #d1fae5; color: #065f46;',
            'Lost': 'background: #fee2e2; color: #991b1b;',
        }
        style = colors.get(obj.stage, 'background: #f1f5f9; color: #475569;')
        return format_html(f'<span style="{style} padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.stage}</span>')
    stage_badge.short_description = 'Stage'

    def status_badge(self, obj):
        color = '#059669' if obj.status == 'Open' else '#dc2626'
        return format_html(f'<span style="color: {color}; font-weight: bold;">● {obj.status}</span>')
    status_badge.short_description = 'Status'

    def score_display(self, obj):
        color = '#059669' if obj.score >= 70 else ('#d97706' if obj.score >= 40 else '#dc2626')
        return format_html(f'<b style="color: {color}; font-size: 12px;">{obj.score}</b>')
    score_display.short_description = 'Lead Score'


@admin.register(LeadActivity)
class LeadActivityAdmin(admin.ModelAdmin):
    list_display = ('id', 'lead', 'type_badge', 'summary_display', 'performed_by', 'created_at', 'tenant')
    list_filter = ('activity_type', 'tenant', 'created_at')
    search_fields = ('lead__name', 'summary', 'tenant__name')
    readonly_fields = ('created_at',)

    def type_badge(self, obj):
        return format_html(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.activity_type}</span>')
    type_badge.short_description = 'Activity'

    def summary_display(self, obj):
        return obj.summary[:50] + '...' if len(obj.summary) > 50 else obj.summary
    summary_display.short_description = 'Summary'
