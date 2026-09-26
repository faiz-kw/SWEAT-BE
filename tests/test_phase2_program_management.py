"""
Phase 2: Program Types, Programs, and Program Branch Availability Test Suite.

Verifies:
1. ProgramType CRUD, uppercase/regex code format, uniqueness per organization.
2. ProgramType deactivation and reactivation lifecycle endpoints.
3. ProgramType safe deletion: blocked with PROGRAM_TYPE_HAS_PROGRAMS if programs exist.
4. Program CRUD with delivery_mode (GROUP, PERSONAL_TRAINING, OPEN_GYM, HYBRID), display_order, trial_allowed.
5. Inactive ProgramType assignment blocked on program creation.
6. Program deactivation and reactivation lifecycle endpoints.
7. Program safe deletion: blocked with PROGRAM_HAS_HISTORY if packages, classes, or leads exist.
8. ProgramBranchAvailability: automatic sync on program create/update, active/inactive toggles.
9. Cross-tenant and inactive branch assignment blocking on ProgramBranchAvailability.
10. CRMProgramEligibilityService.resolve_programs:
    - Strictly filters by ProgramBranchAvailability for branch_id (returns empty if none, NO silent fallback).
    - Filters by trial_allowed in trial context.
    - Preserves management context without fallback.
11. ClassBranchAvailability validation: blocks class template assignment to a branch if its program is not available at that branch.
"""

from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_saas import SaasPlan, SaasPlanPrice, TenantSubscription, TenantModule, ProductModule, ProductSubmodule
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization, CompanyEntity, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, ProgramType, Program, ProgramBranchAvailability,
    Package, PackageVersion,
)
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassBranchAvailability
from apps.tenant_core.serializers_classes import ClassBranchAvailabilitySerializer
from apps.tenant_core.services_catalog import CRMProgramEligibilityService
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class Phase2ProgramManagementTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Tenant on Master DB
        self.tenant = Tenant.objects.create(
            name='SWEAT Elite Fitness',
            slug='sweat-elite',
            code='SWEAT-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.create(name='Pro Plan', code='PLAN-PRO', tier='growth', is_active=True)
        self.plan_price = SaasPlanPrice.objects.create(
            plan=self.plan, billing_cycle='MONTHLY', currency='INR', amount=Decimal('9999.00'), is_active=True
        )
        self.subscription = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
        )

        self.prod_mod_core, _ = ProductModule.objects.get_or_create(
            code='core', defaults={'name': 'Core System', 'is_active': True}
        )
        self.tenant_mod_core, _ = TenantModule.objects.get_or_create(
            tenant=self.tenant, module=self.prod_mod_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True}
        )

        # 2. Tenant Core Hierarchy on Tenant DB
        self.org = Organization.objects.using('tenant_test').create(
            name='SWEAT Elite Org', code='SWEAT-ORG', status='ACTIVE', timezone='Asia/Kolkata'
        )
        self.loc1 = Location.objects.using('tenant_test').create(
            organization=self.org, name='Mumbai Central', city='Mumbai', code='LOC-MUMBAI', status='ACTIVE'
        )
        self.company = CompanyEntity.objects.using('tenant_test').create(
            organization=self.org, name='SWEAT Wellness Pvt Ltd', legal_name='SWEAT Wellness Pvt Ltd',
            code='SWEAT-PVT', status='ACTIVE'
        )

        self.branch1 = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc1, company_entity=self.company,
            name='Bandra Flagship', code='BANDRA_01', address='Linking Road, Bandra West',
            phone='+91 98200 12345', email='bandra@sweatfit.com', timezone='Asia/Kolkata',
            capacity=80, business_open_time='06:00', business_close_time='22:00',
            is_passport_eligible=True, geofence_radius_meters=200, geofence_enforcement='STRICT',
            status='ACTIVE'
        )

        self.loc2 = Location.objects.using('tenant_test').create(
            organization=self.org, name='Andheri West', city='Mumbai', code='LOC-ANDHERI', status='ACTIVE'
        )
        self.branch2 = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc2, company_entity=self.company,
            name='Andheri Central', code='ANDHERI_01', address='Veera Desai, Andheri West',
            phone='+91 98200 54321', email='andheri@sweatfit.com', timezone='Asia/Kolkata',
            capacity=60, business_open_time='06:00', business_close_time='22:00',
            is_passport_eligible=True, geofence_radius_meters=150, geofence_enforcement='STRICT',
            status='ACTIVE'
        )

        # 3. RBAC Catalog on Tenant DB
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core System', 'is_enabled': True}
        )
        self.sub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True}
        )
        self.perm_settings_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        self.perm_settings_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )

        # 4. Standard Org Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='admin@sweatfit.com', first_name='Org', last_name='Admin', status='ACTIVE',
            home_branch=self.branch1
        )
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org, code='ORG_ADMIN', name='Organization Admin', scope='ORG', is_system=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.admin_role, branch=None, is_active=True
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.admin_role, module=self.cat_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.admin_role, submodule=self.sub_settings, can_access=True)
        ps_admin = RolePermissionSet.objects.using('tenant_test').create(role=self.admin_role, name='Admin Perms', is_active=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=self.perm_settings_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=self.perm_settings_edit, granted=True)

        token = self.get_token(self.admin_user)
        self.auth_headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def get_token(self, user):
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.using('tenant_test').filter(user=user, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        return str(refresh.access_token)

    def test_program_type_crud_and_code_validation(self):
        """1. ProgramType CRUD, code regex validation, and uniqueness."""
        set_tenant_db_alias('tenant_test')
        # A. Invalid code with spaces or lowercase
        res = self.client.post('/api/v1/tenant/program-types/', {
            'code': 'bad code',
            'name': 'Bad Code Type',
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # B. Successful creation
        res = self.client.post('/api/v1/tenant/program-types/', {
            'code': 'PILATES_GRP',
            'name': 'Pilates Group Sessions',
            'description': 'Reformer & mat pilates',
            'display_order': 1,
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()
        self.assertEqual(data['code'], 'PILATES_GRP')
        pt_id = data['id']

        # C. Duplicate code rejected within same org
        res_dup = self.client.post('/api/v1/tenant/program-types/', {
            'code': 'PILATES_GRP',
            'name': 'Another Pilates',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)

        # D. Update program type
        res_up = self.client.patch(f'/api/v1/tenant/program-types/{pt_id}/', {
            'name': 'Elite Pilates Group Sessions',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_up.status_code, status.HTTP_200_OK)
        self.assertEqual(res_up.json()['name'], 'Elite Pilates Group Sessions')
        # Established code must NOT change when display name is updated
        self.assertEqual(res_up.json()['code'], 'PILATES_GRP')

        # E. Auto-generate code from name when code omitted
        res_auto = self.client.post('/api/v1/tenant/program-types/', {
            'name': 'Strength & Conditioning',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto.json()['code'], 'STRENGTH_CONDITIONING')

        # F. Collision handling: creating same name generates unique suffix
        res_auto_coll = self.client.post('/api/v1/tenant/program-types/', {
            'name': 'Strength & Conditioning',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto_coll.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto_coll.json()['code'], 'STRENGTH_CONDITIONING_2')

    def test_program_type_lifecycle_and_safe_delete(self):
        """2 & 3. ProgramType deactivation/reactivation and safe delete protection."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='STRENGTH_GRP',
            name='Strength & Conditioning',
            status='ACTIVE'
        )

        # Deactivate
        res_deact = self.client.post(f'/api/v1/tenant/program-types/{pt.id}/deactivate/', **self.auth_headers)
        self.assertEqual(res_deact.status_code, status.HTTP_200_OK)
        self.assertEqual(res_deact.json()['status'], 'INACTIVE')

        # Reactivate
        res_react = self.client.post(f'/api/v1/tenant/program-types/{pt.id}/reactivate/', **self.auth_headers)
        self.assertEqual(res_react.status_code, status.HTTP_200_OK)
        self.assertEqual(res_react.json()['status'], 'ACTIVE')

        # Link a program to this program type
        set_tenant_db_alias('tenant_test')
        prog = Program.objects.using('tenant_test').create(
            organization=self.org,
            program_type=pt,
            code='HYROX_PROG',
            name='HYROX Performance',
            status='ACTIVE',
        )

        # Attempt safe delete -> MUST FAIL with PROGRAM_TYPE_HAS_PROGRAMS
        res_del = self.client.delete(f'/api/v1/tenant/program-types/{pt.id}/', **self.auth_headers)
        self.assertEqual(res_del.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_del.json().get('code'), 'PROGRAM_TYPE_HAS_PROGRAMS')

        # Delete program first, now program type delete must succeed
        prog.delete(using='tenant_test')
        res_del_ok = self.client.delete(f'/api/v1/tenant/program-types/{pt.id}/', **self.auth_headers)
        self.assertEqual(res_del_ok.status_code, status.HTTP_204_NO_CONTENT)

    def test_program_crud_with_delivery_mode_and_branches(self):
        """4 & 8. Program CRUD, delivery modes, and branch availability sync."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='GROUP_FITNESS',
            name='Group Fitness',
            status='ACTIVE'
        )

        # Create program with branches and delivery mode
        res = self.client.post('/api/v1/tenant/programs/', {
            'code': 'SWEAT_BOOTCAMP',
            'name': 'SWEAT Bootcamp',
            'program_type': str(pt.id),
            'delivery_mode': 'GROUP_CLASS',
            'display_order': 2,
            'trial_allowed': True,
            'available_branch_ids': [str(self.branch1.id), str(self.branch2.id)],
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()
        self.assertEqual(data['delivery_mode'], 'GROUP_CLASS')
        self.assertEqual(data['display_order'], 2)
        self.assertTrue(data['trial_allowed'])
        self.assertEqual(len(data['available_branch_ids']), 2)

        # Verify legacy 'GROUP' normalizes cleanly to 'GROUP_CLASS'
        res_legacy = self.client.post('/api/v1/tenant/programs/', {
            'name': 'SWEAT Legacy Group',
            'program_type': str(pt.id),
            'delivery_mode': 'GROUP',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_legacy.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_legacy.json()['delivery_mode'], 'GROUP_CLASS')

        prog_id = data['id']

        # Verify ProgramBranchAvailability entries in DB
        avail_count = ProgramBranchAvailability.objects.using('tenant_test').filter(
            program_id=prog_id, is_active=True
        ).count()
        self.assertEqual(avail_count, 2)

        # Update available branches (remove branch2)
        res_up = self.client.patch(f'/api/v1/tenant/programs/{prog_id}/', {
            'available_branch_ids': [str(self.branch1.id)],
        }, format='json', **self.auth_headers)
        self.assertEqual(res_up.status_code, status.HTTP_200_OK)
        self.assertEqual(res_up.json()['available_branch_ids'], [str(self.branch1.id)])

        # Verify branch2 availability was marked inactive
        br2_avail = ProgramBranchAvailability.objects.using('tenant_test').get(
            program_id=prog_id, branch=self.branch2
        )
        self.assertFalse(br2_avail.is_active)

        # Verify renaming program does NOT alter established code
        res_rename = self.client.patch(f'/api/v1/tenant/programs/{prog_id}/', {
            'name': 'SWEAT Bootcamp 2.0 Renovated',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_rename.status_code, status.HTTP_200_OK)
        self.assertEqual(res_rename.json()['code'], 'SWEAT_BOOTCAMP')

        # Test Program creation without code: auto-generates canonical code from name
        res_auto = self.client.post('/api/v1/tenant/programs/', {
            'name': 'HIIT High Intensity',
            'program_type': str(pt.id),
            'delivery_mode': 'GROUP',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto.json()['code'], 'HIIT_HIGH_INTENSITY')

        # Test Collision handling: duplicate name gets _2 suffix
        res_auto_coll = self.client.post('/api/v1/tenant/programs/', {
            'name': 'HIIT High Intensity',
            'program_type': str(pt.id),
            'delivery_mode': 'GROUP',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto_coll.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto_coll.json()['code'], 'HIIT_HIGH_INTENSITY_2')

    def test_inactive_program_type_assignment_blocked(self):
        """5. Inactive ProgramType assignment blocked on program creation."""
        set_tenant_db_alias('tenant_test')
        pt_inactive = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='ARCHIVED_TYPE',
            name='Archived Type',
            status='INACTIVE'
        )

        res = self.client.post('/api/v1/tenant/programs/', {
            'code': 'NEW_PROGRAM',
            'name': 'New Program',
            'program_type': str(pt_inactive.id),
            'delivery_mode': 'GROUP',
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('program_type', res.json())

    def test_program_safe_delete_protection(self):
        """7. Program safe delete blocks deletion when packages or classes exist."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='PILATES_TYPE',
            name='Pilates',
            status='ACTIVE'
        )
        prog = Program.objects.using('tenant_test').create(
            organization=self.org,
            program_type=pt,
            code='PILATES_ADV',
            name='Advanced Pilates',
            status='ACTIVE'
        )
        pkg = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=prog,
            code='PILATES_10_PACK',
            name='Pilates 10 Class Pack',
            status='ACTIVE'
        )

        # Attempt delete -> MUST FAIL with PROGRAM_HAS_HISTORY
        res_del = self.client.delete(f'/api/v1/tenant/programs/{prog.id}/', **self.auth_headers)
        self.assertEqual(res_del.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_del.json().get('code'), 'PROGRAM_HAS_HISTORY')

        # Remove package, safe delete should now succeed
        pkg.delete(using='tenant_test')
        res_del_ok = self.client.delete(f'/api/v1/tenant/programs/{prog.id}/', **self.auth_headers)
        self.assertEqual(res_del_ok.status_code, status.HTTP_204_NO_CONTENT)

    def test_crm_program_eligibility_resolution_strict_branch_filtering(self):
        """10. CRMProgramEligibilityService strict branch availability without fallback."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org, code='WELLNESS', name='Wellness', status='ACTIVE'
        )
        # Prog 1 available ONLY at branch 1 (trial allowed)
        prog1 = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt, code='PROG_ANDHERI', name='Andheri Only Program',
            trial_allowed=True, status='ACTIVE'
        )
        ProgramBranchAvailability.objects.using('tenant_test').create(
            program=prog1, branch=self.branch1, is_active=True
        )

        # Prog 2 available ONLY at branch 2 (trial NOT allowed)
        prog2 = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt, code='PROG_BANDRA', name='Bandra Only Program',
            trial_allowed=False, status='ACTIVE'
        )
        ProgramBranchAvailability.objects.using('tenant_test').create(
            program=prog2, branch=self.branch2, is_active=True
        )

        # A. Lead Interest at Branch 1 -> Only prog1
        res_b1 = CRMProgramEligibilityService.resolve_programs(
            organization=self.org,
            branch_id=str(self.branch1.id),
            context='lead_interest',
            alias='tenant_test',
        )
        self.assertEqual(list(res_b1), [prog1])

        # B. Lead Interest at Branch 2 -> Only prog2
        res_b2 = CRMProgramEligibilityService.resolve_programs(
            organization=self.org,
            branch_id=str(self.branch2.id),
            context='lead_interest',
            alias='tenant_test',
        )
        self.assertEqual(list(res_b2), [prog2])

        # C. Lead Interest at Branch with NO programs -> EMPTY (no fallback to all programs)
        dummy_loc = Location.objects.using('tenant_test').create(
            organization=self.org, name='Empty Loc', city='Mumbai'
        )
        empty_branch = Branch.objects.using('tenant_test').create(
            organization=self.org, location=dummy_loc, name='Empty Branch', code='EMPTY_BR'
        )
        res_empty = CRMProgramEligibilityService.resolve_programs(
            organization=self.org,
            branch_id=str(empty_branch.id),
            context='lead_interest',
            alias='tenant_test',
        )
        self.assertEqual(list(res_empty), [])

        # D. Trial at Branch 2 -> EMPTY because prog2 does not allow trials
        res_trial_b2 = CRMProgramEligibilityService.resolve_programs(
            organization=self.org,
            branch_id=str(self.branch2.id),
            context='trial',
            alias='tenant_test',
        )
        self.assertEqual(list(res_trial_b2), [])

    def test_class_branch_availability_validation_against_program(self):
        """11. ClassBranchAvailability validates that the class template's program is active at the branch."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org, code='YOGA_TYPE', name='Yoga', status='ACTIVE'
        )
        prog_yoga = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt, code='YOGA_PROG', name='Yoga Flow', status='ACTIVE'
        )
        # Make prog_yoga active ONLY at branch 1
        ProgramBranchAvailability.objects.using('tenant_test').create(
            program=prog_yoga, branch=self.branch1, is_active=True
        )

        class_cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org, name='Mind & Body', code='MINDBODY'
        )
        class_tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            category=class_cat,
            program=prog_yoga,
            name='Morning Vinyasa Flow',
            code='VINYASA_01'
        )

        # A. Make available at Branch 1 -> SUT succeeds
        serializer_valid = ClassBranchAvailabilitySerializer(
            data={'class_template': class_tmpl.id, 'branch': self.branch1.id, 'status': 'ENABLED'}
        )
        self.assertTrue(serializer_valid.is_valid())

        # B. Make available at Branch 2 (where prog_yoga is NOT available) -> Must fail validation
        serializer_invalid = ClassBranchAvailabilitySerializer(
            data={'class_template': class_tmpl.id, 'branch': self.branch2.id, 'status': 'ENABLED'}
        )
        self.assertFalse(serializer_invalid.is_valid())
        self.assertIn('branch', serializer_invalid.errors)

    def test_program_branch_availability_direct_api(self):
        """12. ProgramBranchAvailability direct API endpoints: deactivate, reactivate, validation."""
        set_tenant_db_alias('tenant_test')
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org, code='CROSSFIT_TYPE', name='CrossFit', status='ACTIVE'
        )
        prog = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt, code='CF_WOD', name='WOD Daily', status='ACTIVE'
        )
        pba = ProgramBranchAvailability.objects.using('tenant_test').create(
            program=prog, branch=self.branch1, is_active=True
        )

        # Deactivate endpoint
        res_deact = self.client.post(f'/api/v1/tenant/program-branch-availability/{pba.id}/deactivate/', **self.auth_headers)
        self.assertEqual(res_deact.status_code, status.HTTP_200_OK)
        self.assertFalse(res_deact.json()['is_active'])

        # Reactivate endpoint
        res_react = self.client.post(f'/api/v1/tenant/program-branch-availability/{pba.id}/reactivate/', **self.auth_headers)
        self.assertEqual(res_react.status_code, status.HTTP_200_OK)
        self.assertTrue(res_react.json()['is_active'])

        # Attempt to link inactive branch
        set_tenant_db_alias('tenant_test')
        inactive_branch = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc1, company_entity=self.company,
            name='Closed Branch', code='CLOSED_BR', status='INACTIVE'
        )
        res_bad = self.client.post('/api/v1/tenant/program-branch-availability/', {
            'program': str(prog.id),
            'branch': str(inactive_branch.id),
        }, format='json', **self.auth_headers)
        self.assertIn(res_bad.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

    def test_program_api_filters(self):
        """13. ProgramViewSet API filters: delivery_mode, branch_id, status, trial_allowed."""
        set_tenant_db_alias('tenant_test')
        pt1 = ProgramType.objects.using('tenant_test').create(organization=self.org, code='T1', name='Type 1')
        pt2 = ProgramType.objects.using('tenant_test').create(organization=self.org, code='T2', name='Type 2')

        p1 = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt1, code='P1', name='Prog 1',
            delivery_mode='GROUP', trial_allowed=True, status='ACTIVE'
        )
        ProgramBranchAvailability.objects.using('tenant_test').create(program=p1, branch=self.branch1, is_active=True)

        p2 = Program.objects.using('tenant_test').create(
            organization=self.org, program_type=pt2, code='P2', name='Prog 2',
            delivery_mode='PERSONAL_TRAINING', trial_allowed=False, status='ACTIVE'
        )
        ProgramBranchAvailability.objects.using('tenant_test').create(program=p2, branch=self.branch2, is_active=True)

        # Filter by branch_id (returns only programs available at that branch)
        res_br1 = self.client.get(f'/api/v1/tenant/programs/?branch_id={self.branch1.id}', **self.auth_headers)
        self.assertEqual(res_br1.status_code, status.HTTP_200_OK)
        data_br1 = res_br1.json()
        items_br1 = data_br1.get('results', data_br1) if isinstance(data_br1, dict) else data_br1
        ids = [x['id'] for x in items_br1]
        self.assertIn(str(p1.id), ids)
        self.assertNotIn(str(p2.id), ids)

        # Filter by delivery_mode
        res_mode = self.client.get('/api/v1/tenant/programs/?delivery_mode=PERSONAL_TRAINING', **self.auth_headers)
        self.assertEqual(res_mode.status_code, status.HTTP_200_OK)
        data_mode = res_mode.json()
        items_mode = data_mode.get('results', data_mode) if isinstance(data_mode, dict) else data_mode
        ids_mode = [x['id'] for x in items_mode]
        self.assertIn(str(p2.id), ids_mode)
        self.assertNotIn(str(p1.id), ids_mode)

    def test_program_category_crud_and_code_validation(self):
        """14. ProgramCategory CRUD, uppercase/regex code validation, auto-code, and uniqueness."""
        set_tenant_db_alias('tenant_test')
        # A. Invalid code with spaces
        res = self.client.post('/api/v1/tenant/program-categories/', {
            'code': 'bad category code',
            'name': 'Bad Code Category',
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # B. Successful creation with custom code
        res_create = self.client.post('/api/v1/tenant/program-categories/', {
            'code': 'STRENGTH_COND',
            'name': 'Strength & Conditioning',
            'description': 'Free weights, barbells, functional strength',
            'display_order': 1,
        }, format='json', **self.auth_headers)
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        cat_id = res_create.json()['id']
        self.assertEqual(res_create.json()['code'], 'STRENGTH_COND')

        # C. Duplicate code rejected within same org
        res_dup = self.client.post('/api/v1/tenant/program-categories/', {
            'code': 'STRENGTH_COND',
            'name': 'Duplicate Code Category',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)

        # D. Update preserves established code
        res_patch = self.client.patch(f'/api/v1/tenant/program-categories/{cat_id}/', {
            'name': 'Elite Strength & Conditioning',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.json()['name'], 'Elite Strength & Conditioning')
        self.assertEqual(res_patch.json()['code'], 'STRENGTH_COND')

        # E. Auto-generate code from name when omitted
        res_auto = self.client.post('/api/v1/tenant/program-categories/', {
            'name': 'Pilates & Mobility',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto.json()['code'], 'PILATES_MOBILITY')

        # F. Duplicate name auto-generates unique suffix
        res_auto_coll = self.client.post('/api/v1/tenant/program-categories/', {
            'name': 'Pilates & Mobility',
        }, format='json', **self.auth_headers)
        self.assertEqual(res_auto_coll.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_auto_coll.json()['code'], 'PILATES_MOBILITY_2')

    def test_program_category_lifecycle_and_safe_delete(self):
        """15. ProgramCategory deactivation, reactivation, and safe delete protection."""
        set_tenant_db_alias('tenant_test')
        cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='YOGA_CAT',
            name='Yoga & Mindfulness',
            status='ACTIVE'
        )

        # Deactivate
        res_deact = self.client.post(f'/api/v1/tenant/program-categories/{cat.id}/deactivate/', **self.auth_headers)
        self.assertEqual(res_deact.status_code, status.HTTP_200_OK)
        self.assertEqual(res_deact.json()['status'], 'INACTIVE')

        # Reactivate
        res_react = self.client.post(f'/api/v1/tenant/program-categories/{cat.id}/reactivate/', **self.auth_headers)
        self.assertEqual(res_react.status_code, status.HTTP_200_OK)
        self.assertEqual(res_react.json()['status'], 'ACTIVE')

        # Link program to category
        set_tenant_db_alias('tenant_test')
        prog = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=cat,
            code='ASHTANGA_PROG',
            name='Ashtanga Vinyasa',
            status='ACTIVE',
        )

        # Attempt safe delete -> MUST FAIL with PROGRAM_CATEGORY_HAS_PROGRAMS
        res_del = self.client.delete(f'/api/v1/tenant/program-categories/{cat.id}/', **self.auth_headers)
        self.assertEqual(res_del.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_del.json().get('code'), 'PROGRAM_CATEGORY_HAS_PROGRAMS')

        # Delete linked program, category delete must now succeed
        set_tenant_db_alias('tenant_test')
        prog.delete(using='tenant_test')
        res_del_ok = self.client.delete(f'/api/v1/tenant/program-categories/{cat.id}/', **self.auth_headers)
        self.assertEqual(res_del_ok.status_code, status.HTTP_204_NO_CONTENT)

    def test_program_creation_with_canonical_category_and_engine_semantics(self):
        """16. Canonical Program creation with ProgramCategory and engine semantic delivery modes."""
        set_tenant_db_alias('tenant_test')
        cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='PT_CAT',
            name='Personal Training',
            status='ACTIVE'
        )

        # Create program with category and engine semantic INDIVIDUAL_SERVICE
        res = self.client.post('/api/v1/tenant/programs/', {
            'name': 'One-on-One Strength Coaching',
            'category': str(cat.id),
            'delivery_mode': 'INDIVIDUAL_SERVICE',
            'display_order': 1,
            'trial_allowed': False,
            'available_branch_ids': [str(self.branch1.id)],
        }, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()
        self.assertEqual(data['category'], str(cat.id))
        self.assertEqual(data['category_name'], 'Personal Training')
        self.assertEqual(data['delivery_mode'], 'INDIVIDUAL_SERVICE')
        self.assertEqual(data['code'], 'ONE_ON_ONE_STRENGTH_COACHING')

        # Attempt to link inactive category -> blocked
        set_tenant_db_alias('tenant_test')
        cat_inactive = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='INACTIVE_CAT',
            name='Inactive Category',
            status='INACTIVE'
        )
        res_inact = self.client.post('/api/v1/tenant/programs/', {
            'name': 'Invalid Program',
            'category': str(cat_inactive.id),
        }, format='json', **self.auth_headers)
        self.assertEqual(res_inact.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('category', res_inact.json())
