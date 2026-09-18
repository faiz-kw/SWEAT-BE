"""
Master app serializers.
"""

from decimal import Decimal
from rest_framework import serializers
from .models_iam import PlatformUser, PlatformRole
from .models_tenant import Tenant, TenantDomain, TenantBranding, PlatformBranding
from .models_saas import (
    SaasPlan, SaasPlanPrice, ProductModule, TenantSubscription,
    TenantModule, SubscriptionInvoice, SubscriptionInvoiceItem, TenantResourceUsage, ResourceMetric,
)
from .models_market import MarketplaceIntegration
from .models_infra import TenantProvisioning, TenantDataSource


class TenantSerializer(serializers.ModelSerializer):
    tier = serializers.SerializerMethodField()
    plan_name = serializers.SerializerMethodField()
    enabled_modules = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    status = serializers.SerializerMethodField()
    max_locations = serializers.SerializerMethodField()
    max_members = serializers.SerializerMethodField()
    locations = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'code', 'name', 'legal_name', 'slug', 'status', 'status_display',
            'country', 'currency', 'timezone', 'default_language',
            'tier', 'plan_name', 'enabled_modules',
            'max_locations', 'max_members', 'locations',
            'activated_at', 'suspended_at', 'deactivated_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'activated_at', 'suspended_at', 'deactivated_at', 'created_at', 'updated_at']

    def get_status(self, obj):
        return obj.status.capitalize() if obj.status else 'Active'

    def get_tier(self, obj):
        sub = obj.subscriptions.filter(status__in=['ACTIVE', 'TRIALING']).select_related('plan').first()
        return sub.plan.tier.capitalize() if (sub and sub.plan and sub.plan.tier) else None

    def get_plan_name(self, obj):
        sub = obj.subscriptions.filter(status__in=['ACTIVE', 'TRIALING']).select_related('plan').first()
        return sub.plan.name if (sub and sub.plan) else None

    def get_enabled_modules(self, obj):
        return list(obj.enabled_modules_set.filter(is_enabled=True).values_list('module__code', flat=True))

    def get_max_locations(self, obj):
        limit = obj.resource_limits.filter(metric__code='LOCATIONS').first()
        if limit:
            return int(limit.limit_value)
        sub = obj.subscriptions.filter(status__in=['ACTIVE', 'TRIALING']).select_related('plan').first()
        if sub and sub.plan:
            plan_lim = sub.plan.resource_limits.filter(metric__code='LOCATIONS').first()
            if plan_lim:
                return int(plan_lim.limit_value)
        return 3

    def get_max_members(self, obj):
        limit = obj.resource_limits.filter(metric__code__in=['ACTIVE_MEMBERS', 'ACTIVE_USERS']).first()
        if limit:
            return int(limit.limit_value)
        sub = obj.subscriptions.filter(status__in=['ACTIVE', 'TRIALING']).select_related('plan').first()
        if sub and sub.plan:
            plan_lim = sub.plan.resource_limits.filter(metric__code__in=['ACTIVE_MEMBERS', 'ACTIVE_USERS']).first()
            if plan_lim:
                return int(plan_lim.limit_value)
        return 2000

    def get_locations(self, obj):
        try:
            from apps.master.models_infra import TenantDataSource
            from config.tenant_middleware import _register_tenant_connection
            from config.routers import build_tenant_db_alias, set_tenant_db_alias
            from apps.tenant_core.models_org import Branch

            ds = TenantDataSource.objects.using('default').filter(tenant=obj, status='ACTIVE').first()
            if not ds:
                return []
            db_name = ds.database_name or ds.db_name
            if not db_name:
                return []
            db_alias = build_tenant_db_alias(obj.id)
            _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=obj.id)
            set_tenant_db_alias(db_alias)
            try:
                branches = Branch.objects.using(db_alias).select_related('location').all().order_by('name')
                results = []
                for b in branches:
                    loc = b.location
                    hours = f"{b.business_open_time} - {b.business_close_time}" if b.business_open_time else "06:00 - 22:00"
                    results.append({
                        'id': str(b.id),
                        'name': b.name,
                        'city': loc.city if loc else '',
                        'address': b.address or (loc.area if loc else ''),
                        'phone': b.phone or '',
                        'capacity': b.capacity or 100,
                        'operating_hours': hours,
                        'is_active': b.status == 'ACTIVE',
                        'tenant': str(obj.id),
                        'tenant_name': obj.name,
                        'members_count': 0,
                        'revenue_collected': 0,
                        'created_at': b.activated_at.isoformat() if b.activated_at else None,
                    })
                return results
            finally:
                set_tenant_db_alias(None)
        except Exception:
            return []


class TenantDetailSerializer(TenantSerializer):
    """Detailed tenant view including domains, branding, and subscription."""
    domains = serializers.SerializerMethodField()
    branding = serializers.SerializerMethodField()

    class Meta(TenantSerializer.Meta):
        fields = TenantSerializer.Meta.fields + ['domains', 'branding']

    def get_domains(self, obj):
        return list(obj.domains.values('id', 'domain', 'domain_type', 'is_primary', 'status'))

    def get_branding(self, obj):
        try:
            b = obj.branding
            return {'branding_mode': b.branding_mode, 'app_name': b.app_name, 'primary_color': b.primary_color}
        except Exception:
            return None


class SaasPlanPriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaasPlanPrice
        fields = ['id', 'billing_cycle', 'currency', 'amount', 'is_active']


class SaasPlanSerializer(serializers.ModelSerializer):
    prices = SaasPlanPriceSerializer(many=True, read_only=True)
    price_monthly = serializers.SerializerMethodField()
    price_annual = serializers.SerializerMethodField()
    max_locations = serializers.SerializerMethodField()
    max_members = serializers.SerializerMethodField()
    max_trainers = serializers.SerializerMethodField()
    ai_voice_minutes = serializers.SerializerMethodField()
    features = serializers.SerializerMethodField()
    tenants_count = serializers.SerializerMethodField()

    class Meta:
        model = SaasPlan
        fields = [
            'id', 'name', 'code', 'description', 'tier', 'is_public', 'is_popular',
            'trial_days', 'sort_order', 'prices',
            'price_monthly', 'price_annual', 'max_locations', 'max_members',
            'max_trainers', 'ai_voice_minutes', 'features', 'tenants_count',
        ]

    def get_price_monthly(self, obj):
        p = obj.prices.filter(billing_cycle='MONTHLY', is_active=True).first()
        return float(p.amount) if p else 0

    def get_price_annual(self, obj):
        p = obj.prices.filter(billing_cycle='ANNUAL', is_active=True).first()
        return float(p.amount) if p else 0

    def _get_metric_limit(self, obj, metric_code, default_val=0):
        rl = obj.resource_limits.filter(metric__code=metric_code).first()
        return rl.limit_value if rl else default_val

    def get_max_locations(self, obj):
        return self._get_metric_limit(obj, 'LOCATIONS', 1)

    def get_max_members(self, obj):
        return self._get_metric_limit(obj, 'ACTIVE_MEMBERS', 500)

    def get_max_trainers(self, obj):
        return self._get_metric_limit(obj, 'TRAINERS', 5)

    def get_ai_voice_minutes(self, obj):
        return self._get_metric_limit(obj, 'AI_VOICE_MINUTES', 0)

    def get_features(self, obj):
        modules = obj.plan_modules.filter(is_included=True).select_related('module')
        return [m.module.name for m in modules]

    def get_tenants_count(self, obj):
        return obj.subscriptions.filter(status='ACTIVE').count()


class ProductModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductModule
        fields = ['id', 'name', 'code', 'description', 'icon', 'is_core', 'sort_order']


class PlatformUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformUser
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'phone', 'status', 'is_staff', 'created_at']
        read_only_fields = ['id', 'created_at']
        extra_kwargs = {'password': {'write_only': True}}

    def validate(self, attrs):
        from apps.master.services_auth_directory import check_identifier_available
        subject_id = self.instance.id if self.instance else None

        email = attrs.get('email')
        if email and not check_identifier_available(email, exclude_subject_id=subject_id):
            raise serializers.ValidationError({
                'email': 'This username or email is already registered.'
            })

        username = attrs.get('username')
        if username and not check_identifier_available(username, exclude_subject_id=subject_id):
            raise serializers.ValidationError({
                'username': 'This username or email is already registered.'
            })

        return super().validate(attrs)


class MarketplaceIntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceIntegration
        fields = ['id', 'name', 'code', 'integration_type', 'provider', 'description',
                  'icon_text', 'logo_storage_key', 'is_free', 'price_monthly', 'is_popular', 'status']


class TenantDataSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantDataSource
        fields = ['id', 'db_name', 'source_type', 'status', 'provisioned_at', 'db_schema_version']


class TenantProvisioningSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.name', read_only=True, default='')
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True, default='')

    class Meta:
        model = TenantProvisioning
        fields = [
            'id', 'tenant_name', 'tenant_slug', 'status',
            'current_step', 'total_steps', 'completed_steps',
            'step_log', 'error_step', 'error_message',
            'started_at', 'completed_at',
        ]


class TenantModuleSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    module_code = serializers.CharField(source='module.code', read_only=True)
    module_name = serializers.CharField(source='module.name', read_only=True)

    class Meta:
        model = TenantModule
        fields = [
            'id', 'tenant', 'tenant_slug', 'tenant_name',
            'module', 'module_code', 'module_name',
            'availability_mode', 'is_enabled', 'enabled_by_plan',
            'enabled_at', 'disabled_at',
        ]
        read_only_fields = ['id', 'enabled_at']


class SubscriptionInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionInvoiceItem
        fields = [
            'id', 'item_type', 'description', 'quantity',
            'unit_price', 'subtotal', 'tax_rate', 'tax_amount',
            'total_amount', 'metadata', 'created_at',
        ]


class TenantSubscriptionSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    plan_code = serializers.CharField(source='plan.code', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)

    # BillingWorkspace frontend contract fields
    tenantId = serializers.CharField(source='tenant.id', read_only=True)
    tenantName = serializers.CharField(source='tenant.name', read_only=True)
    tenantLogo = serializers.SerializerMethodField()
    plan = serializers.CharField(source='plan.name', read_only=True)
    tier = serializers.CharField(source='plan.tier', read_only=True)
    cycle = serializers.SerializerMethodField()
    mrr = serializers.SerializerMethodField()
    paymentMethod = serializers.SerializerMethodField()
    nextBillingDate = serializers.SerializerMethodField()

    class Meta:
        model = TenantSubscription
        fields = [
            'id', 'tenant', 'tenant_slug', 'tenant_name',
            'plan', 'plan_code', 'plan_name', 'plan_price',
            'billing_cycle', 'status', 'currency', 'billing_amount',
            'started_at', 'trial_ends_at', 'current_period_start',
            'current_period_end', 'next_renewal_at', 'cancelled_at',
            'cancellation_reason', 'autopay_enabled',
            'created_at', 'updated_at',
            # Frontend contract fields
            'tenantId', 'tenantName', 'tenantLogo', 'tier',
            'cycle', 'mrr', 'paymentMethod', 'nextBillingDate',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_tenantLogo(self, obj):
        branding = getattr(obj.tenant, 'branding', None)
        if branding and branding.logo_storage_key:
            return branding.logo_storage_key
        return ''

    def get_cycle(self, obj):
        if obj.billing_cycle == 'ANNUAL':
            return 'Annual'
        return 'Monthly'

    def get_mrr(self, obj):
        if not obj.billing_amount:
            return 0.0
        if obj.billing_cycle == 'ANNUAL':
            return float(round(obj.billing_amount / Decimal('12.00'), 2))
        return float(obj.billing_amount)

    def get_paymentMethod(self, obj):
        bm = obj.tenant.billing_methods.filter(is_active=True).order_by('-is_default', '-created_at').first()
        if bm:
            return bm.display_name or bm.get_method_type_display()
        return 'Not configured'

    def get_nextBillingDate(self, obj):
        target = obj.next_renewal_at or obj.current_period_end or obj.trial_ends_at
        if target:
            return target.strftime('%Y-%m-%d')
        return ''


class SubscriptionInvoiceSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    items = SubscriptionInvoiceItemSerializer(many=True, read_only=True)

    # BillingWorkspace frontend contract fields
    tenantId = serializers.CharField(source='tenant.id', read_only=True)
    tenantName = serializers.CharField(source='tenant.name', read_only=True)
    amount = serializers.SerializerMethodField()
    baseAmount = serializers.SerializerMethodField()
    gstAmount = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    pdfUrl = serializers.SerializerMethodField()
    invoiceNumber = serializers.CharField(source='invoice_number', read_only=True)

    class Meta:
        model = SubscriptionInvoice
        fields = [
            'id', 'invoice_number', 'tenant', 'tenant_slug', 'tenant_name',
            'subscription', 'status', 'currency',
            'subtotal', 'tax_amount', 'discount_amount', 'total', 'total_amount',
            'billing_period_start', 'billing_period_end',
            'due_date', 'due_at', 'issued_at', 'paid_at',
            'invoice_file_key', 'notes', 'items',
            'created_at', 'updated_at',
            # Frontend contract fields
            'tenantId', 'tenantName', 'invoiceNumber',
            'amount', 'baseAmount', 'gstAmount', 'date', 'pdfUrl',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_amount(self, obj):
        return float(obj.total_amount or obj.total or 0)

    def get_baseAmount(self, obj):
        return float(obj.subtotal or 0)

    def get_gstAmount(self, obj):
        return float(obj.tax_amount or 0)

    def get_date(self, obj):
        dt = obj.issued_at or obj.created_at
        return dt.strftime('%Y-%m-%d') if dt else ''

    def get_pdfUrl(self, obj):
        return obj.invoice_file_key or ''


class TenantResourceUsageSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    metric_code = serializers.CharField(source='metric.code', read_only=True)
    metric_name = serializers.CharField(source='metric.name', read_only=True)
    metric_unit = serializers.CharField(source='metric.unit', read_only=True)

    class Meta:
        model = TenantResourceUsage
        fields = [
            'id', 'tenant', 'tenant_slug', 'tenant_name',
            'metric', 'metric_code', 'metric_name', 'metric_unit',
            'current_value', 'billing_period_start', 'billing_period_end',
            'last_calculated_at', 'is_platform_billable',
        ]
        read_only_fields = ['id', 'last_calculated_at']


class PlatformBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformBranding
        fields = [
            'id', 'brand_name', 'platform_name',
            'logo_url', 'favicon_url',
            'primary_color', 'secondary_color', 'accent_color',
            'login_title', 'support_phone', 'support_email', 'support_url',
            'logo_storage_key', 'favicon_storage_key',
            'login_logo_key', 'login_background_key', 'login_background_url',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class TenantBrandingSerializer(serializers.ModelSerializer):
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    custom_domain = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    cname_verified = serializers.BooleanField(required=False, allow_null=True)

    class Meta:
        model = TenantBranding
        fields = [
            'id', 'tenant', 'tenant_slug', 'tenant_name', 'branding_mode',
            'brand_name', 'app_name',
            'logo_url', 'favicon_url',
            'primary_color', 'secondary_color', 'accent_color',
            'theme_preset_code', 'theme_tokens',
            'support_phone', 'support_email',
            'email_footer', 'remove_watermark',
            'logo_storage_key', 'favicon_storage_key',
            'login_logo_key', 'login_background_key', 'email_logo_key',
            'login_background_url', 'login_tagline',
            'custom_domain', 'cname_verified',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tenant_slug', 'tenant_name', 'created_at', 'updated_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        domain = instance.tenant.domains.filter(is_primary=True).first() or instance.tenant.domains.first()
        ret['custom_domain'] = domain.domain if domain else ''
        ret['cname_verified'] = domain.is_verified if domain else False
        return ret

    def update(self, instance, validated_data):
        custom_domain = validated_data.pop('custom_domain', None)
        cname_verified = validated_data.pop('cname_verified', None)

        updated_instance = super().update(instance, validated_data)

        if custom_domain is not None:
            clean_domain = custom_domain.strip().lower()
            if clean_domain:
                domain_obj = instance.tenant.domains.filter(domain=clean_domain).first()
                if not domain_obj:
                    instance.tenant.domains.filter(is_primary=True).update(is_primary=False)
                    domain_type = 'PLATFORM' if clean_domain.endswith('.performanceos.io') else 'CUSTOM'
                    TenantDomain.objects.using('default').create(
                        tenant=instance.tenant,
                        domain=clean_domain,
                        domain_type=domain_type,
                        is_primary=True,
                        is_verified=bool(cname_verified) if cname_verified is not None else False,
                        status='ACTIVE' if cname_verified else 'PENDING',
                    )
                else:
                    instance.tenant.domains.exclude(id=domain_obj.id).filter(is_primary=True).update(is_primary=False)
                    domain_obj.is_primary = True
                    if cname_verified is not None:
                        domain_obj.is_verified = cname_verified
                        if cname_verified:
                            domain_obj.status = 'ACTIVE'
                    domain_obj.save(using='default')
            elif cname_verified is not None:
                domain_obj = instance.tenant.domains.filter(is_primary=True).first() or instance.tenant.domains.first()
                if domain_obj:
                    domain_obj.is_verified = cname_verified
                    if cname_verified:
                        domain_obj.status = 'ACTIVE'
                    domain_obj.save(using='default')
        elif cname_verified is not None:
            domain_obj = instance.tenant.domains.filter(is_primary=True).first() or instance.tenant.domains.first()
            if domain_obj:
                domain_obj.is_verified = cname_verified
                if cname_verified:
                    domain_obj.status = 'ACTIVE'
                domain_obj.save(using='default')

        return updated_instance


