from rest_framework import serializers
from django.db import transaction
from django.utils.text import slugify
import uuid

from .models import (
    Tenant, Location, PlatformPlan, TenantBranding, TenantUsage,
    MarketplaceApp, TenantAppInstallation, HistoricalUsageSnapshot
)


class LocationSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()
    revenue_collected = serializers.SerializerMethodField()
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    # tenant is required in DB but resolved in perform_create; allow write via tenant_id
    tenant = serializers.PrimaryKeyRelatedField(
        queryset=Tenant.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Location
        fields = [
            'id', 'tenant', 'tenant_name', 'name', 'city', 'address', 'phone', 
            'capacity', 'operating_hours', 'is_active', 
            'members_count', 'revenue_collected',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'tenant_name', 'created_at', 'updated_at']

    def validate_phone(self, value):
        """Accept exactly 10 digits (with optional +91 or 0 prefix stripped before storage)."""
        import re
        if not value:
            return value
        # Strip non-digit characters except leading +
        digits_only = re.sub(r'\D', '', value)
        # Accept 10-digit or 12-digit (91 + 10 digit)
        if len(digits_only) == 12 and digits_only.startswith('91'):
            digits_only = digits_only[2:]
        if len(digits_only) != 10:
            raise serializers.ValidationError("Contact phone must be exactly 10 digits.")
        return digits_only

    def get_members_count(self, obj):
        from apps.members.models import Member
        return Member.objects.filter(location=obj).count()

    def get_revenue_collected(self, obj):
        from apps.finance.models import Invoice
        from django.db.models import Sum
        return float(Invoice.objects.filter(location=obj, status='Paid').aggregate(s=Sum('total_amount'))['s'] or 0.0)


class TenantBrandingSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantBranding
        fields = [
            'tenant', 'app_name', 'logo_url', 'favicon_url', 
            'primary_color', 'accent_color', 'custom_domain', 
            'cname_verified', 'email_footer', 'support_email',
            'remove_watermark', 'login_tagline', 'updated_at'
        ]
        read_only_fields = ['updated_at']


class HistoricalUsageSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = HistoricalUsageSnapshot
        fields = [
            'id', 'tenant', 'snapshot_date', 'members_count',
            'storage_used_mb', 'ai_minutes_used', 'api_requests_count', 'created_at'
        ]


class TenantUsageSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_tier = serializers.CharField(source='tenant.tier', read_only=True)
    plan_name = serializers.SerializerMethodField()
    max_members = serializers.SerializerMethodField()
    max_locations = serializers.SerializerMethodField()
    max_trainers = serializers.SerializerMethodField()
    max_ai_minutes = serializers.SerializerMethodField()
    max_storage_mb = serializers.SerializerMethodField()
    max_api_requests = serializers.SerializerMethodField()
    
    # Calculated utilization metrics
    member_utilization_pct = serializers.SerializerMethodField()
    voice_utilization_pct = serializers.SerializerMethodField()
    storage_utilization_pct = serializers.SerializerMethodField()
    api_utilization_pct = serializers.SerializerMethodField()
    overall_utilization_pct = serializers.SerializerMethodField()
    
    status_label = serializers.SerializerMethodField()
    status_tone = serializers.SerializerMethodField()
    billing_period = serializers.SerializerMethodField()
    historical_snapshots = serializers.SerializerMethodField()

    class Meta:
        model = TenantUsage
        fields = [
            'tenant', 'tenant_id', 'tenant_name', 'tenant_tier', 'plan_name',
            'active_members_count', 'locations_count', 'trainers_count',
            'storage_used_mb', 'ai_minutes_used', 'api_requests_count',
            'max_members', 'max_locations', 'max_trainers',
            'max_ai_minutes', 'max_storage_mb', 'max_api_requests',
            'member_utilization_pct', 'voice_utilization_pct',
            'storage_utilization_pct', 'api_utilization_pct', 'overall_utilization_pct',
            'status_label', 'status_tone', 'billing_period', 'historical_snapshots',
            'last_calculated_at'
        ]
        read_only_fields = ['tenant', 'last_calculated_at']

    def get_plan_name(self, obj):
        if obj.tenant and obj.tenant.plan:
            return obj.tenant.plan.name
        return f"{obj.tenant.tier} Tier" if obj.tenant else "Growth Tier"

    def get_max_members(self, obj):
        if obj.tenant and obj.tenant.plan:
            return obj.tenant.plan.max_members
        return obj.tenant.max_members if obj.tenant else 2000

    def get_max_locations(self, obj):
        if obj.tenant and obj.tenant.plan:
            return obj.tenant.plan.max_locations
        return obj.tenant.max_locations if obj.tenant else 3

    def get_max_trainers(self, obj):
        if obj.tenant and obj.tenant.plan:
            return obj.tenant.plan.max_trainers
        tier_limits = {'Starter': 10, 'Growth': 25, 'Enterprise': 500}
        return tier_limits.get(obj.tenant.tier, 25) if obj.tenant else 25

    def get_max_ai_minutes(self, obj):
        if obj.tenant and obj.tenant.plan:
            return obj.tenant.plan.ai_voice_minutes
        tier_limits = {'Starter': 100, 'Growth': 300, 'Enterprise': 5000}
        return tier_limits.get(obj.tenant.tier, 300) if obj.tenant else 300

    def get_max_storage_mb(self, obj):
        tier_limits = {'Starter': 1024.0, 'Growth': 5120.0, 'Enterprise': 51200.0}
        return tier_limits.get(obj.tenant.tier, 5120.0) if obj.tenant else 5120.0

    def get_max_api_requests(self, obj):
        tier_limits = {'Starter': 50000, 'Growth': 250000, 'Enterprise': 5000000}
        return tier_limits.get(obj.tenant.tier, 250000) if obj.tenant else 250000

    def get_member_utilization_pct(self, obj):
        limit = self.get_max_members(obj)
        return min(100, round((obj.active_members_count / (limit or 1)) * 100, 1))

    def get_voice_utilization_pct(self, obj):
        limit = self.get_max_ai_minutes(obj)
        return min(100, round((obj.ai_minutes_used / (limit or 1)) * 100, 1))

    def get_storage_utilization_pct(self, obj):
        limit = self.get_max_storage_mb(obj)
        return min(100, round((obj.storage_used_mb / (limit or 1)) * 100, 1))

    def get_api_utilization_pct(self, obj):
        limit = self.get_max_api_requests(obj)
        return min(100, round((obj.api_requests_count / (limit or 1)) * 100, 1))

    def get_overall_utilization_pct(self, obj):
        m_pct = self.get_member_utilization_pct(obj)
        v_pct = self.get_voice_utilization_pct(obj)
        s_pct = self.get_storage_utilization_pct(obj)
        a_pct = self.get_api_utilization_pct(obj)
        return round((m_pct * 0.4) + (v_pct * 0.3) + (s_pct * 0.2) + (a_pct * 0.1), 1)

    def get_status_label(self, obj):
        max_pct = max(
            self.get_member_utilization_pct(obj),
            self.get_voice_utilization_pct(obj),
            self.get_storage_utilization_pct(obj)
        )
        if max_pct >= 100:
            return "Limit Reached"
        elif max_pct >= 90:
            return "Near Limit"
        elif max_pct >= 75:
            return "Warning"
        elif max_pct >= 40:
            return "Normal"
        return "Healthy"

    def get_status_tone(self, obj):
        status = self.get_status_label(obj)
        if status in ["Limit Reached", "Exceeded"]:
            return "critical"
        if status in ["Near Limit", "Warning"]:
            return "warn"
        return "positive"

    def get_billing_period(self, obj):
        from django.utils import timezone
        now = timezone.now()
        return now.strftime("%B %Y")

    def get_historical_snapshots(self, obj):
        if not obj.tenant:
            return []
        snapshots = HistoricalUsageSnapshot.objects.filter(tenant=obj.tenant).order_by('-snapshot_date')[:7]
        return [
            {
                'date': s.snapshot_date.strftime('%Y-%m-%d'),
                'members': s.members_count,
                'storage_mb': s.storage_used_mb,
                'ai_minutes': s.ai_minutes_used,
                'api_requests': s.api_requests_count
            }
            for s in reversed(list(snapshots))
        ]



class PlatformPlanSerializer(serializers.ModelSerializer):
    tenants_count = serializers.SerializerMethodField()
    code = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = PlatformPlan
        fields = [
            'id', 'name', 'code', 'description', 'price_monthly', 'price_annual',
            'currency', 'max_locations', 'max_members', 'max_trainers',
            'ai_voice_minutes', 'features', 'is_popular', 'is_active',
            'tenants_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        if not attrs.get('code') and attrs.get('name'):
            base_code = attrs['name'].upper().replace(' ', '_')
            clean_code = f"PLAN_{base_code}"
            if PlatformPlan.objects.filter(code=clean_code).exists():
                clean_code = f"{clean_code}_{uuid.uuid4().hex[:4].upper()}"
            attrs['code'] = clean_code
        return attrs

    def get_tenants_count(self, obj):
        return obj.tenants.count()


class TenantSerializer(serializers.ModelSerializer):
    locations = LocationSerializer(many=True, read_only=True)
    branding = TenantBrandingSerializer(read_only=True)
    usage = TenantUsageSerializer(read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    members_count = serializers.SerializerMethodField()
    active_members_count = serializers.SerializerMethodField()
    total_paid_revenue = serializers.SerializerMethodField()
    pending_revenue = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'slug', 'status', 'tier', 'plan', 'plan_name',
            'contact_email', 'phone', 'website', 'currency', 'timezone',
            'max_locations', 'max_members', 'enabled_modules', 'is_active',
            'locations', 'branding', 'usage',
            'members_count', 'active_members_count', 'total_paid_revenue', 'pending_revenue',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_members_count(self, obj):
        from apps.members.models import Member
        cnt = Member.objects.filter(tenant=obj).count()
        if cnt == 0 and hasattr(obj, 'usage') and obj.usage:
            return obj.usage.active_members_count
        return cnt

    def get_active_members_count(self, obj):
        from apps.members.models import Member
        cnt = Member.objects.filter(tenant=obj, status='Active').count()
        if cnt == 0 and hasattr(obj, 'usage') and obj.usage:
            return obj.usage.active_members_count
        return cnt

    def get_total_paid_revenue(self, obj):
        from apps.finance.models import Invoice, Payment
        from django.db.models import Sum
        paid = Payment.objects.filter(tenant=obj, status='Completed').aggregate(s=Sum('amount'))['s']
        if paid is None or paid == 0:
            paid = Invoice.objects.filter(tenant=obj, status='Paid').aggregate(s=Sum('total_amount'))['s'] or 0.0
        return float(paid)

    def get_pending_revenue(self, obj):
        from apps.finance.models import Invoice
        from django.db.models import Sum
        pending = Invoice.objects.filter(tenant=obj, status__in=['Pending', 'Overdue']).aggregate(s=Sum('total_amount'))['s'] or 0.0
        return float(pending)


class TenantOnboardingSerializer(serializers.Serializer):
    """
    Validates and executes atomic provisioning of a new Tenant, Primary Location,
    and Super Admin account in a single transaction.
    """
    # Brand details
    brand_name = serializers.CharField(max_length=255)
    slug = serializers.SlugField(max_length=255, required=False)
    tier = serializers.ChoiceField(choices=['Starter', 'Growth', 'Enterprise'], default='Growth')
    plan_id = serializers.CharField(required=False, allow_null=True)
    currency = serializers.CharField(max_length=8, default='INR')
    timezone = serializers.CharField(max_length=64, default='Asia/Kolkata')
    
    # Primary Location
    location_name = serializers.CharField(max_length=255, default='Main Studio')
    city = serializers.CharField(max_length=128, default='Bengaluru')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    
    # Admin User
    admin_first_name = serializers.CharField(max_length=150)
    admin_last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    admin_email = serializers.EmailField()
    admin_phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    admin_password = serializers.CharField(write_only=True, min_length=3)

    # Add-on Modules
    enabled_modules = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    def validate_slug(self, value):
        if not value:
            return value
        if Tenant.objects.filter(slug=value).exists():
            raise serializers.ValidationError("A tenant with this subdomain/slug already exists.")
        return value

    def validate_admin_email(self, value):
        from apps.users.models import User
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        from apps.users.models import User, Role

        brand_name = validated_data['brand_name']
        slug = validated_data.get('slug') or slugify(brand_name)
        if Tenant.objects.filter(slug=slug).exists():
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"

        plan_id = validated_data.get('plan_id')
        plan = None
        if plan_id:
            plan = PlatformPlan.objects.filter(id=plan_id).first()

        with transaction.atomic():
            # 1. Create Tenant
            tenant_id = f"TEN-{uuid.uuid4().hex[:6].upper()}"
            tenant = Tenant.objects.create(
                id=tenant_id,
                name=brand_name,
                slug=slug,
                tier=validated_data.get('tier', 'Growth'),
                plan=plan,
                contact_email=validated_data['admin_email'],
                phone=validated_data.get('admin_phone', ''),
                currency=validated_data.get('currency', 'INR'),
                timezone=validated_data.get('timezone', 'Asia/Kolkata'),
                enabled_modules=validated_data.get('enabled_modules', [
                    'crm', 'members', 'operations', 'finance', 'coaching', 'ai'
                ])
            )

            # 2. Create Primary Location
            location_id = f"LOC-{uuid.uuid4().hex[:6].upper()}"
            location = Location.objects.create(
                id=location_id,
                tenant=tenant,
                name=validated_data.get('location_name', 'Main Studio'),
                city=validated_data.get('city', 'Bengaluru'),
                address=validated_data.get('address', ''),
                is_active=True
            )

            # 3. Create Super Admin User
            user_id = f"USR-{uuid.uuid4().hex[:6].upper()}"
            admin_user = User.objects.create_user(
                id=user_id,
                email=validated_data['admin_email'],
                password=validated_data['admin_password'],
                first_name=validated_data['admin_first_name'],
                last_name=validated_data.get('admin_last_name', ''),
                phone=validated_data.get('admin_phone', ''),
                tenant=tenant,
                role=Role.ADMIN,
                active_location=location
            )
            admin_user.allowed_locations.add(location)

            # 4. Create Initial Branding
            TenantBranding.objects.create(
                tenant=tenant,
                app_name=brand_name,
                primary_color='#0f766e',
                accent_color='#f59e0b'
            )

            # 5. Create Initial Usage Tracker
            TenantUsage.objects.create(
                tenant=tenant,
                active_members_count=0,
                locations_count=1,
                trainers_count=1,
                storage_used_mb=5.0,
                ai_minutes_used=0
            )

            return {
                'tenant': TenantSerializer(tenant).data,
                'location': LocationSerializer(location).data,
                'admin_user_id': admin_user.id,
                'admin_email': admin_user.email,
                'message': 'Tenant organization onboarded successfully.'
            }


class MarketplaceAppSerializer(serializers.ModelSerializer):
    is_installed = serializers.SerializerMethodField()
    installation_id = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceApp
        fields = [
            'id', 'name', 'category', 'icon_text', 'developer',
            'description', 'price_monthly', 'required_tier',
            'is_popular', 'is_active', 'is_installed', 'installation_id',
            'created_at', 'updated_at'
        ]

    def get_is_installed(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return False
        tenant = request.user.tenant if hasattr(request.user, 'tenant') and request.user.tenant else Tenant.objects.first()
        if not tenant:
            return False
        return TenantAppInstallation.objects.filter(tenant=tenant, app=obj, is_active=True).exists()

    def get_installation_id(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return None
        tenant = request.user.tenant if hasattr(request.user, 'tenant') and request.user.tenant else Tenant.objects.first()
        if not tenant:
            return None
        inst = TenantAppInstallation.objects.filter(tenant=tenant, app=obj).first()
        return inst.id if inst else None


class TenantAppInstallationSerializer(serializers.ModelSerializer):
    app_details = MarketplaceAppSerializer(source='app', read_only=True)

    class Meta:
        model = TenantAppInstallation
        fields = [
            'id', 'tenant', 'app', 'app_details', 'is_active',
            'config_data', 'installed_at', 'updated_at'
        ]
        read_only_fields = ['id', 'installed_at', 'updated_at']


