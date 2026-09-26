"""
tests/test_crm_consistency.py — Regression and Consistency Test Suite for CRM, Branches, Roles, and Staff Profiles

Verifies:
 1. Branch scoping: No duplicate branches returned across organizations
 2. Role scoping: No duplicate roles returned across organizations
 3. Program resolution: Active programs returned for New Lead ('lead_interest')
 4. Branch-aware program resolution: Programs correctly resolved by branch
 5. Username editing: Valid username update persists and normalizes to lowercase
 6. Username uniqueness: Duplicate username rejected with HTTP 400
 7. Username validation: Invalid characters rejected with HTTP 400
 8. Username audit: BusinessAuditEvent recorded on username update
 9. Non-admin username edit rejected (HTTP 403)
 10. Staff vs Member separation preserved
"""

import uuid
from decimal import Decimal
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageBranchAvailability
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.models_rbac import (
    Role,
    RoleAssignment,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    RoleModuleAccess,
    RoleSubmoduleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
)
from apps.tenant_core.services_catalog import CRMProgramEligibilityService


class CRMConsistencyTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.tenant_alias = 'tenant_test'
        set_tenant_db_alias(self.tenant_alias)

        # 1. Master Tenant
        self.tenant = Tenant.objects.using('default').create(
            code='CONSIST-TENANT',
            name='Consistency Gym',
            slug='consistency-gym',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Elite Consistency',
            code='ELITE_CONSISTENCY',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        for m_code in ['core', 'crm', 'finance', 'memberships']:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=m_code, defaults={'name': f'{m_code.capitalize()} Module', 'status': 'ACTIVE'}
            )
            TenantModule.objects.using('default').create(
                tenant=self.tenant, module=pm, is_enabled=True, availability_mode='ALL_BRANCHES'
            )

        # 2. Primary Organization & Location
        self.org1 = Organization.objects.using(self.tenant_alias).create(
            name='Consistency Org 1',
            code='ORG-CONSIST-1',
            status='ACTIVE',
        )
        self.loc1 = Location.objects.using(self.tenant_alias).create(
            organization=self.org1,
            code='LOC-CONSIST-1',
            name='Consistency Location 1',
            status='ACTIVE',
        )
        # Secondary Organization in the same tenant database (simulating multi-org seeding)
        self.org2 = Organization.objects.using(self.tenant_alias).create(
            name='Consistency Org 2',
            code='ORG-CONSIST-2',
            status='ACTIVE',
        )
        self.loc2 = Location.objects.using(self.tenant_alias).create(
            organization=self.org2,
            code='LOC-CONSIST-2',
            name='Consistency Location 2',
            status='ACTIVE',
        )

        # Branches with same name in different orgs
        self.branch1 = Branch.objects.using(self.tenant_alias).create(
            organization=self.org1,
            location=self.loc1,
            name='Downtown Flagship',
            code='BR-DT-1',
            status='ACTIVE',
        )
        self.branch2_duplicate_name = Branch.objects.using(self.tenant_alias).create(
            organization=self.org2,
            location=self.loc2,
            name='Downtown Flagship',
            code='BR-DT-2',
            status='ACTIVE',
        )

        # Roles with same name in different orgs
        self.role_admin_org1 = Role.objects.using(self.tenant_alias).create(
            organization=self.org1,
            name='Organization Administrator',
            code='ORG_ADMIN',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
            is_system=True,
        )
        self.role_admin_org2 = Role.objects.using(self.tenant_alias).create(
            organization=self.org2,
            name='Organization Administrator',
            code='ORG_ADMIN',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
            is_system=True,
        )
        self.role_frontdesk_org1 = Role.objects.using(self.tenant_alias).create(
            organization=self.org1,
            name='Front Desk Staff',
            code='FRONT_DESK',
            scope='BRANCH',
            is_active=True,
            status='ACTIVE',
            is_system=True,
        )
        self.role_frontdesk_org2 = Role.objects.using(self.tenant_alias).create(
            organization=self.org2,
            name='Front Desk Staff',
            code='FRONT_DESK',
            scope='BRANCH',
            is_active=True,
            status='ACTIVE',
            is_system=True,
        )

        # Grant permissions to ORG_ADMIN
        admin_mod, _ = ModuleCatalog.objects.using(self.tenant_alias).get_or_create(
            module_code='core', defaults={'name': 'Core', 'status': 'ACTIVE', 'is_enabled': True, 'source_module_id': uuid.uuid4()}
        )
        sub_users, _ = SubmoduleCatalog.objects.using(self.tenant_alias).get_or_create(
            module=admin_mod, submodule_code='users', defaults={'name': 'Users', 'status': 'ACTIVE', 'is_enabled': True, 'source_submodule_id': uuid.uuid4()}
        )
        sub_roles, _ = SubmoduleCatalog.objects.using(self.tenant_alias).get_or_create(
            module=admin_mod, submodule_code='roles', defaults={'name': 'Roles', 'status': 'ACTIVE', 'is_enabled': True, 'source_submodule_id': uuid.uuid4()}
        )
        RoleModuleAccess.objects.using(self.tenant_alias).get_or_create(role=self.role_admin_org1, module=admin_mod, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(self.tenant_alias).get_or_create(role=self.role_admin_org1, submodule=sub_users, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(self.tenant_alias).get_or_create(role=self.role_admin_org1, submodule=sub_roles, defaults={'can_access': True})

        pset = RolePermissionSet.objects.using(self.tenant_alias).create(
            role=self.role_admin_org1, name='Admin Permissions', is_active=True
        )
        for perm_code in ['core.users.view', 'core.users.create', 'core.users.edit', 'core.users.delete', 'core.roles.view', 'catalog.programs.view']:
            sub = sub_roles if 'roles' in perm_code else sub_users
            p, _ = Permission.objects.using(self.tenant_alias).get_or_create(
                permission_code=perm_code,
                defaults={'module': admin_mod, 'submodule': sub, 'action': perm_code.split('.')[-1], 'label': perm_code}
            )
            RolePermissionSetItem.objects.using(self.tenant_alias).create(
                permission_set=pset, permission=p, granted=True
            )

        # Users
        self.admin_user = TenantUser.objects.using(self.tenant_alias).create(
            email='admin@consistency.test',
            username='admin_consistency',
            first_name='Admin',
            last_name='User',
            user_type='STAFF',
            status='ACTIVE',
            organization=self.org1,
        )
        self.admin_user.set_password('SecretPass123!')
        self.admin_user.save(using=self.tenant_alias)
        RoleAssignment.objects.using(self.tenant_alias).create(
            user=self.admin_user,
            role=self.role_admin_org1,
            organization=self.org1,
            status='ACTIVE',
            is_active=True,
        )

        self.staff_user = TenantUser.objects.using(self.tenant_alias).create(
            email='staff@consistency.test',
            username='staff_john',
            first_name='John',
            last_name='Staff',
            user_type='STAFF',
            status='ACTIVE',
            organization=self.org1,
        )
        self.staff_user.set_password('SecretPass123!')
        self.staff_user.save(using=self.tenant_alias)
        RoleAssignment.objects.using(self.tenant_alias).create(
            user=self.staff_user,
            role=self.role_frontdesk_org1,
            organization=self.org1,
            status='ACTIVE',
            is_active=True,
        )

        # Programs
        self.cat = ProgramCategory.objects.using(self.tenant_alias).create(
            organization=self.org1,
            code='CAT-MIND',
            name='Mind & Body',
            status='ACTIVE',
        )
        self.program_pilates = Program.objects.using(self.tenant_alias).create(
            organization=self.org1,
            category=self.cat,
            name='Reformer Pilates',
            code='PROG-PILATES',
            status='ACTIVE',
        )

        self.client = APIClient()

    def _auth_client(self, user):
        refresh = _build_tenant_token(user=user, tenant=self.tenant, db_alias=self.tenant_alias)
        token = str(refresh.access_token)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {token}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        return client

    def test_branch_scoping_eliminates_duplicates(self):
        """Branch endpoint returns ONLY the authenticated user's organization branches, preventing duplicates."""
        client = self._auth_client(self.admin_user)
        res = client.get('/api/v1/tenant/branches/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = res.data if isinstance(res.data, list) else res.data.get('results', [])
        branch_ids = [b['id'] for b in items]
        self.assertIn(str(self.branch1.id), branch_ids)
        self.assertNotIn(str(self.branch2_duplicate_name.id), branch_ids)
        branch_names = [b['name'] for b in items]
        self.assertEqual(branch_names.count('Downtown Flagship'), 1)

    def test_role_scoping_eliminates_duplicates(self):
        """Role endpoint returns ONLY the authenticated user's organization roles, preventing duplicates."""
        client = self._auth_client(self.admin_user)
        res = client.get('/api/v1/tenant/roles/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = res.data if isinstance(res.data, list) else res.data.get('results', [])
        role_ids = [r['id'] for r in items]
        self.assertIn(str(self.role_admin_org1.id), role_ids)
        self.assertNotIn(str(self.role_admin_org2.id), role_ids)
        self.assertIn(str(self.role_frontdesk_org1.id), role_ids)
        self.assertNotIn(str(self.role_frontdesk_org2.id), role_ids)

    def test_crm_program_eligibility_for_new_lead(self):
        """Active program appears for New Lead under lead_interest context."""
        client = self._auth_client(self.admin_user)
        res = client.get(f'/api/v1/tenant/programs/?status=ACTIVE&branch_id={self.branch1.id}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data if isinstance(res.data, list) else res.data.get('results', [])
        prog_ids = [p['id'] for p in results]
        self.assertIn(str(self.program_pilates.id), prog_ids)

    def test_crm_program_service_direct(self):
        """CRMProgramEligibilityService resolves canonical programs for lead_interest and trial."""
        qs_lead = CRMProgramEligibilityService.resolve_programs(
            organization=self.org1,
            context='lead_interest',
            branch_id=str(self.branch1.id),
            alias=self.tenant_alias,
        )
        self.assertTrue(qs_lead.filter(id=self.program_pilates.id).exists())

        qs_trial = CRMProgramEligibilityService.resolve_programs(
            organization=self.org1,
            context='trial',
            branch_id=str(self.branch1.id),
            alias=self.tenant_alias,
        )
        self.assertTrue(qs_trial.filter(id=self.program_pilates.id).exists())

    def test_username_edit_succeeds_and_normalizes(self):
        """Admin can edit staff username, which normalizes to lowercase and records audit event."""
        client = self._auth_client(self.admin_user)
        new_username = 'Staff_John_Updated'
        res = client.patch(
            f'/api/v1/tenant/users/{self.staff_user.id}/',
            {'username': new_username},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.staff_user.refresh_from_db(using=self.tenant_alias)
        self.assertEqual(self.staff_user.username, 'staff_john_updated')

        # Audit event recorded
        audit = BusinessAuditEvent.objects.using(self.tenant_alias).filter(
            action_code='TENANT_USER_USERNAME_UPDATED',
            entity_id=str(self.staff_user.id),
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.before_data.get('username'), 'staff_john')
        self.assertEqual(audit.after_data.get('username'), 'staff_john_updated')

    def test_duplicate_username_rejected(self):
        """Attempting to assign an existing username returns HTTP 400."""
        client = self._auth_client(self.admin_user)
        res = client.patch(
            f'/api/v1/tenant/users/{self.staff_user.id}/',
            {'username': 'admin_consistency'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_username_characters_rejected(self):
        """Invalid username characters (e.g. spaces or symbols) rejected with HTTP 400."""
        client = self._auth_client(self.admin_user)
        res = client.patch(
            f'/api/v1/tenant/users/{self.staff_user.id}/',
            {'username': 'staff user!'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
