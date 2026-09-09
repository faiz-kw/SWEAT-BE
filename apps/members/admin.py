from django.contrib import admin
from django.utils.html import format_html
from .models import MembershipPlan, Member, MemberSubscription, Attendance


class MemberSubscriptionInline(admin.TabularInline):
    model = MemberSubscription
    extra = 0
    fields = ('plan', 'start_date', 'end_date', 'amount_paid', 'status')
    show_change_link = True


class AttendanceInline(admin.TabularInline):
    model = Attendance
    extra = 0
    fields = ('location', 'check_in_time', 'method')
    readonly_fields = ('check_in_time',)


@admin.register(MembershipPlan)
class MembershipPlanAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category_badge', 'duration_display', 'price_display', 'is_active', 'tenant')
    list_filter = ('category', 'is_active', 'tenant')
    search_fields = ('id', 'name', 'tenant__name')

    def category_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.category}</span>')
    category_badge.short_description = 'Category'

    def duration_display(self, obj):
        return f"{obj.duration_months} Months"
    duration_display.short_description = 'Duration'

    def price_display(self, obj):
        return f"₹{obj.price:,.2f}"
    price_display.short_description = 'Price (INR)'


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'member_info', 'phone', 'location', 'status_badge', 
        'risk_badge', 'health_score_display', 'primary_coach', 'tenant', 'joined_at'
    )
    list_filter = ('status', 'risk_level', 'gender', 'location', 'tenant', 'joined_at')
    search_fields = ('id', 'name', 'phone', 'email', 'tenant__name')
    readonly_fields = ('id', 'joined_at', 'created_at', 'updated_at')
    inlines = [MemberSubscriptionInline, AttendanceInline]

    fieldsets = (
        ('Member Profile', {
            'fields': ('id', 'name', 'phone', 'email', 'gender', 'date_of_birth', 'avatar_url', 'emergency_contact')
        }),
        ('Tenancy & Location', {
            'fields': ('tenant', 'location', 'primary_coach')
        }),
        ('Status & Engagement Intelligence', {
            'fields': ('status', 'risk_level', 'health_score', 'joined_at')
        }),
        ('Medical & Notes', {
            'fields': ('medical_notes', 'notes'),
            'classes': ('collapse',)
        }),
    )

    def member_info(self, obj):
        return format_html(f'<b>{obj.name}</b><br><span style="color: #64748b; font-size: 11px;">{obj.email}</span>')
    member_info.short_description = 'Member'

    def status_badge(self, obj):
        colors = {
            'Active': 'color: #059669;',
            'Expiring': 'color: #d97706;',
            'Frozen': 'color: #0284c7;',
            'Expired': 'color: #dc2626;',
            'Cancelled': 'color: #4b5563;',
        }
        style = colors.get(obj.status, 'color: #4b5563;')
        return format_html(f'<span style="{style} font-weight: bold; font-size: 11px;">● {obj.status}</span>')
    status_badge.short_description = 'Status'

    def risk_badge(self, obj):
        colors = {
            'Low': 'background: #d1fae5; color: #065f46;',
            'Medium': 'background: #fef3c7; color: #92400e;',
            'High': 'background: #fee2e2; color: #991b1b;',
        }
        style = colors.get(obj.risk_level, 'background: #f1f5f9; color: #475569;')
        return format_html(f'<span style="{style} padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.risk_level} Risk</span>')
    risk_badge.short_description = 'Churn Risk'

    def health_score_display(self, obj):
        color = '#059669' if obj.health_score >= 80 else ('#d97706' if obj.health_score >= 50 else '#dc2626')
        return format_html(f'<b style="color: {color};">{obj.health_score}/100</b>')
    health_score_display.short_description = 'Health Score'


@admin.register(MemberSubscription)
class MemberSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'member', 'plan', 'validity_display', 'status_badge', 'amount_display', 'tenant')
    list_filter = ('status', 'plan', 'tenant', 'start_date', 'end_date')
    search_fields = ('member__name', 'plan__name', 'tenant__name')

    def validity_display(self, obj):
        return f"{obj.start_date} → {obj.end_date}"
    validity_display.short_description = 'Term'

    def status_badge(self, obj):
        color = '#059669' if obj.status == 'Active' else '#dc2626'
        return format_html(f'<span style="color: {color}; font-weight: bold;">{obj.status}</span>')
    status_badge.short_description = 'Status'

    def amount_display(self, obj):
        return f"₹{obj.amount_paid:,.2f}"
    amount_display.short_description = 'Amount'


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'member', 'location', 'check_in_time', 'method_badge', 'tenant')
    list_filter = ('location', 'method', 'tenant', 'check_in_time')
    search_fields = ('member__name', 'location__name', 'tenant__name')
    readonly_fields = ('check_in_time',)

    def method_badge(self, obj):
        return format_html(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.method}</span>')
    method_badge.short_description = 'Access Method'
