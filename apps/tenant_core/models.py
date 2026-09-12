"""
Tenant core app models — re-exports from all model modules.
"""

from .models_org import Organization, CompanyEntity, Location, Branch

from .models_users import TenantUser, Department, UserBranch, UserDepartment

from .models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RoleModuleAccess, RoleSubmoduleAccess,
    RolePermissionSetItem, RoleAssignment, BranchModule,
)

from .models_govern import OrganizationSettings, BranchSettings, NotificationTemplate

from .models_privacy import (
    ProcessingPurpose, ConsentRecord, PrivacyRequest, TenantAuditEvent,
)

from .models_infra import File, Integration, LegacyEntityMap

__all__ = [
    # Org
    'Organization', 'CompanyEntity', 'Location', 'Branch',
    # Users
    'TenantUser', 'Department', 'UserBranch', 'UserDepartment',
    # RBAC
    'ModuleCatalog', 'SubmoduleCatalog', 'Permission', 'Role',
    'RolePermissionSet', 'RoleModuleAccess', 'RoleSubmoduleAccess',
    'RolePermissionSetItem', 'RoleAssignment', 'BranchModule',
    # Governance
    'OrganizationSettings', 'BranchSettings', 'NotificationTemplate',
    # Privacy
    'ProcessingPurpose', 'ConsentRecord', 'PrivacyRequest', 'TenantAuditEvent',
    # Infra
    'File', 'Integration', 'LegacyEntityMap',
]
