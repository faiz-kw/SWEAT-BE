from django.contrib import admin
from django.utils.html import format_html
from .models import Invoice, Payment, Coupon


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ('amount', 'payment_method', 'transaction_id', 'status', 'paid_at')
    readonly_fields = ('paid_at',)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'member', 'total_display', 'tax_display', 'status_badge', 'due_date', 'location', 'tenant')
    list_filter = ('status', 'location', 'tenant', 'due_date')
    search_fields = ('id', 'member__name', 'description', 'tenant__name')
    inlines = [PaymentInline]

    def total_display(self, obj):
        return format_html(f'<b>₹{obj.total_amount:,.2f}</b>')
    total_display.short_description = 'Total (INR)'

    def tax_display(self, obj):
        return f"₹{obj.tax_amount:,.2f} GST"
    tax_display.short_description = 'Tax'

    def status_badge(self, obj):
        colors = {
            'Paid': 'background: #d1fae5; color: #065f46;',
            'Pending': 'background: #fef3c7; color: #92400e;',
            'Overdue': 'background: #fee2e2; color: #991b1b;',
            'Refunded': 'background: #f1f5f9; color: #475569;',
        }
        style = colors.get(obj.status, 'background: #f1f5f9; color: #475569;')
        return format_html(f'<span style="{style} padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.status}</span>')
    status_badge.short_description = 'Status'


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'member', 'amount_display', 'method_badge', 'transaction_id', 'status_badge', 'paid_at', 'tenant')
    list_filter = ('payment_method', 'status', 'tenant', 'paid_at')
    search_fields = ('id', 'transaction_id', 'member__name', 'tenant__name')

    def amount_display(self, obj):
        return f"₹{obj.amount:,.2f}"
    amount_display.short_description = 'Amount'

    def method_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.payment_method}</span>')
    method_badge.short_description = 'Payment Mode'

    def status_badge(self, obj):
        color = '#059669' if obj.status == 'Completed' else ('#dc2626' if obj.status == 'Failed' else '#d97706')
        return format_html(f'<span style="color: {color}; font-weight: bold;">● {obj.status}</span>')
    status_badge.short_description = 'Status'


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ('id', 'code_display', 'discount_display', 'validity_display', 'times_used', 'is_active', 'tenant')
    list_filter = ('discount_type', 'is_active', 'tenant')
    search_fields = ('id', 'code', 'description')

    def code_display(self, obj):
        return format_html(f'<code style="font-size: 12px; font-weight: bold; background: #fef3c7; color: #92400e; padding: 2px 6px; border-radius: 4px;">{obj.code}</code>')
    code_display.short_description = 'Coupon Code'

    def discount_display(self, obj):
        if obj.discount_type == 'Percentage':
            return f"{obj.discount_value}% OFF"
        return f"₹{obj.discount_value:,.2f} FLAT"
    discount_display.short_description = 'Discount'

    def validity_display(self, obj):
        return f"{obj.valid_from} → {obj.valid_until}"
    validity_display.short_description = 'Validity Period'
