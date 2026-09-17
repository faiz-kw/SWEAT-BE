"""
Master DB — Platform IAM Models (11 Tables)
Tables: platform_users, platform_departments, platform_user_departments,
        platform_roles, platform_modules, platform_submodules, platform_permissions,
        platform_role_module_access, platform_role_submodule_access,
        platform_role_permissions, platform_user_roles
"""

import uuid
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from .managers import PlatformUserManager


class PlatformUser(AbstractBaseUser, PermissionsMixin):
    """
    Internal SaaS platform team member (your employees).
    Stored in Master DB. NOT tenant end-users.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=True, null=True, blank=True)
    email = models.EmailField(max_length=320, unique=True)
    phone = models.CharField(max_length=30, blank=True, null=True, default='')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    avatar_url = models.TextField(blank=True, default='')

    STATUS_CHOICES = [
        ('INVITED', 'Invited'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
    ]
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='INVITED')
    is_mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=128, blank=True, default='')
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    # Django internals
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_superuser = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    objects = PlatformUserManager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_users'
        ordering = ['email']

    def __str__(self):
        return f"{self.first_name} {self.last_name} <{self.email}>"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def save(self, *args, **kwargs):
        from .services_auth_directory import check_identifier_available
        from django.core.exceptions import ValidationError
        if self.email and not check_identifier_available(self.email, exclude_subject_id=self.id):
            raise ValidationError({'email': 'This username or email is already registered.'})
        if getattr(self, 'username', None) and not check_identifier_available(self.username, exclude_subject_id=self.id):
            raise ValidationError({'username': 'This username or email is already registered.'})
        super().save(*args, **kwargs)
        try:
            from .services_auth_directory import sync_platform_user_identity
            sync_platform_user_identity(self)
        except Exception:
            pass


class PlatformDepartment(models.Model):
    """
    Organizational departments for platform team (e.g. Engineering, Support, Sales).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_departments'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class PlatformUserDepartmentQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'user':
                t['platform_user'] = v
            elif k.startswith('user__'):
                t['platform_user__' + k[6:]] = v
            elif k == 'joined_at':
                t['assigned_at'] = v
            elif k.startswith('joined_at__'):
                t['assigned_at__' + k[11:]] = v
            elif k == 'left_at':
                t['unassigned_at'] = v
            elif k.startswith('left_at__'):
                t['unassigned_at__' + k[9:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, **kwargs):
        defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)


class PlatformUserDepartment(models.Model):
    """
    M2M: Platform users ↔ Platform departments (with role within department).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform_user = models.ForeignKey(
        PlatformUser, on_delete=models.RESTRICT, db_column='platform_user_id',
        related_name='department_memberships'
    )
    department = models.ForeignKey(
        PlatformDepartment, on_delete=models.RESTRICT, related_name='members'
    )
    is_primary = models.BooleanField(default=False)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_department_head = models.BooleanField(default=False)
    assigned_at = models.DateTimeField(default=timezone.now, db_column='assigned_at')
    unassigned_at = models.DateTimeField(null=True, blank=True, db_column='unassigned_at')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformUserDepartmentQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_user_departments'
        unique_together = ('platform_user', 'department')

    def __init__(self, *args, **kwargs):
        if 'user' in kwargs and 'platform_user' not in kwargs:
            kwargs['platform_user'] = kwargs.pop('user')
        if 'joined_at' in kwargs and 'assigned_at' not in kwargs:
            kwargs['assigned_at'] = kwargs.pop('joined_at')
        if 'left_at' in kwargs and 'unassigned_at' not in kwargs:
            kwargs['unassigned_at'] = kwargs.pop('left_at')
        super().__init__(*args, **kwargs)

    @property
    def user(self):
        return self.platform_user

    @user.setter
    def user(self, val):
        self.platform_user = val

    @property
    def user_id(self):
        return self.platform_user_id

    @user_id.setter
    def user_id(self, val):
        self.platform_user_id = val

    @property
    def joined_at(self):
        return self.assigned_at

    @joined_at.setter
    def joined_at(self, val):
        self.assigned_at = val

    @property
    def left_at(self):
        return self.unassigned_at

    @left_at.setter
    def left_at(self, val):
        self.unassigned_at = val

    def __str__(self):
        return f"{self.user.email} → {self.department.code}"


class PlatformRoleQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'is_system':
                t['is_system_role'] = v
            elif k.startswith('is_system__'):
                t['is_system_role__' + k[11:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().update_or_create(**tr_kwargs)


class PlatformRole(models.Model):
    """
    Roles for platform staff (e.g. Super Admin, Support Agent, Finance Officer).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        PlatformDepartment, on_delete=models.SET_NULL, null=True, blank=True, related_name='roles'
    )
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    is_system_role = models.BooleanField(default=False, db_column='is_system_role', help_text='System roles cannot be deleted')
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformRoleQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_roles'
        ordering = ['name']

    def __init__(self, *args, **kwargs):
        if 'is_system' in kwargs and 'is_system_role' not in kwargs:
            kwargs['is_system_role'] = kwargs.pop('is_system')
        super().__init__(*args, **kwargs)

    @property
    def is_system(self):
        return self.is_system_role

    @is_system.setter
    def is_system(self, val):
        self.is_system_role = val

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class PlatformModule(models.Model):
    """
    Platform admin panel modules (e.g. Tenant Management, Billing, Marketplace).
    Controls what sections of the platform admin panel are visible to platform roles.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    icon = models.CharField(max_length=100, blank=True, default='')
    display_order = models.IntegerField(default=0, db_column='display_order')
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_modules'
        ordering = ['display_order', 'name']

    def __init__(self, *args, **kwargs):
        if 'sort_order' in kwargs and 'display_order' not in kwargs:
            kwargs['display_order'] = kwargs.pop('sort_order')
        super().__init__(*args, **kwargs)

    @property
    def sort_order(self):
        return self.display_order

    @sort_order.setter
    def sort_order(self, val):
        self.display_order = val

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class PlatformSubmodule(models.Model):
    """
    Sub-sections within platform modules (e.g. Tenant Management → Onboarding).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    module = models.ForeignKey(PlatformModule, on_delete=models.RESTRICT, related_name='submodules')
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    display_order = models.IntegerField(default=0, db_column='display_order')
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_submodules'
        unique_together = ('module', 'code')
        ordering = ['module', 'display_order', 'name']

    def __init__(self, *args, **kwargs):
        if 'sort_order' in kwargs and 'display_order' not in kwargs:
            kwargs['display_order'] = kwargs.pop('sort_order')
        super().__init__(*args, **kwargs)

    @property
    def sort_order(self):
        return self.display_order

    @sort_order.setter
    def sort_order(self, val):
        self.display_order = val

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.module.code} → {self.code}"


class PlatformPermissionQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'name':
                t['label'] = v
            elif k.startswith('name__'):
                t['label__' + k[6:]] = v
            elif k == 'module' and isinstance(v, str):
                mod, _ = PlatformModule.objects.using(self.db).get_or_create(
                    code=v, defaults={'name': v.capitalize(), 'is_active': True}
                )
                t['module'] = mod
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().update_or_create(**tr_kwargs)


class PlatformPermission(models.Model):
    """
    Granular action permissions for platform modules (e.g. tenants.create, billing.export).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    module = models.ForeignKey(PlatformModule, on_delete=models.RESTRICT, related_name='permissions')
    submodule = models.ForeignKey(PlatformSubmodule, on_delete=models.SET_NULL, related_name='permissions', null=True, blank=True)
    action = models.CharField(max_length=50, help_text='e.g. view, create, edit, delete, export, manage')
    code = models.CharField(max_length=150, unique=True, help_text='e.g. tenants.create')
    label = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='', null=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformPermissionQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_permissions'
        ordering = ['module', 'action']

    def __init__(self, *args, **kwargs):
        if 'name' in kwargs and 'label' not in kwargs:
            kwargs['label'] = kwargs.pop('name')
        super().__init__(*args, **kwargs)

    @property
    def name(self):
        return self.label

    @name.setter
    def name(self, val):
        self.label = val

    def __str__(self):
        return f"{self.code} — {self.label or self.action}"


class PlatformRoleModuleAccessQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'can_access':
                t['is_visible'] = v
            elif k.startswith('can_access__'):
                t['is_visible__' + k[12:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().update_or_create(**tr_kwargs)


class PlatformRoleModuleAccess(models.Model):
    """
    Controls which platform modules are visible for a platform role.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(PlatformRole, on_delete=models.RESTRICT, related_name='module_access')
    module = models.ForeignKey(PlatformModule, on_delete=models.RESTRICT, related_name='role_access')
    is_visible = models.BooleanField(default=False, db_column='is_visible')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformRoleModuleAccessQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_role_module_access'
        unique_together = ('role', 'module')

    def __init__(self, *args, **kwargs):
        if 'can_access' in kwargs and 'is_visible' not in kwargs:
            kwargs['is_visible'] = kwargs.pop('can_access')
        super().__init__(*args, **kwargs)

    @property
    def can_access(self):
        return self.is_visible

    @can_access.setter
    def can_access(self, val):
        self.is_visible = val

    def __str__(self):
        return f"{self.role.code} → {self.module.code}: {'✓' if self.is_visible else '✗'}"


class PlatformRoleSubmoduleAccessQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'can_access':
                t['is_visible'] = v
            elif k.startswith('can_access__'):
                t['is_visible__' + k[12:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().update_or_create(**tr_kwargs)


class PlatformRoleSubmoduleAccess(models.Model):
    """
    Controls which platform submodules are visible for a platform role.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(PlatformRole, on_delete=models.RESTRICT, related_name='submodule_access')
    submodule = models.ForeignKey(PlatformSubmodule, on_delete=models.RESTRICT, related_name='role_access')
    is_visible = models.BooleanField(default=False, db_column='is_visible')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformRoleSubmoduleAccessQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_role_submodule_access'
        unique_together = ('role', 'submodule')

    def __init__(self, *args, **kwargs):
        if 'can_access' in kwargs and 'is_visible' not in kwargs:
            kwargs['is_visible'] = kwargs.pop('can_access')
        super().__init__(*args, **kwargs)

    @property
    def can_access(self):
        return self.is_visible

    @can_access.setter
    def can_access(self, val):
        self.is_visible = val

    def __str__(self):
        return f"{self.role.code} → {self.submodule.code}: {'✓' if self.is_visible else '✗'}"


class PlatformRolePermissionQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'granted':
                t['is_allowed'] = v
            elif k.startswith('granted__'):
                t['is_allowed__' + k[9:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        if defaults is None:
            defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().update_or_create(**tr_kwargs)


class PlatformRolePermission(models.Model):
    """
    Grants specific action permissions to platform roles.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(PlatformRole, on_delete=models.RESTRICT, related_name='permissions')
    permission = models.ForeignKey(PlatformPermission, on_delete=models.RESTRICT, related_name='role_grants')
    is_allowed = models.BooleanField(default=True, db_column='is_allowed')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformRolePermissionQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_role_permissions'
        unique_together = ('role', 'permission')

    def __init__(self, *args, **kwargs):
        if 'granted' in kwargs and 'is_allowed' not in kwargs:
            kwargs['is_allowed'] = kwargs.pop('granted')
        super().__init__(*args, **kwargs)

    @property
    def granted(self):
        return self.is_allowed

    @granted.setter
    def granted(self, val):
        self.is_allowed = val

    def __str__(self):
        return f"{self.role.code} → {self.permission.code}: {'granted' if self.is_allowed else 'denied'}"


class PlatformUserRoleQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'user':
                t['platform_user'] = v
            elif k.startswith('user__'):
                t['platform_user__' + k[6:]] = v
            else:
                t[k] = v
        return t

    def filter(self, *args, **kwargs):
        return super().filter(*args, **self._translate_kwargs(kwargs))

    def create(self, **kwargs):
        return super().create(**self._translate_kwargs(kwargs))

    def get_or_create(self, **kwargs):
        defaults = kwargs.pop('defaults', None)
        tr_kwargs = self._translate_kwargs(kwargs)
        if defaults:
            tr_kwargs['defaults'] = self._translate_kwargs(defaults)
        return super().get_or_create(**tr_kwargs)


class PlatformUserRole(models.Model):
    """
    Assigns platform roles to platform users.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform_user = models.ForeignKey(
        PlatformUser, on_delete=models.RESTRICT, db_column='platform_user_id',
        related_name='role_assignments'
    )
    role = models.ForeignKey(PlatformRole, on_delete=models.RESTRICT, related_name='user_assignments')
    assigned_at = models.DateTimeField(default=timezone.now)
    assigned_by = models.ForeignKey(
        PlatformUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='role_assignments_made'
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformUserRoleQuerySet.as_manager()

    class Meta:
        app_label = 'master'
        db_table = 'platform_user_roles'
        unique_together = ('platform_user', 'role')

    def __init__(self, *args, **kwargs):
        if 'user' in kwargs and 'platform_user' not in kwargs:
            kwargs['platform_user'] = kwargs.pop('user')
        super().__init__(*args, **kwargs)

    @property
    def user(self):
        return self.platform_user

    @user.setter
    def user(self, val):
        self.platform_user = val

    @property
    def platform_user_id(self):
        return self.platform_user.id if self.platform_user else None

    @property
    def user_id(self):
        return self.platform_user_id

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.email} → {self.role.code}"


class AuthenticationIdentity(models.Model):
    """
    Universal Authentication Routing Directory (Master/Control DB only).
    Routes an identifier (email or username) to either:
      - A PlatformUser in Master DB ('default')
      - A TenantUser in the tenant's dedicated DB (via tenant_id)
    NEVER stores passwords, password hashes, or tenant DB secrets.
    """
    IDENTIFIER_TYPES = [
        ('EMAIL', 'Email Address'),
        ('USERNAME', 'Username'),
    ]
    ACCOUNT_TYPES = [
        ('PLATFORM', 'Platform User'),
        ('TENANT', 'Tenant User'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('INVITED', 'Invited'),
        ('SUSPENDED', 'Suspended'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # HMAC-SHA256 hex digest of normalized identifier
    lookup_hash = models.CharField(max_length=64, unique=True, db_index=True)
    identifier_type = models.CharField(max_length=20, choices=IDENTIFIER_TYPES, default='EMAIL')
    identifier = models.CharField(max_length=320, db_index=True)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    # Subject UUID: PlatformUser.id or TenantUser.id
    subject_id = models.UUIDField(db_index=True)
    # Tenant UUID: None for Platform users; Tenant.id for Tenant users
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'authentication_identities'
        indexes = [
            models.Index(fields=['lookup_hash'], name='idx_auth_ident_hash'),
            models.Index(fields=['account_type', 'subject_id'], name='idx_auth_ident_subj'),
            models.Index(fields=['tenant_id', 'status'], name='idx_auth_ident_tenant'),
            models.Index(fields=['identifier'], name='idx_auth_ident_val'),
        ]

    def __str__(self):
        return f"{self.identifier} ({self.account_type}) [{self.status}]"

