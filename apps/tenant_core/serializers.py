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
    city = serializers.CharField(source='location.city', read_only=True)
    operating_hours = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = ['id', 'organization', 'company_entity', 'location', 'location_name', 'city', 'code', 'name',
                  'address', 'address_line_1', 'address_line_2', 'latitude', 'longitude',
                  'geofence_radius_meters', 'geofence_enforcement', 'timezone',
                  'phone', 'email', 'capacity', 'business_open_time', 'business_close_time', 'operating_hours',
                  'status', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_operating_hours(self, obj):
        return f"{obj.business_open_time} - {obj.business_close_time}" if obj.business_open_time else "06:00 - 22:00"


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'organization', 'name', 'code', 'description', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class TenantUserSerializer(serializers.ModelSerializer):
    ALLOWED_TRANSITIONS = {
        'INVITED': {'ACTIVE', 'INACTIVE', 'DEACTIVATED'},
        'ACTIVE': {'INACTIVE', 'SUSPENDED', 'BLOCKED', 'DEACTIVATED'},
        'INACTIVE': {'ACTIVE', 'DEACTIVATED'},
        'SUSPENDED': {'ACTIVE', 'DEACTIVATED'},
        'BLOCKED': {'ACTIVE', 'DEACTIVATED'},
        'DEACTIVATED': {'ACTIVE'},
    }

    full_name = serializers.CharField(read_only=True)
    role = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    branch_access = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    departments = serializers.SerializerMethodField()
    home_branch_name = serializers.CharField(source='home_branch.name', read_only=True, default=None)
    active_location_name = serializers.CharField(source='home_branch.name', read_only=True, default=None)
    active_location_id = serializers.CharField(source='home_branch_id', read_only=True, default=None)
    tenant_id = serializers.SerializerMethodField()
    tenant_name = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(source='is_accessible', read_only=True)
    reports_to_id = serializers.SerializerMethodField()
    reports_to_name = serializers.SerializerMethodField()

    class Meta:
        model = TenantUser
        fields = [
            'id', 'organization', 'email', 'first_name', 'last_name', 'phone',
            'status', 'is_login_allowed', 'home_branch', 'deactivated_at',
            'deactivated_by', 'deactivation_reason', 'suspended_until',
            'last_login_at', 'created_at',
            'full_name', 'role', 'role_name', 'roles', 'branch_access', 'department', 'departments',
            'home_branch_name', 'active_location_name', 'active_location_id', 'tenant_id', 'tenant_name', 'is_active',
            'reports_to_id', 'reports_to_name',
        ]
        read_only_fields = ['id', 'last_login_at', 'created_at', 'deactivated_at', 'deactivated_by']
        extra_kwargs = {
            'organization': {'required': False},
        }

    def get_reports_to_id(self, obj):
        try:
            profile = getattr(obj, 'profile', None)
            emp = getattr(profile, 'employee_profile', None) if profile else None
            mgr = emp.reporting_manager if emp else None
            if mgr:
                mgr_user = mgr.user_profile.user if (mgr.user_profile and mgr.user_profile.user) else None
                return str(mgr_user.id) if mgr_user else str(mgr.id)
        except Exception:
            pass
        return None

    def get_reports_to_name(self, obj):
        try:
            profile = getattr(obj, 'profile', None)
            emp = getattr(profile, 'employee_profile', None) if profile else None
            mgr = emp.reporting_manager if emp else None
            if mgr:
                mgr_user = mgr.user_profile.user if (mgr.user_profile and mgr.user_profile.user) else None
                if mgr_user:
                    return mgr_user.full_name or mgr_user.email
                return mgr.employee_code or 'Supervisor'
        except Exception:
            pass
        return None

    def get_tenant_id(self, obj):
        return str(obj.organization_id) if obj.organization_id else ''

    def get_tenant_name(self, obj):
        try:
            return obj.organization.name if obj.organization else ''
        except Exception:
            return ''

    def get_role(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_rbac import RoleAssignment
            ra = RoleAssignment.objects.using(db).filter(user=obj, is_active=True).select_related('role').first()
            if ra and ra.role:
                return ra.role.code
        except Exception:
            pass
        return 'STAFF'

    def get_role_name(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_rbac import RoleAssignment
            ra = RoleAssignment.objects.using(db).filter(user=obj, is_active=True).select_related('role').first()
            if ra and ra.role:
                return ra.role.name
        except Exception:
            pass
        return 'Staff Member'

    def get_branch_access(self, obj):
        from .models_rbac import RoleAssignment
        rows = RoleAssignment.objects.using(obj._state.db).filter(
            user=obj, role__scope='BRANCH', branch__isnull=False,
        ).select_related('branch')
        # Older edits may leave multiple historical assignments. Active wins.
        access = {}
        for row in rows:
            key = (str(row.role_id), str(row.branch_id))
            enabled = row.is_active and row.status == 'ACTIVE'
            if key not in access or enabled:
                access[key] = {'role_id': key[0], 'branch_id': key[1],
                               'branch_name': row.branch.name, 'enabled': enabled}
        return list(access.values())

    def get_roles(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_rbac import RoleAssignment
            ras = RoleAssignment.objects.using(db).filter(user=obj, is_active=True).select_related('role', 'branch')
            return [{
                'id': str(ra.role.id),
                'code': ra.role.code,
                'name': ra.role.name,
                'scope': ra.role.scope,
                'branch_id': str(ra.branch.id) if ra.branch else None,
                'branch_name': ra.branch.name if ra.branch else None,
            } for ra in ras if ra.role]
        except Exception:
            return []

    def get_department(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_users import UserDepartment
            ud = UserDepartment.objects.using(db).filter(user=obj, status='ACTIVE').select_related('department').first()
            if ud and ud.department:
                return ud.department.name
        except Exception:
            pass
        return ''

    def get_departments(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_users import UserDepartment
            uds = UserDepartment.objects.using(db).filter(user=obj, status='ACTIVE').select_related('department')
            return [{'id': str(ud.department.id), 'name': ud.department.name, 'code': ud.department.code} for ud in uds if ud.department]
        except Exception:
            return []

    def validate(self, attrs):
        from apps.master.services_auth_directory import check_identifier_available
        subject_id = self.instance.id if self.instance else None

        email = attrs.get('email')
        if email and not check_identifier_available(email, exclude_subject_id=subject_id, account_type='TENANT', db=self.context.get('db_alias') or getattr(getattr(self.instance, '_state', None), 'db', None)):
            raise serializers.ValidationError({
                'email': 'This username or email is already registered.'
            })

        username = attrs.get('username')
        if username and not check_identifier_available(username, exclude_subject_id=subject_id, account_type='TENANT', db=self.context.get('db_alias') or getattr(getattr(self.instance, '_state', None), 'db', None)):
            raise serializers.ValidationError({
                'username': 'This username or email is already registered.'
            })

        return super().validate(attrs)

    def update(self, instance, validated_data):
        from django.db import transaction
        from django.utils import timezone
        from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
        from config.routers import get_tenant_db_alias
        from .models_org import Organization
        from .models_users import TenantUser

        db_alias = self.context.get('db_alias') or get_tenant_db_alias()
        new_status = validated_data.get('status')
        old_status = instance.status

        # Enforce state transition matrix if status is changing
        if new_status and new_status != old_status:
            allowed = self.ALLOWED_TRANSITIONS.get(old_status, set())
            if new_status not in allowed:
                raise serializers.ValidationError({
                    'status': f"Invalid lifecycle transition from '{old_status}' to '{new_status}'."
                })

            # Handle DEACTIVATED transition audit fields
            if new_status == 'DEACTIVATED':
                validated_data['deactivated_at'] = timezone.now()
                request = self.context.get('request')
                if request and hasattr(request, 'user') and hasattr(request.user, 'id'):
                    try:
                        actor = TenantUser.objects.using(db_alias).filter(id=request.user.id).first()
                        if actor:
                            validated_data['deactivated_by'] = actor
                    except Exception:
                        pass

            # Handle SUSPENDED -> ACTIVE explicit transition
            if new_status == 'ACTIVE' and old_status == 'SUSPENDED':
                validated_data['suspended_until'] = None

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

        # Update reporting manager if reports_to_id is provided in payload
        if hasattr(self, 'initial_data') and 'reports_to_id' in self.initial_data:
            reports_to_param = self.initial_data.get('reports_to_id')
            import uuid
            from .models_workforce import UserProfile, EmployeeProfile
            db_alias = getattr(getattr(instance, '_state', None), 'db', None) or 'default'
            user_profile, _ = UserProfile.objects.using(db_alias).get_or_create(
                user=instance,
                defaults={'first_name_snapshot': instance.first_name, 'last_name_snapshot': instance.last_name}
            )
            mgr_emp = None
            if reports_to_param and str(reports_to_param).strip() and str(reports_to_param).strip().lower() not in ('none', 'unassigned', ''):
                try:
                    mgr_user = TenantUser.objects.using(db_alias).filter(id=uuid.UUID(str(reports_to_param))).first()
                    if mgr_user:
                        mgr_prof, _ = UserProfile.objects.using(db_alias).get_or_create(
                            user=mgr_user,
                            defaults={'first_name_snapshot': mgr_user.first_name, 'last_name_snapshot': mgr_user.last_name}
                        )
                        mgr_emp, _ = EmployeeProfile.objects.using(db_alias).get_or_create(
                            user_profile=mgr_prof,
                            organization=instance.organization,
                            defaults={'employee_code': f"EMP-{mgr_user.id.hex[:6].upper()}"}
                        )
                except Exception:
                    mgr_emp = None

            emp_profile, _ = EmployeeProfile.objects.using(db_alias).get_or_create(
                user_profile=user_profile,
                organization=instance.organization,
                defaults={
                    'employee_code': f"EMP-{instance.id.hex[:6].upper()}",
                    'reporting_manager': mgr_emp,
                }
            )
            if emp_profile.reporting_manager != mgr_emp:
                emp_profile.reporting_manager = mgr_emp
                emp_profile.save(using=db_alias, update_fields=['reporting_manager'])

        return super().update(instance, validated_data)


class TenantUserCreateSerializer(TenantUserSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, default='')
    role = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    department_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    branch = serializers.CharField(write_only=True, required=False, allow_blank=True)
    branch_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    home_branch = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    reports_to_id = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)

    class Meta(TenantUserSerializer.Meta):
        fields = TenantUserSerializer.Meta.fields + ['password', 'role', 'role_id', 'department_id', 'branch', 'branch_id', 'reports_to_id']

    def validate_password(self, value):
        if not value:
            return value
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['role'] = self.get_role(instance)
        ret['role_name'] = self.get_role_name(instance)
        return ret

    def create(self, validated_data):
        import uuid
        from rest_framework.exceptions import PermissionDenied
        from django.db import transaction
        from django.utils import timezone
        from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
        from config.routers import get_tenant_db_alias
        from .models_org import Organization, Branch
        from .models_users import TenantUser, UserDepartment, UserBranch, Department
        from .models_rbac import Role, RoleAssignment

        db_alias = self.context.get('db_alias') or get_tenant_db_alias()
        if not db_alias:
            raise PermissionDenied(
                'Tenant context is not active for user creation. '
                'Tenant user cannot be created without active tenant DB context.'
            )

        org = validated_data.get('organization')
        if not org:
            org = Organization.objects.using(db_alias).first()
            if org:
                validated_data['organization'] = org
            else:
                raise serializers.ValidationError({'organization': 'Organization is required.'})

        # Pop write-only relational fields before constructing user
        role_param = validated_data.pop('role', None)
        role_id_param = validated_data.pop('role_id', None)
        dept_id_param = validated_data.pop('department_id', None)
        reports_to_id_param = validated_data.pop('reports_to_id', None)
        # Consume every alias: the frontend sends all three. Short-circuiting
        # pop() leaves branch_id in the model kwargs and causes a TypeError.
        branch_value = validated_data.pop('branch', None)
        branch_id_value = validated_data.pop('branch_id', None)
        home_branch_value = validated_data.pop('home_branch', None)
        branch_param = branch_value or branch_id_value or home_branch_value

        # Resolve home_branch from branch_param if provided
        if branch_param:
            if isinstance(branch_param, Branch):
                validated_data['home_branch'] = branch_param
            else:
                branch_obj = None
                try:
                    branch_obj = Branch.objects.using(db_alias).filter(id=uuid.UUID(str(branch_param))).first()
                except Exception:
                    branch_obj = Branch.objects.using(db_alias).filter(name__iexact=str(branch_param)).first()
                if branch_obj:
                    validated_data['home_branch'] = branch_obj

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
            raw_password = validated_data.pop('password', '')
            if not raw_password:
                validated_data['status'] = 'INVITED'
                validated_data['invited_at'] = timezone.now()
            else:
                if 'status' not in validated_data:
                    validated_data['status'] = 'ACTIVE'
                validated_data['activated_at'] = timezone.now()

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
            user = TenantUser(**validated_data)
            if raw_password:
                user.set_password(raw_password)
            else:
                user.set_password(uuid.uuid4().hex)
            user.save(using=db_alias)

            # 6. Create RoleAssignment if role specified — STRICT: No silent fallback
            target_role = None
            if role_id_param:
                try:
                    target_role = Role.objects.using(db_alias).filter(
                        id=uuid.UUID(str(role_id_param)),
                        organization=org,
                        is_active=True,
                        status='ACTIVE',
                    ).first()
                except Exception:
                    target_role = Role.objects.using(db_alias).filter(
                        code=str(role_id_param),
                        organization=org,
                        is_active=True,
                        status='ACTIVE',
                    ).first()
                if not target_role:
                    raise serializers.ValidationError({
                        'role_id': f"Active role '{role_id_param}' not found in current organization."
                    })

            elif role_param:
                target_role = (
                    Role.objects.using(db_alias).filter(code__iexact=str(role_param), organization=org, is_active=True, status='ACTIVE').first() or
                    Role.objects.using(db_alias).filter(name__iexact=str(role_param), organization=org, is_active=True, status='ACTIVE').first()
                )
                if not target_role:
                    raise serializers.ValidationError({
                        'role': f"Active role '{role_param}' not found in current organization."
                    })

            # Safeguard actor FK constraint on tenant DB
            actor = None
            if request and hasattr(request, 'user') and hasattr(request.user, 'id'):
                try:
                    actor = TenantUser.objects.using(db_alias).filter(id=request.user.id).first()
                except Exception:
                    actor = None

            if target_role:
                RoleAssignment.objects.using(db_alias).create(
                    organization=org,
                    user=user,
                    role=target_role,
                    branch=user.home_branch,
                    scope_type='BRANCH' if user.home_branch else 'ORGANIZATION',
                    status='ACTIVE',
                    is_active=True,
                    assigned_by=actor,
                )

            # 7. Create UserDepartment if specified
            if dept_id_param:
                dept = None
                try:
                    dept = Department.objects.using(db_alias).filter(id=uuid.UUID(str(dept_id_param))).first()
                except Exception:
                    dept = (
                        Department.objects.using(db_alias).filter(code__iexact=str(dept_id_param)).first() or
                        Department.objects.using(db_alias).filter(name__iexact=str(dept_id_param)).first()
                    )
                if dept:
                    UserDepartment.objects.using(db_alias).create(
                        user=user,
                        department=dept,
                        is_primary=True,
                        status='ACTIVE',
                    )

            # 8. Create UserBranch if home_branch is set
            if user.home_branch:
                UserBranch.objects.using(db_alias).get_or_create(
                    user=user,
                    branch=user.home_branch,
                    scope_type='HOME',
                    defaults={'is_active': True, 'status': 'ACTIVE', 'is_primary': True, 'relationship_type': 'PRIMARY'},
                )

            # 9. Handle Reporting Manager & EmployeeProfile (Optional for non-Org Admin roles)
            from .models_workforce import UserProfile, EmployeeProfile
            user_profile, _ = UserProfile.objects.using(db_alias).get_or_create(
                user=user,
                defaults={
                    'first_name_snapshot': user.first_name,
                    'last_name_snapshot': user.last_name,
                }
            )
            mgr_emp = None
            role_code = (target_role.code if target_role else str(role_param or '')).upper()
            if role_code not in ('ORG_ADMIN', 'SUPER_ADMIN', 'ORGANIZATION_ADMINISTRATOR') and reports_to_id_param:
                if str(reports_to_id_param).strip().lower() not in ('none', 'unassigned', ''):
                    try:
                        mgr_user = TenantUser.objects.using(db_alias).filter(id=uuid.UUID(str(reports_to_id_param))).first()
                        if mgr_user:
                            mgr_prof, _ = UserProfile.objects.using(db_alias).get_or_create(
                                user=mgr_user,
                                defaults={'first_name_snapshot': mgr_user.first_name, 'last_name_snapshot': mgr_user.last_name}
                            )
                            mgr_emp, _ = EmployeeProfile.objects.using(db_alias).get_or_create(
                                user_profile=mgr_prof,
                                organization=org,
                                defaults={'employee_code': f"EMP-{mgr_user.id.hex[:6].upper()}"}
                            )
                    except Exception:
                        mgr_emp = None

            emp_profile, _ = EmployeeProfile.objects.using(db_alias).get_or_create(
                user_profile=user_profile,
                organization=org,
                defaults={
                    'employee_code': f"EMP-{user.id.hex[:6].upper()}",
                    'reporting_manager': mgr_emp,
                }
            )
            if emp_profile.reporting_manager != mgr_emp:
                emp_profile.reporting_manager = mgr_emp
                emp_profile.save(using=db_alias, update_fields=['reporting_manager'])

            return user


class RoleSerializer(serializers.ModelSerializer):
    users_count = serializers.SerializerMethodField()
    permission_set_id = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    module_access = serializers.SerializerMethodField()
    submodule_access = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            'id', 'organization', 'department', 'name', 'code', 'description',
            'scope', 'is_system', 'is_active', 'created_at',
            'users_count', 'permission_set_id', 'permissions', 'module_access', 'submodule_access',
        ]
        read_only_fields = ['id', 'is_system', 'created_at']
        validators = []
        extra_kwargs = {
            'organization': {'required': False, 'allow_null': True},
            'department': {'required': False, 'allow_null': True},
        }

    def validate(self, attrs):
        if not attrs.get('organization'):
            request = self.context.get('request')
            db = getattr(request.user, '_db_alias', None) if request and hasattr(request, 'user') else None
            if not db:
                from config.routers import get_tenant_db_alias
                db = get_tenant_db_alias() or 'default'
            from .models_org import Organization
            attrs['organization'] = Organization.objects.using(db).first()

        if self.instance is None and attrs.get('organization') and attrs.get('code'):
            db = attrs['organization']._state.db or 'default'
            if Role.objects.using(db).filter(organization=attrs['organization'], code=attrs['code']).exists():
                raise serializers.ValidationError({'code': f"Role with code '{attrs['code']}' already exists for this organization."})
        return attrs

    def get_users_count(self, obj):
        db = obj._state.db or 'default'
        try:
            from .models_rbac import RoleAssignment
            return RoleAssignment.objects.using(db).filter(role=obj, is_active=True).count()
        except Exception:
            return 0

    def get_permission_set_id(self, obj):
        try:
            rps = obj.permission_sets.filter(is_active=True).first()
            return str(rps.id) if rps else None
        except Exception:
            return None

    def get_permissions(self, obj):
        try:
            rps = obj.permission_sets.filter(is_active=True).first()
            if not rps:
                return []
            return [
                {
                    'permission_id': str(item.permission.id),
                    'permission_code': item.permission.permission_code,
                    'permission': {
                        'id': str(item.permission.id),
                        'code': item.permission.permission_code,
                        'label': item.permission.label,
                        'action': item.permission.action,
                        'module': item.permission.module.module_code if item.permission.module else '',
                        'submodule': item.permission.submodule.submodule_code if item.permission.submodule else '',
                    },
                    'granted': item.granted,
                }
                for item in rps.items.select_related('permission', 'permission__module', 'permission__submodule').all()
            ]
        except Exception:
            return []

    def get_module_access(self, obj):
        try:
            return [
                {
                    'module_code': ma.module.module_code,
                    'can_access': ma.can_access,
                    'is_visible': ma.is_visible,
                }
                for ma in obj.module_access.select_related('module').all()
            ]
        except Exception:
            return []

    def get_submodule_access(self, obj):
        try:
            return [
                {
                    'module_code': sa.submodule.module.module_code if sa.submodule.module else '',
                    'submodule_code': sa.submodule.submodule_code,
                    'can_access': sa.can_access,
                    'is_visible': sa.is_visible,
                }
                for sa in obj.submodule_access.select_related('submodule', 'submodule__module').all()
            ]
        except Exception:
            return []


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
    module_name = serializers.CharField(source='module.name', read_only=True, default='')
    submodule_code = serializers.CharField(source='submodule.submodule_code', read_only=True, default=None)
    submodule_name = serializers.CharField(source='submodule.name', read_only=True, default='')
    code = serializers.CharField(source='permission_code', read_only=True)

    class Meta:
        from .models_rbac import Permission
        model = Permission
        fields = [
            'id', 'module', 'module_code', 'module_name',
            'submodule', 'submodule_code', 'submodule_name',
            'permission_code', 'code', 'action', 'label', 'description', 'is_active'
        ]


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
            'has_split_shift', 'open_time_2', 'close_time_2',
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
        has_split_shift = data.get('has_split_shift', getattr(self.instance, 'has_split_shift', False))
        open_time_2 = data.get('open_time_2', getattr(self.instance, 'open_time_2', None))
        close_time_2 = data.get('close_time_2', getattr(self.instance, 'close_time_2', None))

        if is_open and not is_24_hours:
            if not open_time or not close_time:
                raise serializers.ValidationError('open_time and close_time are required when branch is open and not 24 hours.')
            if has_split_shift:
                if not open_time_2 or not close_time_2:
                    raise serializers.ValidationError('open_time_2 and close_time_2 are required when split shift is enabled.')

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



