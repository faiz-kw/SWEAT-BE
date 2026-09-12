"""
Tenant core serializers.
"""

from rest_framework import serializers
from .models_org import Organization, CompanyEntity, Location, Branch
from .models_users import TenantUser, Department, UserBranch, UserDepartment
from .models_rbac import Role, RoleAssignment, ModuleCatalog


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'code', 'name', 'legal_name', 'email', 'phone',
                  'country', 'currency', 'timezone', 'status', 'created_at']
        read_only_fields = ['id', 'created_at']


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ['id', 'organization', 'code', 'name', 'city', 'area',
                  'state', 'country', 'postal_code', 'status', 'created_at']
        read_only_fields = ['id', 'created_at']


class BranchSerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(source='location.name', read_only=True)

    class Meta:
        model = Branch
        fields = ['id', 'organization', 'company_entity', 'location', 'location_name', 'code', 'name',
                  'address', 'address_line_1', 'address_line_2', 'latitude', 'longitude', 'timezone',
                  'phone', 'email', 'capacity', 'status', 'created_at']
        read_only_fields = ['id', 'created_at']


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'organization', 'name', 'code', 'description', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class TenantUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantUser
        fields = ['id', 'organization', 'email', 'first_name', 'last_name', 'phone',
                  'status', 'home_branch', 'last_login_at', 'created_at']
        read_only_fields = ['id', 'last_login_at', 'created_at']

    def update(self, instance, validated_data):
        from django.db import transaction
        from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
        from config.routers import get_tenant_db_alias
        from .models_org import Organization

        db_alias = self.context.get('db_alias') or get_tenant_db_alias()
        new_status = validated_data.get('status')
        old_status = instance.status

        # If activating a previously non-counted user, check quota
        if new_status in ('ACTIVE', 'INVITED') and old_status not in ('ACTIVE', 'INVITED'):
            request = self.context.get('request')
            tenant_id = None
            if request and hasattr(request, 'user'):
                tenant_id = getattr(request.user, '_tenant_id', None)
                if not tenant_id and hasattr(request, 'auth') and isinstance(request.auth, dict):
                    tenant_id = request.auth.get('tid')
            if not tenant_id:
                tenant_id = self.context.get('tenant_id')
            if not tenant_id and db_alias:
                from apps.master.models_infra import TenantDataSource
                clean_alias = db_alias.replace('tenant_tenant_', 'tenant_').replace('tenant_', '')
                ds = TenantDataSource.objects.using('default').filter(
                    db_name__icontains=clean_alias
                ).first()
                if ds:
                    tenant_id = str(ds.tenant_id)

            if tenant_id and db_alias:
                with transaction.atomic(using=db_alias):
                    Organization.objects.using(db_alias).select_for_update().get(id=instance.organization_id)
                    try:
                        QuotaChecker.assert_quota_available(
                            tenant_id=tenant_id,
                            metric_code='ACTIVE_USERS',
                            db_alias=db_alias,
                            requested_increment=1,
                            org_id=instance.organization_id,
                        )
                    except QuotaExceededError as qe:
                        raise serializers.ValidationError({
                            'detail': f"Active user quota exceeded ({qe.current_usage}/{qe.limit_value}). Upgrade your plan to add more staff.",
                            'code': 'QUOTA_EXCEEDED',
                            'metric': 'ACTIVE_USERS',
                            'current_usage': qe.current_usage,
                            'limit': qe.limit_value,
                        })
                    except QuotaConfigurationError as qc:
                        raise serializers.ValidationError({
                            'detail': str(qc),
                            'code': 'QUOTA_CONFIG_ERROR',
                        })
                    return super().update(instance, validated_data)

        return super().update(instance, validated_data)


class TenantUserCreateSerializer(TenantUserSerializer):
    password = serializers.CharField(write_only=True)

    class Meta(TenantUserSerializer.Meta):
        fields = TenantUserSerializer.Meta.fields + ['password']

    def create(self, validated_data):
        from rest_framework.exceptions import PermissionDenied
        from django.db import transaction
        from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
        from config.routers import get_tenant_db_alias
        from .models_org import Organization

        db_alias = self.context.get('db_alias') or get_tenant_db_alias()
        if not db_alias:
            raise PermissionDenied(
                'Tenant context is not active for user creation. '
                'Tenant user cannot be created without active tenant DB context.'
            )

        org = validated_data.get('organization')
        if not org:
            raise serializers.ValidationError({'organization': 'Organization is required.'})

        # Resolve tenant_id from auth context
        request = self.context.get('request')
        tenant_id = None
        if request and hasattr(request, 'user'):
            tenant_id = getattr(request.user, '_tenant_id', None)
            if not tenant_id and hasattr(request, 'auth') and isinstance(request.auth, dict):
                tenant_id = request.auth.get('tid')
        if not tenant_id:
            tenant_id = self.context.get('tenant_id')
        if not tenant_id:
            # Fallback lookup via TenantDataSource using db_alias
            from apps.master.models_infra import TenantDataSource
            clean_alias = db_alias.replace('tenant_tenant_', 'tenant_').replace('tenant_', '')
            ds = TenantDataSource.objects.using('default').filter(
                db_name__icontains=clean_alias
            ).first()
            if ds:
                tenant_id = str(ds.tenant_id)

        if not tenant_id:
            raise serializers.ValidationError(
                {'detail': 'Could not resolve tenant context for quota enforcement.'}
            )

        # Concurrency safety: atomic transaction on tenant DB with Organization row lock
        with transaction.atomic(using=db_alias):
            # 1. Lock the authoritative tenant organization row
            locked_org = Organization.objects.using(db_alias).select_for_update().get(id=org.id)

            # 2 & 3 & 4. Count users and resolve/assert Master DB quota
            status = validated_data.get('status', 'INVITED')
            if status in ('ACTIVE', 'INVITED'):
                try:
                    QuotaChecker.assert_quota_available(
                        tenant_id=tenant_id,
                        metric_code='ACTIVE_USERS',
                        db_alias=db_alias,
                        requested_increment=1,
                        org_id=locked_org.id,
                    )
                except QuotaExceededError as qe:
                    raise serializers.ValidationError({
                        'detail': f"Active user quota exceeded ({qe.current_usage}/{qe.limit_value}). Upgrade your plan to add more staff.",
                        'code': 'QUOTA_EXCEEDED',
                        'metric': 'ACTIVE_USERS',
                        'current_usage': qe.current_usage,
                        'limit': qe.limit_value,
                    })
                except QuotaConfigurationError as qc:
                    raise serializers.ValidationError({
                        'detail': str(qc),
                        'code': 'QUOTA_CONFIG_ERROR',
                    })

            # 5. Create user only after quota check passes
            password = validated_data.pop('password')
            user = TenantUser(**validated_data)
            user.set_password(password)
            user.save(using=db_alias)
            return user


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ['id', 'organization', 'department', 'name', 'code', 'description', 'scope', 'is_system', 'is_active', 'created_at']
        read_only_fields = ['id', 'is_system', 'created_at']


class RoleAssignmentSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source='user.email', read_only=True)
    role_name = serializers.CharField(source='role.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)

    class Meta:
        model = RoleAssignment
        fields = ['id', 'organization', 'user', 'user_email', 'role', 'role_name',
                  'scope_type', 'company_entity', 'location', 'branch', 'branch_name',
                  'status', 'is_active', 'assigned_by', 'assigned_at', 'expires_at', 'unassigned_at']
        read_only_fields = ['id', 'organization', 'assigned_at', 'assigned_by']

    def validate(self, attrs):
        # Synchronize status and is_active (status is authoritative)
        if 'status' in attrs:
            attrs['is_active'] = (attrs['status'] == 'ACTIVE')
        elif 'is_active' in attrs:
            attrs['status'] = 'ACTIVE' if attrs['is_active'] else 'INACTIVE'
        return super().validate(attrs)


class ModuleCatalogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModuleCatalog
        fields = ['id', 'module_code', 'source_module_id', 'name', 'description', 'icon', 'is_enabled', 'sort_order']
        read_only_fields = ['id', 'source_module_id']


class RolePermissionSetItemSerializer(serializers.ModelSerializer):
    permission_code = serializers.CharField(source='permission.permission_code', read_only=True)

    class Meta:
        from .models_rbac import RolePermissionSetItem
        model = RolePermissionSetItem
        fields = ['id', 'permission_set', 'permission', 'permission_code', 'granted']


class RolePermissionSetSerializer(serializers.ModelSerializer):
    items = RolePermissionSetItemSerializer(many=True, read_only=True)

    class Meta:
        from .models_rbac import RolePermissionSet
        model = RolePermissionSet
        fields = ['id', 'organization', 'role', 'name', 'scope_type', 'company_entity',
                  'location', 'branch', 'inherits_from', 'is_override', 'description',
                  'is_active', 'created_at', 'items']
        read_only_fields = ['id', 'organization', 'created_at']


class BranchModuleSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        from .models_rbac import BranchModule
        model = BranchModule
        fields = ['id', 'branch', 'branch_name', 'module_code', 'is_enabled', 'enabled_at', 'disabled_at']
        read_only_fields = ['id', 'enabled_at']


class SubmoduleCatalogSerializer(serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.module_code', read_only=True)

    class Meta:
        from .models_rbac import SubmoduleCatalog
        model = SubmoduleCatalog
        fields = ['id', 'module', 'module_code', 'submodule_code', 'name', 'description', 'sort_order', 'is_enabled']


class PermissionSerializer(serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.module_code', read_only=True)
    submodule_code = serializers.CharField(source='submodule.submodule_code', read_only=True, default=None)

    class Meta:
        from .models_rbac import Permission
        model = Permission
        fields = ['id', 'module', 'module_code', 'submodule', 'submodule_code', 'permission_code', 'action', 'label', 'description', 'is_active']


class RoleModuleAccessSerializer(serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.module_code', read_only=True)

    class Meta:
        from .models_rbac import RoleModuleAccess
        model = RoleModuleAccess
        fields = ['id', 'role', 'permission_set', 'module', 'module_code', 'can_access', 'is_visible', 'created_at', 'updated_at']


class RoleSubmoduleAccessSerializer(serializers.ModelSerializer):
    submodule_code = serializers.CharField(source='submodule.submodule_code', read_only=True)

    class Meta:
        from .models_rbac import RoleSubmoduleAccess
        model = RoleSubmoduleAccess
        fields = ['id', 'role', 'permission_set', 'submodule', 'submodule_code', 'can_access', 'is_visible', 'created_at', 'updated_at']


class CompanyEntitySerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyEntity
        fields = ['id', 'organization', 'code', 'name', 'legal_name', 'registration_number', 'tax_registration_number', 'email', 'phone', 'address', 'status', 'created_at']
        read_only_fields = ['id', 'created_at']


class UserBranchSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source='user.email', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = UserBranch
        fields = ['id', 'user', 'user_email', 'branch', 'branch_name', 'scope_type', 'is_active', 'assigned_at', 'unassigned_at']
        read_only_fields = ['id', 'assigned_at']


class OrganizationSettingsSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        from .models_govern import OrganizationSettings
        model = OrganizationSettings
        fields = [
            'id', 'organization', 'organization_name',
            'currency', 'tax_rate_pct', 'tax_id_number',
            'default_timezone', 'language', 'date_format', 'time_format',
            'membership_config', 'booking_config', 'attendance_config',
            'notification_config', 'ai_config',
            'booking_cancellation_window_hours', 'late_cancellation_fee',
            'allow_guest_passes', 'guest_passes_per_month',
            'membership_grace_period_days', 'allow_member_freeze',
            'max_freeze_days_per_year', 'send_booking_reminders',
            'booking_reminder_hours_before', 'send_membership_expiry_alerts',
            'membership_expiry_alert_days', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class BranchSettingsSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        from .models_govern import BranchSettings
        model = BranchSettings
        fields = [
            'id', 'branch', 'branch_name',
            'business_open_time', 'business_close_time',
            'max_booking_capacity', 'booking_cancellation_window_hours',
            'late_cancellation_fee', 'allow_guest_passes',
            'guest_passes_per_month',
            'membership_config', 'booking_config', 'attendance_config',
            'notification_config', 'ai_config',
            'contact_email', 'contact_phone', 'whatsapp_number',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class NotificationTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        from .models_govern import NotificationTemplate
        model = NotificationTemplate
        fields = [
            'id', 'organization', 'branch', 'name', 'channel', 'event_type',
            'event_code', 'language', 'version',
            'subject', 'body', 'is_active', 'is_default',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class TenantAuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source='actor.email', read_only=True, default='')

    class Meta:
        from .models_privacy import TenantAuditEvent
        model = TenantAuditEvent
        fields = [
            'id', 'actor', 'actor_type', 'actor_email',
            'organization_id', 'company_entity_id', 'location_id', 'branch_id',
            'action', 'resource_type', 'resource_id',
            'description', 'before_state', 'after_state',
            'ip_address', 'user_agent', 'request_id', 'correlation_id',
            'source_application', 'created_at',
        ]
        read_only_fields = [
            'id', 'actor', 'actor_type', 'actor_email',
            'organization_id', 'company_entity_id', 'location_id', 'branch_id',
            'action', 'resource_type', 'resource_id',
            'description', 'before_state', 'after_state',
            'ip_address', 'user_agent', 'request_id', 'correlation_id',
            'source_application', 'created_at',
        ]


class BranchWorkingHoursSerializer(serializers.ModelSerializer):
    class Meta:
        from .models_govern import BranchWorkingHours
        model = BranchWorkingHours
        fields = [
            'id', 'branch', 'day_of_week', 'is_open',
            'open_time', 'close_time', 'is_24_hours',
            'created_by', 'updated_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'updated_by', 'created_at', 'updated_at']

    def validate(self, data):
        day_of_week = data.get('day_of_week', getattr(self.instance, 'day_of_week', None))
        if day_of_week is not None and not (1 <= day_of_week <= 7):
            raise serializers.ValidationError({'day_of_week': 'day_of_week must be between 1 (Monday) and 7 (Sunday).'})

        is_open = data.get('is_open', getattr(self.instance, 'is_open', True))
        is_24_hours = data.get('is_24_hours', getattr(self.instance, 'is_24_hours', False))
        open_time = data.get('open_time', getattr(self.instance, 'open_time', None))
        close_time = data.get('close_time', getattr(self.instance, 'close_time', None))

        if is_open and not is_24_hours:
            if not open_time or not close_time:
                raise serializers.ValidationError('open_time and close_time are required when branch is open and not 24 hours.')

        return data


class BranchOperatingExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        from .models_govern import BranchOperatingException
        model = BranchOperatingException
        fields = [
            'id', 'branch', 'exception_date', 'is_closed',
            'open_time', 'close_time', 'reason',
            'created_by', 'updated_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'updated_by', 'created_at', 'updated_at']

    def validate(self, data):
        is_closed = data.get('is_closed', getattr(self.instance, 'is_closed', False))
        open_time = data.get('open_time', getattr(self.instance, 'open_time', None))
        close_time = data.get('close_time', getattr(self.instance, 'close_time', None))

        if not is_closed:
            if not open_time or not close_time:
                raise serializers.ValidationError('open_time and close_time are required when branch exception is not closed.')

        return data



