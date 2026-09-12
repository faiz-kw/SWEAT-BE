from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Sum, Count, Q
from .models import (
    Tenant, Location, PlatformPlan, TenantBranding, TenantUsage,
    MarketplaceApp, TenantAppInstallation, HistoricalUsageSnapshot
)


class LocationInline(admin.TabularInline):
    model = Location
    extra = 0
    fields = ('id', 'name', 'city', 'members_count_display', 'revenue_display', 'capacity', 'operating_hours', 'is_active')
    readonly_fields = ('id', 'members_count_display', 'revenue_display')
    show_change_link = True

    def members_count_display(self, obj):
        if not obj.id:
            return '-'
        from apps.members.models import Member
        count = Member.objects.filter(location=obj).count()
        return f"{count:,} Members"
    members_count_display.short_description = 'Enrolled Members'

    def revenue_display(self, obj):
        if not obj.id:
            return '-'
        from apps.finance.models import Invoice
        total = Invoice.objects.filter(location=obj, status='Paid').aggregate(s=Sum('total_amount'))['s'] or 0.0
        return f"₹{total:,.2f}"
    revenue_display.short_description = 'Revenue Collected'


class TenantBrandingInline(admin.StackedInline):
    model = TenantBranding
    can_delete = False
    fields = ('app_name', 'primary_color', 'accent_color', 'custom_domain', 'cname_verified')


class TenantUsageInline(admin.StackedInline):
    model = TenantUsage
    can_delete = False
    fields = ('active_members_count', 'locations_count', 'trainers_count', 'storage_used_mb', 'ai_minutes_used', 'api_requests_count')


class TenantAppInstallationInline(admin.TabularInline):
    model = TenantAppInstallation
    extra = 0
    fields = ('app', 'is_active', 'installed_at')
    readonly_fields = ('installed_at',)
    show_change_link = True


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'tenant_title', 'status_badge', 'tier_badge', 
        'branches_display', 'members_display', 'revenue_paid_display', 
        'currency_tz', 'modules_badge', 'is_active', 'created_at'
    )
    list_filter = ('tier', 'status', 'is_active', 'currency', 'timezone', 'created_at')
    search_fields = ('id', 'name', 'slug', 'contact_email', 'phone')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('name',)
    inlines = [LocationInline, TenantBrandingInline, TenantUsageInline, TenantAppInstallationInline]

    fieldsets = (
        ('Organization Identity', {
            'fields': ('id', 'name', 'slug', 'status', 'tier', 'plan', 'is_active')
        }),
        ('Contact & Regional Localization', {
            'fields': ('contact_email', 'phone', 'website', 'currency', 'timezone')
        }),
        ('Quotas & Resource Limits', {
            'fields': ('max_locations', 'max_members')
        }),
        ('Enabled Modules & Submodules', {
            'description': 'JSON list of enabled parent modules and granular submodule route paths.',
            'fields': ('enabled_modules',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    actions = ['mark_as_active', 'mark_as_suspended']

    def tenant_title(self, obj):
        return format_html(f'<b>{obj.name}</b><br><span style="color: #64748b; font-size: 11px;">{obj.slug}.performanceos.in</span>')
    tenant_title.short_description = 'Tenant Brand'

    def status_badge(self, obj):
        colors = {
            'Active': 'background: #059669; color: white;',
            'Trial': 'background: #d97706; color: white;',
            'Suspended': 'background: #dc2626; color: white;'
        }
        style = colors.get(obj.status, 'background: #4b5563; color: white;')
        return format_html(f'<span style="{style} padding: 3px 8px; border-radius: 9999px; font-weight: 600; font-size: 11px;">{obj.status}</span>')
    status_badge.short_description = 'Status'

    def tier_badge(self, obj):
        colors = {
            'Enterprise': 'background: #7c3aed; color: white;',
            'Growth': 'background: #2563eb; color: white;',
            'Starter': 'background: #475569; color: white;'
        }
        style = colors.get(obj.tier, 'background: #4b5563; color: white;')
        return format_html(f'<span style="{style} padding: 3px 8px; border-radius: 6px; font-weight: 600; font-size: 11px;">{obj.tier}</span>')
    tier_badge.short_description = 'Tier'

    def branches_display(self, obj):
        locs = list(obj.locations.values_list('name', flat=True))
        count = len(locs)
        max_locs = obj.max_locations or 1
        loc_str = ", ".join(locs[:2])
        if len(locs) > 2:
            loc_str += f" +{len(locs)-2} more"
        return format_html(
            f'<b style="color: #0f766e;">{count} of {max_locs} Studios</b><br>'
            f'<span style="color: #64748b; font-size: 11px;">{loc_str or "No branches"}</span>'
        )
    branches_display.short_description = 'Sub-Branches'

    def members_display(self, obj):
        from apps.members.models import Member
        total_members = Member.objects.filter(tenant=obj).count()
        active_members = Member.objects.filter(tenant=obj, status='Active').count()
        quota = obj.max_members or 2000
        if total_members == 0 and hasattr(obj, 'usage') and obj.usage:
            total_members = obj.usage.active_members_count
            active_members = obj.usage.active_members_count

        pct = min(100, int((total_members / quota) * 100)) if quota else 0
        return format_html(
            f'<b>{total_members:,} Members</b> <span style="font-size: 11px; color: #059669;">({active_members:,} Active)</span><br>'
            f'<span style="color: #64748b; font-size: 11px;">Quota: {quota:,} ({pct}% utilized)</span>'
        )
    members_display.short_description = 'Member Base'

    def revenue_paid_display(self, obj):
        from apps.finance.models import Invoice, Payment
        paid_total = Payment.objects.filter(tenant=obj, status='Completed').aggregate(s=Sum('amount'))['s']
        if paid_total is None or paid_total == 0:
            paid_total = Invoice.objects.filter(tenant=obj, status='Paid').aggregate(s=Sum('total_amount'))['s'] or 0.0

        pending_total = Invoice.objects.filter(tenant=obj, status__in=['Pending', 'Overdue']).aggregate(s=Sum('total_amount'))['s'] or 0.0

        currency_symbol = '₹' if obj.currency == 'INR' else (obj.currency + ' ')
        return format_html(
            f'<b style="color: #059669; font-size: 12.5px;">{currency_symbol}{float(paid_total):,.2f}</b><br>'
            f'<span style="color: #dc2626; font-size: 11px;">Pending: {currency_symbol}{float(pending_total):,.2f}</span>'
        )
    revenue_paid_display.short_description = 'Total Collected & Paid'

    def currency_tz(self, obj):
        return format_html(f'<b>{obj.currency}</b><br><span style="color: #64748b; font-size: 11px;">{obj.timezone}</span>')
    currency_tz.short_description = 'Region'

    def modules_badge(self, obj):
        mods = obj.enabled_modules or []
        parent_count = len([m for m in mods if not m.startswith('/')])
        sub_count = len([m for m in mods if m.startswith('/')])
        return format_html(f'<span style="background: #0f766e; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{parent_count} Mods ({sub_count} Subs)</span>')
    modules_badge.short_description = 'Features'

    @admin.action(description="Mark selected tenants as Active")
    def mark_as_active(self, request, queryset):
        queryset.update(status='Active', is_active=True)

    @admin.action(description="Mark selected tenants as Suspended")
    def mark_as_suspended(self, request, queryset):
        queryset.update(status='Suspended', is_active=False)


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'location_title', 'tenant_link', 'members_display', 
        'revenue_collected_display', 'capacity_display', 'operating_hours', 'is_active'
    )
    search_fields = ('id', 'name', 'city', 'address', 'tenant__name')
    list_filter = ('tenant', 'is_active', 'city', 'created_at')
    readonly_fields = ('id', 'created_at', 'updated_at')

    def location_title(self, obj):
        return format_html(f'<b>{obj.name}</b><br><span style="color: #64748b; font-size: 11px;">{obj.city} • {obj.address or "Main Facility"}</span>')
    location_title.short_description = 'Studio Branch'

    def tenant_link(self, obj):
        return format_html(f'<b>{obj.tenant.name}</b>') if obj.tenant else '-'
    tenant_link.short_description = 'Tenant Brand'

    def members_display(self, obj):
        from apps.members.models import Member
        total = Member.objects.filter(location=obj).count()
        active = Member.objects.filter(location=obj, status='Active').count()
        return format_html(f'<b>{total:,} Enrolled</b><br><span style="color: #059669; font-size: 11px;">{active:,} Active</span>')
    members_display.short_description = 'Members in Branch'

    def revenue_collected_display(self, obj):
        from apps.finance.models import Invoice
        total = Invoice.objects.filter(location=obj, status='Paid').aggregate(s=Sum('total_amount'))['s'] or 0.0
        currency = obj.tenant.currency if obj.tenant else 'INR'
        sym = '₹' if currency == 'INR' else (currency + ' ')
        return format_html(f'<b style="color: #059669;">{sym}{total:,.2f}</b>')
    revenue_collected_display.short_description = 'Branch Revenue Paid'

    def capacity_display(self, obj):
        return f"{obj.capacity} Max Athletes"
    capacity_display.short_description = 'Capacity'


@admin.register(PlatformPlan)
class PlatformPlanAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'price_display', 'max_locations', 'max_members', 'max_trainers', 'ai_voice_minutes', 'is_popular', 'is_active', 'created_at')
    search_fields = ('id', 'name', 'code')
    list_filter = ('is_popular', 'is_active', 'created_at')
    readonly_fields = ('id', 'created_at', 'updated_at')

    def price_display(self, obj):
        return f"₹{obj.price_monthly:,.2f}/mo"
    price_display.short_description = 'Price (INR)'


@admin.register(TenantBranding)
class TenantBrandingAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'app_name', 'color_preview', 'custom_domain', 'cname_verified', 'updated_at')
    search_fields = ('tenant__name', 'app_name', 'custom_domain')
    list_filter = ('cname_verified',)

    def color_preview(self, obj):
        return format_html(
            f'<span style="display: inline-block; width: 14px; height: 14px; background: {obj.primary_color}; border-radius: 3px; margin-right: 4px; vertical-align: middle;"></span> {obj.primary_color}'
        )
    color_preview.short_description = 'Primary Color'


@admin.register(TenantUsage)
class TenantUsageAdmin(admin.ModelAdmin):
    list_display = (
        'tenant', 'quota_status_badge', 'members_progress', 
        'ai_voice_display', 'storage_display', 'api_requests_count', 'last_calculated_at'
    )
    search_fields = ('tenant__name', 'tenant__id')
    ordering = ('tenant__name',)

    def quota_status_badge(self, obj):
        max_m = obj.tenant.plan.max_members if (obj.tenant and obj.tenant.plan and obj.tenant.plan.max_members) else (obj.tenant.max_members or 500)
        pct = (obj.active_members_count / (max_m or 1)) * 100
        if pct >= 100:
            return format_html('<span style="background: #ef4444; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">LIMIT REACHED</span>')
        elif pct >= 90:
            return format_html('<span style="background: #f59e0b; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">NEAR LIMIT</span>')
        elif pct >= 75:
            return format_html('<span style="background: #eab308; color: black; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">WARNING</span>')
        return format_html('<span style="background: #10b981; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">HEALTHY</span>')
    quota_status_badge.short_description = 'Quota Status'

    def members_progress(self, obj):
        max_m = obj.tenant.plan.max_members if (obj.tenant and obj.tenant.plan and obj.tenant.plan.max_members) else (obj.tenant.max_members or 500)
        pct = min(100, int((obj.active_members_count / (max_m or 1)) * 100))
        return format_html(f'<b>{obj.active_members_count} / {max_m}</b> ({pct}%)')
    members_progress.short_description = 'Active Members'

    def ai_voice_display(self, obj):
        max_v = obj.tenant.plan.ai_voice_minutes if (obj.tenant and obj.tenant.plan and obj.tenant.plan.ai_voice_minutes) else 300
        return f"{obj.ai_minutes_used} / {max_v} min"
    ai_voice_display.short_description = 'AI Voice Mins'

    def storage_display(self, obj):
        return f"{obj.storage_used_mb:.1f} MB"
    storage_display.short_description = 'Cloud Storage'


@admin.register(MarketplaceApp)
class MarketplaceAppAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category_badge', 'developer', 'price_display', 'required_tier', 'is_popular', 'is_active')
    list_filter = ('category', 'required_tier', 'is_popular', 'is_active')
    search_fields = ('id', 'name', 'developer', 'description')
    ordering = ['category', 'name']

    def category_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.category}</span>')
    category_badge.short_description = 'Category'

    def price_display(self, obj):
        if obj.price_monthly == 0:
            return format_html('<span style="color: #059669; font-weight: bold;">Included</span>')
        return f"₹{obj.price_monthly:,.2f}/mo"
    price_display.short_description = 'Price'


@admin.register(TenantAppInstallation)
class TenantAppInstallationAdmin(admin.ModelAdmin):
    list_display = ('id', 'tenant', 'app', 'is_active', 'installed_at', 'updated_at')
    list_filter = ('is_active', 'app__category', 'tenant')
    search_fields = ('tenant__name', 'app__name')


@admin.register(HistoricalUsageSnapshot)
class HistoricalUsageSnapshotAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'snapshot_date', 'members_count', 'storage_used_mb', 'ai_minutes_used', 'api_requests_count')
    list_filter = ('snapshot_date', 'tenant')
    search_fields = ('tenant__name',)
    ordering = ['-snapshot_date']

