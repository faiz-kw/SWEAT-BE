from django.contrib import admin
from django.utils.html import format_html
from .models import Trainer, FitnessClass, Booking


@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    list_display = ('id', 'trainer_name', 'spec_badge', 'rating_display', 'rate_display', 'is_available', 'tenant')
    list_filter = ('specialization', 'is_available', 'tenant')
    search_fields = ('id', 'user__first_name', 'user__last_name', 'user__email', 'tenant__name')

    def trainer_name(self, obj):
        name = f"{obj.user.first_name} {obj.user.last_name}".strip() if obj.user else '-'
        return format_html(f'<b>{name}</b>')
    trainer_name.short_description = 'Trainer'

    def spec_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.specialization}</span>')
    spec_badge.short_description = 'Specialization'

    def rating_display(self, obj):
        return format_html(f'<span style="color: #d97706; font-weight: bold;">★ {obj.rating:.1f}</span>')
    rating_display.short_description = 'Rating'

    def rate_display(self, obj):
        return f"₹{obj.pt_hourly_rate:,.2f}/hr"
    rate_display.short_description = 'PT Rate'


@admin.register(FitnessClass)
class FitnessClassAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category_badge', 'location', 'trainer', 'schedule_display', 'capacity_display', 'status_badge', 'tenant')
    list_filter = ('category', 'location', 'is_cancelled', 'tenant')
    search_fields = ('id', 'name', 'trainer__user__first_name', 'tenant__name')

    def category_badge(self, obj):
        return format_html(f'<span style="background: #f3e8ff; color: #6b21a8; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.category}</span>')
    category_badge.short_description = 'Category'

    def schedule_display(self, obj):
        return f"{obj.start_time.strftime('%b %d, %H:%M')} - {obj.end_time.strftime('%H:%M')}"
    schedule_display.short_description = 'Schedule'

    def capacity_display(self, obj):
        return f"{obj.max_capacity} Max"
    capacity_display.short_description = 'Capacity'

    def status_badge(self, obj):
        if obj.is_cancelled:
            return format_html('<span style="color: #dc2626; font-weight: bold;">CANCELLED</span>')
        return format_html('<span style="color: #059669; font-weight: bold;">SCHEDULED</span>')
    status_badge.short_description = 'Status'


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'member', 'type_badge', 'trainer', 'fitness_class', 'location', 'scheduled_at', 'status_badge', 'tenant')
    list_filter = ('booking_type', 'status', 'location', 'tenant', 'scheduled_at')
    search_fields = ('id', 'member__name', 'notes', 'tenant__name')

    def type_badge(self, obj):
        return format_html(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.booking_type}</span>')
    type_badge.short_description = 'Type'

    def status_badge(self, obj):
        colors = {
            'Confirmed': 'color: #059669;',
            'Attended': 'color: #0284c7;',
            'Cancelled': 'color: #dc2626;',
            'No Show': 'color: #991b1b;',
        }
        style = colors.get(obj.status, 'color: #4b5563;')
        return format_html(f'<span style="{style} font-weight: bold;">● {obj.status}</span>')
    status_badge.short_description = 'Status'
