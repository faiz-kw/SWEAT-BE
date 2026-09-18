"""
Dedicated Tenant DB — RBAC Matrix Models (10 Tables)
Tables: module_catalog, submodule_catalog, permissions, roles, role_permission_sets,
        role_module_access, role_submodule_access, role_permission_set_items,
        role_assignments, branch_modules

These models power the 10-check authorization rule from the schema document.
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Branch, Organization, CompanyEntity, Location
from .models_users import TenantUser, Department


class ModuleCatalog(models.Model):
    """
    Tenant-local copy of product modules enabled for this tenant.
    Seeded from master.TenantPermissionCatalog during provisioning.
    Updated on plan changes / super admin overrides.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # References master product module by code (cross-DB string ref, not FK)
    code = models.CharField(max_length=100, unique=True, null=True, blank=True)
    module_code = models.CharField(max_length=50, unique=True, null=True, blank=True, help_text='Matches master.ProductModule.code')
    source_module_id = models.UUIDField(
        unique=True,
        help_text='Logical cross-DB ref to master.ProductModule.id'
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    icon = models.CharField(max_length=100, blank=True, default='')
    supports_branch_scope = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)
    sort_order = models.IntegerField(default=0)
    is_enabled = models.BooleanField(default=True, help_text='Enabled for this tenant')
    catalog_version = models.IntegerField(default=1)
    status = models.CharField(max_length=30, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'module_catalog'
        ordering = ['display_order', 'sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.code or self.module_code})"

    def save(self, *args, **kwargs):
        if not self.source_module_id:
            self.source_module_id = uuid.uuid4()
        if self.code and not self.module_code:
            self.module_code = self.code[:50]
        elif self.module_code and not self.code:
            self.code = self.module_code
        if self.display_order and not self.sort_order:
            self.sort_order = self.display_order
        elif self.sort_order and not self.display_order:
            self.display_order = self.sort_order
        super().save(*args, **kwargs)


class SubmoduleCatalog(models.Model):
    """
    Tenant-local copy of product submodules.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_submodule_id = models.UUIDField(
        unique=True,
        help_text='Logical cross-DB ref to master.ProductSubmodule.id'
    )
    module = models.ForeignKey(ModuleCatalog, on_delete=models.RESTRICT, related_name='submodules')
    code = models.CharField(max_length=100, null=True, blank=True)
    submodule_code = models.CharField(max_length=50, null=True, blank=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='', null=True)
    display_order = models.IntegerField(default=0)
    sort_order = models.IntegerField(default=0)
    catalog_version = models.IntegerField(default=1)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'submodule_catalog'
        ordering = ['module', 'display_order', 'sort_order']

    def __str__(self):
        return f"{self.module.code or self.module.module_code} → {self.code or self.submodule_code}"

    def save(self, *args, **kwargs):
        if not self.source_submodule_id:
            self.source_submodule_id = uuid.uuid4()
        if self.code and not self.submodule_code:
            self.submodule_code = self.code[:50]
        elif self.submodule_code and not self.code:
            self.code = self.submodule_code
        if self.display_order and not self.sort_order:
            self.sort_order = self.display_order
        elif self.sort_order and not self.display_order:
            self.display_order = self.sort_order
        super().save(*args, **kwargs)


class Permission(models.Model):
    """
    Tenant-local copy of granular action permissions.
    Seeded from master.TenantPermissionCatalog during provisioning.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_permission_id = models.UUIDField(
        unique=True,
        help_text='Logical cross-DB ref to master.TenantPermissionCatalog.id'
    )
    module = models.ForeignKey(ModuleCatalog, on_delete=models.RESTRICT, related_name='permissions')
    submodule = models.ForeignKey(SubmoduleCatalog, on_delete=models.SET_NULL, null=True, blank=True)
    code = models.CharField(max_length=150, unique=True, null=True, blank=True)
    permission_code = models.CharField(max_length=150, unique=True, null=True, blank=True, help_text='e.g. crm.leads.view')
    action = models.CharField(max_length=50)
    label = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='', null=True)
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'permissions'
        ordering = ['module', 'action']

    def __str__(self):
        return f"{self.code or self.permission_code} — {self.label or self.action}"

    def save(self, *args, **kwargs):
        if self.code and not self.permission_code:
            self.permission_code = self.code
        elif self.permission_code and not self.code:
            self.code = self.permission_code

        # Deterministically resolve source_permission_id from Master catalog if possible
        code_to_check = self.code or self.permission_code
        if code_to_check:
            try:
                from apps.master.models_saas import TenantPermissionCatalog
                cat = TenantPermissionCatalog.objects.using('default').filter(code=code_to_check).first()
                if cat:
                    self.source_permission_id = cat.id
            except Exception:
                pass

        if not self.source_permission_id:
            self.source_permission_id = uuid.uuid4()

        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class Role(models.Model):
    """
    Custom roles defined by the Tenant Org Admin (e.g. Head Trainer, Sales Executive).
    """
    SCOPE = [
        ('ORG', 'Organization-wide'),
        ('BRANCH', 'Branch-scoped'),
        ('LOCATION', 'Location-scoped'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='roles')
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name='roles'
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100, help_text='Stable code unique per org')
    description = models.TextField(blank=True, default='', null=True)
    scope = models.CharField(max_length=20, choices=SCOPE, default='BRANCH')
    is_system_role = models.BooleanField(default=False)
    is_system = models.BooleanField(default=False, help_text='System roles (e.g. Org Admin) cannot be deleted')
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_roles'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'roles'
        unique_together = ('organization', 'code')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code}) [{self.scope}]"

    def save(self, *args, **kwargs):
        if self.is_system_role and not self.is_system:
            self.is_system = self.is_system_role
        elif self.is_system and not self.is_system_role:
            self.is_system_role = self.is_system
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class RolePermissionSet(models.Model):
    """
    Named, reusable permission sets (e.g. 'CRM Full Access', 'Read-Only Finance').
    Roles reference these sets rather than individual permissions.
    """
    SCOPE_TYPE = [
        ('ORGANIZATION', 'Organization'),
        ('COMPANY_ENTITY', 'Company Entity'),
        ('LOCATION', 'Location'),
        ('BRANCH', 'Branch'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name='permission_sets')
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='role_permission_sets')
    scope_type = models.CharField(max_length=30, choices=SCOPE_TYPE, default='ORGANIZATION')
    company_entity = models.ForeignKey(
        CompanyEntity, on_delete=models.SET_NULL, null=True, blank=True, related_name='role_permission_sets'
    )
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True, related_name='role_permission_sets'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='role_permission_sets'
    )
    inherits_from = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_sets'
    )
    is_override = models.BooleanField(default=False)
    name = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='', null=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_permission_sets'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'role_permission_sets'
        ordering = ['role', 'name']

    def save(self, *args, **kwargs):
        if not self.organization_id and self.role_id:
            if hasattr(self.role, 'organization_id') and self.role.organization_id:
                self.organization_id = self.role.organization_id
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.role.code} → {self.name or self.scope_type}"


class RoleModuleAccess(models.Model):
    """
    Controls which modules a role can see (module visibility gate — check #7).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='module_access')
    permission_set = models.ForeignKey(
        RolePermissionSet, on_delete=models.RESTRICT, db_column='permission_set_id',
        related_name='module_access'
    )
    module = models.ForeignKey(ModuleCatalog, on_delete=models.PROTECT)
    is_visible = models.BooleanField(default=True, db_column='is_visible')
    can_access = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'role_module_access'
        unique_together = ('role', 'module')

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or self._state.db or 'default'
        if not self.permission_set_id and self.role_id:
            rps = RolePermissionSet.objects.using(using).filter(role_id=self.role_id).order_by('created_at').first()
            if not rps:
                role_obj = self.role
                rps = RolePermissionSet.objects.using(using).create(
                    role=role_obj,
                    organization_id=getattr(role_obj, 'organization_id', None),
                    name=f"{role_obj.name} Default PSet",
                    scope_type='ORGANIZATION',
                )
            self.permission_set = rps
        if self.can_access is False or self.is_visible is False:
            self.can_access = False
            self.is_visible = False
        else:
            self.can_access = True
            self.is_visible = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.role.code} → {self.module.code or self.module.module_code}: {'✓' if self.is_visible else '✗'}"


class RoleSubmoduleAccess(models.Model):
    """
    Controls which submodules a role can see (submodule visibility gate — check #8).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='submodule_access')
    permission_set = models.ForeignKey(
        RolePermissionSet, on_delete=models.RESTRICT, db_column='permission_set_id',
        related_name='submodule_access'
    )
    submodule = models.ForeignKey(SubmoduleCatalog, on_delete=models.PROTECT)
    is_visible = models.BooleanField(default=True, db_column='is_visible')
    can_access = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'role_submodule_access'
        unique_together = ('role', 'submodule')

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or self._state.db or 'default'
        if not self.permission_set_id and self.role_id:
            rps = RolePermissionSet.objects.using(using).filter(role_id=self.role_id).order_by('created_at').first()
            if not rps:
                role_obj = self.role
                rps = RolePermissionSet.objects.using(using).create(
                    role=role_obj,
                    organization_id=getattr(role_obj, 'organization_id', None),
                    name=f"{role_obj.name} Default PSet",
                    scope_type='ORGANIZATION',
                )
            self.permission_set = rps
        if self.can_access is False or self.is_visible is False:
            self.can_access = False
            self.is_visible = False
        else:
            self.can_access = True
            self.is_visible = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.role.code} → {self.submodule.code or self.submodule.submodule_code}: {'✓' if self.is_visible else '✗'}"


class RolePermissionSetItem(models.Model):
    """
    Individual permission grants within a role permission set (action gate — check #9).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permission_set = models.ForeignKey(RolePermissionSet, on_delete=models.RESTRICT, related_name='items')
    permission = models.ForeignKey(Permission, on_delete=models.RESTRICT)
    is_allowed = models.BooleanField(default=True)
    granted = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'role_permission_set_items'
        unique_together = ('permission_set', 'permission')

    def save(self, *args, **kwargs):
        # The API and authorization engine use granted as the authoritative value.
        self.is_allowed = self.granted
        if kwargs.get('update_fields') is not None:
            fields = set(kwargs['update_fields'])
            if fields & {'granted', 'is_allowed'}:
                kwargs['update_fields'] = fields | {'granted', 'is_allowed'}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.permission_set} → {self.permission.code or self.permission.permission_code}: {'✓' if self.is_allowed else '✗'}"


class RoleAssignment(models.Model):
    """
    Assigns a role to a user with optional branch/location scope.
    Multiple active role assignments per user are supported.
    Deactivating a branch does NOT remove role assignments — history preserved.
    """
    SCOPE_TYPE = [
        ('ORGANIZATION', 'Organization'),
        ('COMPANY_ENTITY', 'Company Entity'),
        ('LOCATION', 'Location'),
        ('BRANCH', 'Branch'),
    ]
    STATUS = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='role_assignments')
    user = models.ForeignKey(TenantUser, on_delete=models.RESTRICT, related_name='role_assignments')
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name='user_assignments')
    scope_type = models.CharField(max_length=30, choices=SCOPE_TYPE, default='ORGANIZATION')
    company_entity = models.ForeignKey(
        CompanyEntity, on_delete=models.RESTRICT, null=True, blank=True, related_name='role_assignments'
    )
    location = models.ForeignKey(
        Location, on_delete=models.RESTRICT, null=True, blank=True, related_name='role_assignments'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.RESTRICT, null=True, blank=True, related_name='role_assignments',
        help_text='NULL = org-wide scope'
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True
    )
    status = models.CharField(max_length=30, choices=STATUS, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    unassigned_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True,
        db_column='assigned_by_user_id', related_name='assigned_roles'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'role_assignments'
        ordering = ['user', '-assigned_at']

    def __init__(self, *args, **kwargs):
        status_passed = 'status' in kwargs
        is_active_passed = 'is_active' in kwargs
        super().__init__(*args, **kwargs)
        if is_active_passed and not status_passed:
            self.status = 'ACTIVE' if self.is_active else 'INACTIVE'
        elif status_passed:
            self.is_active = (self.status == 'ACTIVE')
        self._initial_status = self.status
        self._initial_is_active = self.is_active

    @property
    def assigned_by_user_id(self):
        return self.assigned_by_id

    @assigned_by_user_id.setter
    def assigned_by_user_id(self, val):
        self.assigned_by_id = val

    def clean(self):
        super().clean()
        if hasattr(self, '_initial_status') and self.status != self._initial_status:
            self.is_active = (self.status == 'ACTIVE')
        elif hasattr(self, '_initial_is_active') and self.is_active != self._initial_is_active:
            self.status = 'ACTIVE' if self.is_active else 'INACTIVE'
        else:
            if self.status == 'INACTIVE':
                self.is_active = False
            elif self.status == 'ACTIVE':
                self.is_active = True
        self._initial_status = self.status
        self._initial_is_active = self.is_active

    def save(self, *args, **kwargs):
        if not self.organization_id and self.user_id:
            if hasattr(self.user, 'organization_id') and self.user.organization_id:
                self.organization_id = self.user.organization_id
        if not self.organization_id and self.role_id:
            if hasattr(self.role, 'organization_id') and self.role.organization_id:
                self.organization_id = self.role.organization_id
        if not self.scope_type:
            self.scope_type = 'BRANCH' if self.branch_id else 'ORGANIZATION'
        elif self.branch_id and self.scope_type == 'ORGANIZATION':
            self.scope_type = 'BRANCH'
        elif not self.branch_id and self.scope_type == 'BRANCH':
            self.scope_type = 'ORGANIZATION'

        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        scope = self.branch.name if self.branch else 'Org-wide'
        return f"{self.user.email} → {self.role.code} @ {scope}"


class BranchModule(models.Model):
    """
    Explicit module ↔ branch mapping for SELECTED_BRANCHES availability mode.
    Only needed when master.TenantModule.availability_mode = 'SELECTED_BRANCHES'.
    For ALL_BRANCHES mode, this table has no rows for that module.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.RESTRICT, related_name='enabled_modules')
    module = models.ForeignKey(
        ModuleCatalog, on_delete=models.RESTRICT, db_column='module_id',
        null=True, blank=True, related_name='branch_modules'
    )
    # Cross-DB ref to master product module by code
    module_code = models.CharField(max_length=50, help_text='Matches master.ProductModule.code')
    status = models.CharField(max_length=30, default='ENABLED')
    is_enabled = models.BooleanField(default=True)
    enabled_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    assigned_by_type = models.CharField(max_length=30, default='PLATFORM_USER')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'branch_modules'
        unique_together = ('branch', 'module_code')
        ordering = ['branch', 'module_code']

    def save(self, *args, **kwargs):
        if not self.module_id and self.module_code:
            from .models_rbac import ModuleCatalog
            from config.routers import get_tenant_db_alias
            db = (
                kwargs.get('using')
                or self._state.db
                or (getattr(getattr(self, 'branch', None), '_state', None) and self.branch._state.db)
                or get_tenant_db_alias()
                or 'default'
            )
            try:
                mod = ModuleCatalog.objects.using(db).filter(
                    models.Q(code=self.module_code) | models.Q(module_code=self.module_code)
                ).first()
                if not mod:
                    mod, _ = ModuleCatalog.objects.using(db).get_or_create(
                        code=self.module_code,
                        defaults={
                            'module_code': self.module_code,
                            'name': self.module_code.title(),
                            'source_module_id': uuid.uuid4(),
                            'is_enabled': True,
                        }
                    )
                if mod:
                    self.module = mod
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to link ModuleCatalog in BranchModule.save: %s", e)
        elif self.module_id and not self.module_code:
            self.module_code = getattr(self.module, 'code', None) or getattr(self.module, 'module_code', '')

        if self.status == 'ENABLED':
            self.is_enabled = True
        elif self.status == 'DISABLED':
            self.is_enabled = False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.branch.name} → {self.module_code}: {'✓' if self.is_enabled else '✗'}"
