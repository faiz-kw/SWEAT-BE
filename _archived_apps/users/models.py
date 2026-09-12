"""
Custom User Model and Granular RBAC Permissions for PerformanceOS.
Supports email authentication, tenant binding, role-based access, and location assignment.
"""

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from .managers import UserManager
from apps.tenants.models import Tenant, Location


class Role(models.TextChoices):
    SUPER_ADMIN = 'Super Admin', 'Super Admin'
    ADMIN = 'Admin', 'Admin'
    STUDIO_MANAGER = 'Studio Manager', 'Studio Manager'
    OPERATIONS_MANAGER = 'Operations Manager', 'Operations Manager'
    SALES = 'Sales', 'Sales'
    FRONT_DESK = 'Front Desk', 'Front Desk'
    TRAINER = 'Trainer', 'Trainer'
    PILATES_INSTRUCTOR = 'Pilates Instructor', 'Pilates Instructor'
    NUTRITION_COACH = 'Nutrition Coach', 'Nutrition Coach'
    FINANCE = 'Finance', 'Finance'
    SUPPORT = 'Support', 'Support'
    MEMBER = 'Member', 'Member'


class UserStatus(models.TextChoices):
    ACTIVE = 'Active', 'Active'
    INACTIVE = 'Inactive', 'Inactive'
    INVITED = 'Invited', 'Invited'
    SUSPENDED = 'Suspended', 'Suspended'


class RoleScope(models.TextChoices):
    PLATFORM = 'platform', 'Platform Scope'
    TENANT = 'tenant', 'Tenant Scope'


class RoleDefinition(models.Model):
    """
    Configurable roles in the system.
    Can be platform-wide (scope='platform', is_system=True) or custom to a specific Tenant (scope='tenant').
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. ROLE-ADMIN")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='custom_roles')
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=64)
    scope = models.CharField(max_length=32, choices=RoleScope.choices, default=RoleScope.TENANT)
    description = models.TextField(blank=True, default='')
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'roles'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code}) [{self.scope}]"


class PermissionDefinition(models.Model):
    """
    Granular permission items representing actions on specific modules (e.g. module.resource.action).
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. users.view or PERM-CRM-LEADS-VIEW")
    module = models.CharField(max_length=64, help_text="e.g. CRM, Members, Operations, Finance, Platform, Administration, Users, Roles")
    action = models.CharField(max_length=32, help_text="e.g. view, create, edit, delete, manage, export")
    label = models.CharField(max_length=128)
    scope = models.CharField(max_length=32, choices=RoleScope.choices, default=RoleScope.TENANT)
    description = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'permissions'
        ordering = ['module', 'action']

    def __str__(self):
        return f"{self.module} - {self.action} ({self.label})"


class RolePermission(models.Model):
    """
    Maps RoleDefinition to PermissionDefinition.
    """
    role = models.ForeignKey(RoleDefinition, on_delete=models.CASCADE, related_name='permissions')
    permission = models.ForeignKey(PermissionDefinition, on_delete=models.CASCADE, related_name='role_mappings')
    granted = models.BooleanField(default=True)

    class Meta:
        db_table = 'role_permissions'
        unique_together = ('role', 'permission')

    def __str__(self):
        return f"{self.role.name} -> {self.permission.id}: {self.granted}"


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model where email is the primary login field.
    Each user is bound to a Tenant organization (or None for Platform Super Admins) and has assigned location scopes.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Unique User ID (e.g. USR-001 or UUID string)"
    )
    email = models.EmailField(unique=True, max_length=255)
    first_name = models.CharField(max_length=150, blank=True, default='')
    last_name = models.CharField(max_length=150, blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    avatar_url = models.URLField(blank=True, default='')
    emergency_contact = models.CharField(max_length=255, blank=True, default='')

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='users',
        help_text="Tenant organization this user belongs to (null for platform super admin)"
    )

    role = models.CharField(
        max_length=64,
        choices=Role.choices,
        default=Role.ADMIN,
        help_text="User role display name in the system"
    )

    role_definition = models.ForeignKey(
        RoleDefinition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_users'
    )

    allowed_locations = models.ManyToManyField(
        Location,
        blank=True,
        related_name='permitted_users',
        help_text="Locations this user has permission to access"
    )

    active_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_users',
        help_text="The currently selected/active location for this user"
    )

    status = models.CharField(max_length=32, choices=UserStatus.choices, default=UserStatus.ACTIVE)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        db_table = 'users'
        ordering = ['email']

    def __str__(self):
        return f"{self.email} ({self.role})"

    @property
    def full_name(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name if name else self.email

    @property
    def is_platform_admin(self):
        return self.is_superuser or (self.role and 'Super' in self.role) or (self.tenant_id is None)

    def has_permission_code(self, perm_code):
        """
        Evaluate if this user has the given granular permission.
        Super Admin always has wildcard permission.
        """
        if self.is_platform_admin:
            return True
        if not self.role_definition:
            # Fallback legacy check
            return self.role in [Role.SUPER_ADMIN, Role.ADMIN]
        return self.role_definition.permissions.filter(
            permission_id=perm_code,
            granted=True
        ).exists()

