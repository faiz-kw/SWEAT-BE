"""
Dedicated Tenant DB — Users, Departments & Branch Assignments (4 Tables)
Tables: users, user_branches, departments, user_departments

These are tenant end-users (staff, trainers, admins) — NOT platform users.
"""

import uuid
from django.contrib.auth.hashers import make_password, check_password as django_check_password
from django.db import models
from django.utils import timezone
from .models_org import Branch, Organization


class Department(models.Model):
    """
    Organizational departments within a tenant (e.g. Sales, Operations, Nutrition, Training).
    Created and managed by the Tenant Org Admin.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='departments')
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, help_text='Stable code, unique per organization')
    description = models.TextField(blank=True, default='', null=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'tenant_core.TenantUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_departments'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'departments'
        unique_together = ('organization', 'code')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class TenantUser(models.Model):
    """
    Staff/admin users within a tenant's dedicated database.
    These are NOT platform users (PlatformUser). Completely separate identity store.
    Includes gym managers, trainers, front desk, nutrition coaches, etc.
    """
    STATUS = [
        ('INVITED', 'Invited'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
        ('BLOCKED', 'Blocked'),
        ('DEACTIVATED', 'Deactivated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='users')
    username = models.CharField(max_length=150, unique=True, null=True, blank=True)
    email = models.EmailField(max_length=320, unique=True)
    phone = models.CharField(max_length=30, blank=True, default='', null=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    display_name = models.CharField(max_length=150, blank=True, default='')
    gender = models.CharField(max_length=20, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    profile_file_id = models.ForeignKey(
        'tenant_core.File', on_delete=models.SET_NULL, null=True, blank=True,
        db_column='profile_file_id', related_name='profile_users'
    )
    avatar_url = models.TextField(blank=True, default='')
    password_hash = models.TextField(default='')
    user_type = models.CharField(max_length=30, default='STAFF')
    status = models.CharField(max_length=30, choices=STATUS, default='INVITED')
    is_login_allowed = models.BooleanField(default=True)
    is_mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=128, blank=True, default='')
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='deactivated_by',
        related_name='deactivated_users',
    )
    deactivation_reason = models.TextField(null=True, blank=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    # Default home branch — set during user creation; doesn't change on branch deactivation
    home_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='home_users'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'users'
        ordering = ['email']

    def __str__(self):
        return f"{self.first_name} {self.last_name} <{self.email}>"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def set_password(self, raw_password: str):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return django_check_password(raw_password, self.password_hash)

    @property
    def is_accessible(self):
        return self.status == 'ACTIVE' and self.is_login_allowed

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def save(self, *args, **kwargs):
        if not self.display_name:
            self.display_name = self.full_name
        from apps.master.services_auth_directory import check_identifier_available
        from django.core.exceptions import ValidationError
        if self.email and not check_identifier_available(self.email, exclude_subject_id=self.id):
            raise ValidationError({'email': 'This username or email is already registered.'})
        if getattr(self, 'username', None) and not check_identifier_available(self.username, exclude_subject_id=self.id):
            raise ValidationError({'username': 'This username or email is already registered.'})
        super().save(*args, **kwargs)
        try:
            from apps.master.services_auth_directory import sync_tenant_user_identity
            sync_tenant_user_identity(self, db=kwargs.get('using') or self._state.db)
        except Exception:
            pass


class UserBranch(models.Model):
    """
    Records which branches a user is assigned to, their role scope at that branch,
    and whether that branch is their home branch.
    Passport (cross-branch) entitlements are modelled here as well.
    Deactivating a branch does NOT remove UserBranch rows — history is preserved.
    """
    SCOPE_TYPE = [
        ('HOME', 'Home Branch — primary assignment'),
        ('ADDITIONAL', 'Additional Branch — secondary work location'),
        ('PASSPORT', 'Passport — cross-branch access entitlement'),
        ('ALL', 'All Branches — org-wide access'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(TenantUser, on_delete=models.RESTRICT, related_name='branch_assignments')
    branch = models.ForeignKey(Branch, on_delete=models.RESTRICT, related_name='user_assignments')
    relationship_type = models.CharField(max_length=30, default='PRIMARY')
    scope_type = models.CharField(max_length=20, choices=SCOPE_TYPE, default='HOME')
    is_primary = models.BooleanField(default=False)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    unassigned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'user_branches'
        unique_together = ('user', 'branch', 'scope_type')
        ordering = ['user', 'scope_type']
        verbose_name_plural = 'User branches'

    def __str__(self):
        return f"{self.user.email} → {self.branch.name} ({self.scope_type})"

    def save(self, *args, **kwargs):
        if self.scope_type == 'HOME':
            self.relationship_type = 'PRIMARY'
            self.is_primary = True
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class UserDepartmentQuerySet(models.QuerySet):
    def _translate_kwargs(self, kwargs):
        t = {}
        for k, v in kwargs.items():
            if k == 'joined_at':
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


class UserDepartment(models.Model):
    """
    M2M: Tenant users ↔ Departments. Tracks who belongs to which department.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(TenantUser, on_delete=models.RESTRICT, related_name='department_memberships')
    department = models.ForeignKey(Department, on_delete=models.RESTRICT, related_name='members')
    is_primary = models.BooleanField(default=False)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_department_head = models.BooleanField(default=False)
    assigned_at = models.DateTimeField(default=timezone.now, db_column='assigned_at')
    unassigned_at = models.DateTimeField(null=True, blank=True, db_column='unassigned_at')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserDepartmentQuerySet.as_manager()

    class Meta:
        app_label = 'tenant_core'
        db_table = 'user_departments'
        unique_together = ('user', 'department')

    def __init__(self, *args, **kwargs):
        if 'joined_at' in kwargs and 'assigned_at' not in kwargs:
            kwargs['assigned_at'] = kwargs.pop('joined_at')
        if 'left_at' in kwargs and 'unassigned_at' not in kwargs:
            kwargs['unassigned_at'] = kwargs.pop('left_at')
        super().__init__(*args, **kwargs)

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
