import uuid
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    ProductModule,
    SaasPlan,
    Tenant,
    TenantDataSource,
    TenantModule,
    TenantSubscription,
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_catalog import (
    Package,
    PackageEntitlementDefinition,
    PackagePrice,
    PackageVersion,
    Program,
    ProgramCategory,
)
from apps.tenant_core.models_commerce import Order
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_rbac import (
    ModuleCatalog,
    Permission,
    Role,
    RoleAssignment,
    RoleModuleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleSubmoduleAccess,
    SubmoduleCatalog,
)
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile

DB = 'tenant_test'
OTHER_DB = 'tenant_other'


class MemberPlansSessionsTestCase(APITestCase):
    databases = {'default', 'tenant_test', 'tenant_other'}

    def setUp(self):
        set_tenant_db_alias(DB)

        self.tenant = Tenant.objects.using('default').create(
            code=f'T-PLAN-{uuid.uuid4().hex[:6]}',
            name='Plan Test Gym',
            slug=f'plan-gym-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code=f'ENT-{uuid.uuid4().hex[:6]}',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=plan,
            status='ACTIVE',
        )
        mod_core_prod, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Platform', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=mod_core_prod,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        self.org = Organization.objects.using(DB).create(
            code=f'ORG-{uuid.uuid4().hex[:6]}',
            name='Plan Test Org',
            status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org,
            code=f'LOC-{uuid.uuid4().hex[:6]}',
            name='HQ',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=loc,
            code=f'BR-{uuid.uuid4().hex[:6]}',
            name='Andheri West',
            status='ACTIVE',
        )

        self.admin_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='admin@plans.test',
            first_name='Admin',
            last_name='Tester',
            status='ACTIVE',
            user_type='ADMIN',
        )
        self.admin_user.set_password('AdminPass123!')
        self.admin_user.save(using=DB)

        self.no_perm_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='noperm@plans.test',
            first_name='NoPerm',
            last_name='Tester',
            status='ACTIVE',
            user_type='STAFF',
        )
        self.no_perm_user.set_password('StaffPass123!')
        self.no_perm_user.save(using=DB)

        role_admin = Role.objects.using(DB).create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.admin_user,
            role=role_admin,
            organization=self.org,
            is_active=True,
        )
        mod_core, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True},
        )
        sub_users, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=role_admin, module=mod_core, defaults={'can_access': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=role_admin, submodule=sub_users, defaults={'can_access': True},
        )
        perm_users_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core,
            submodule=sub_users,
            action='view',
            defaults={'permission_code': 'core.users.view', 'label': 'View Users'},
        )
        perm_set = RolePermissionSet.objects.using(DB).create(
            role=role_admin, name='Admin Users View Set',
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=perm_set, permission=perm_users_view, granted=True,
        )

        self.cat = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            code='PILATES_GRP',
            name='Sweat Pilates - Group Classes',
            status='ACTIVE',
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org,
            category=self.cat,
            code='SWEAT_PILATES',
            name='Sweat Pilates',
            delivery_mode='GROUP_CLASS',
            trial_allowed=True,
            status='ACTIVE',
        )

    def _auth(self, user, tenant=None, alias=DB):
        target_tenant = tenant or self.tenant
        refresh = _build_tenant_token(user=user, tenant=target_tenant, db_alias=alias)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def _create_package_with_version(
        self,
        *,
        name: str,
        code: str,
        version_status: str = 'ACTIVE',
        version_number: int = 1,
        duration_value: int = 30,
        duration_unit: str = 'DAY',
        validity_days: int | None = 30,
        price: Decimal = Decimal('0.00'),
        entitlements: list[tuple[str, Decimal | None, bool]] | None = None,
        pkg_status: str = 'ACTIVE',
    ) -> tuple[Package, PackageVersion]:
        pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            code=code,
            name=name,
            status=pkg_status,
        )
        ver = PackageVersion.objects.using(DB).create(
            package=pkg,
            version_number=version_number,
            name_snapshot=f'{name} v{version_number}',
            duration_value=duration_value,
            duration_unit=duration_unit,
            total_days=validity_days or duration_value,
            validity_days=validity_days,
            effective_from=timezone.now(),
            status=version_status,
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=ver,
            currency='INR',
            base_price=price,
            prices_include_tax=False,
            tax_percent=Decimal('18.000'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        for ent_type, units, is_unlim in (entitlements or []):
            PackageEntitlementDefinition.objects.using(DB).create(
                package_version=ver,
                entitlement_type=ent_type,
                allocated_units=units,
                is_unlimited=is_unlim,
                status='ACTIVE',
            )
        return pkg, ver

    def test_plans_requires_auth_and_permission(self):
        self.client.credentials()
        unauth_resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(unauth_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        self._auth(self.no_perm_user)
        denied_resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(denied_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_trial_session_without_active_version_resolves_real_sessions_and_validity(self):
        """
        Regression: Trial session with no digits in name and no ACTIVE PackageVersion
        (e.g., retired v1 + draft v4, or retired v5) must still resolve 1 session and 30 days
        to match /ops/programs instead of returning total_sessions=None (which triggered 24).
        """
        pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            code='PIL_TRIAL',
            name='Trial session',
            status='ACTIVE',
        )
        v1 = PackageVersion.objects.using(DB).create(
            package=pkg,
            version_number=1,
            name_snapshot='Trial session v1',
            duration_value=90,
            duration_unit='DAY',
            total_days=90,
            validity_days=90,
            is_trial=True,
            is_trial_package=True,
            effective_from=timezone.now(),
            status='RETIRED',
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=v1,
            currency='INR',
            base_price=Decimal('0.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=v1,
            entitlement_type='CLASS_SESSION',
            allocated_units=Decimal('1.00'),
            is_unlimited=False,
            status='ACTIVE',
        )
        v5 = PackageVersion.objects.using(DB).create(
            package=pkg,
            version_number=5,
            name_snapshot='Trial session v5',
            duration_value=30,
            duration_unit='DAY',
            total_days=30,
            validity_days=30,
            is_trial=True,
            is_trial_package=True,
            effective_from=timezone.now(),
            status='RETIRED',
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=v5,
            currency='INR',
            base_price=Decimal('0.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=v5,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('1.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

        self._auth(self.admin_user)
        resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {row['name']: row for row in resp.data}
        self.assertIn('Trial session', rows)
        trial = rows['Trial session']
        self.assertEqual(trial['total_sessions'], 1)
        self.assertFalse(trial['is_unlimited'])
        self.assertEqual(trial['validity_days'], 30)
        self.assertEqual(trial['price'], 0.0)

    def test_plans_entitlement_types_cross_branch_credit_and_unlimited(self):
        # 1. Active trial pack with PT/OpenAccess entitlement (no digits in name)
        self._create_package_with_version(
            name='Trial session PT',
            code='PT_TRIAL',
            duration_value=14,
            validity_days=14,
            price=Decimal('0.00'),
            entitlements=[('PERSONAL_TRAINING_SESSION', Decimal('1.00'), False)],
        )
        # 2. Cross-branch passport must NOT inflate home session total
        self._create_package_with_version(
            name='Intro Pack - 5 session',
            code='INTRO_5',
            duration_value=12,
            validity_days=12,
            price=Decimal('7999.00'),
            entitlements=[
                ('HOME_BRANCH_SESSION', Decimal('4.00'), False),
                ('CROSS_BRANCH_SESSION', Decimal('1.00'), False),
            ],
        )
        # 3. Credit wallet pack without digits in name
        self._create_package_with_version(
            name='Sweat Elite Starter',
            code='ELITE_START',
            duration_value=84,
            validity_days=84,
            price=Decimal('49999.00'),
            entitlements=[
                ('HOME_BRANCH_SESSION', Decimal('0.00'), False),
                ('CREDIT_WALLET', Decimal('75.00'), False),
            ],
        )
        # 4. Unlimited access pack
        self._create_package_with_version(
            name='All Access Monthly',
            code='ALL_UNLIM',
            duration_value=30,
            validity_days=30,
            price=Decimal('19999.00'),
            entitlements=[('OPEN_ACCESS', None, True)],
        )
        # 5. Inactive package must be excluded
        self._create_package_with_version(
            name='Retired Pack',
            code='RET_PACK',
            pkg_status='INACTIVE',
            entitlements=[('HOME_BRANCH_SESSION', Decimal('10.00'), False)],
        )

        self._auth(self.admin_user)
        resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {row['name']: row for row in resp.data}

        self.assertEqual(rows['Trial session PT']['total_sessions'], 1)
        self.assertEqual(rows['Trial session PT']['validity_days'], 14)
        self.assertEqual(rows['Intro Pack - 5 session']['total_sessions'], 4)
        self.assertEqual(rows['Sweat Elite Starter']['total_sessions'], 75)
        self.assertTrue(rows['All Access Monthly']['is_unlimited'])
        self.assertIsNone(rows['All Access Monthly']['total_sessions'])
        self.assertNotIn('Retired Pack', rows)

    def test_plans_organization_and_tenant_isolation(self):
        self._create_package_with_version(
            name='Trial session',
            code='PIL_TRIAL_ISO',
            entitlements=[('HOME_BRANCH_SESSION', Decimal('1.00'), False)],
        )
        # Ensure tenant_other DB remains completely isolated
        self.assertEqual(Package.objects.using(OTHER_DB).count(), 0)

        # User in a separate Organization within the same tenant DB cannot see self.org's packages
        other_org = Organization.objects.using(DB).create(
            code=f'ORG-OTH-{uuid.uuid4().hex[:6]}',
            name='Other Org',
            status='ACTIVE',
        )
        other_user = TenantUser.objects.using(DB).create(
            organization=other_org,
            email='admin@other.test',
            first_name='Other',
            last_name='Admin',
            status='ACTIVE',
            user_type='ADMIN',
        )
        other_role = Role.objects.using(DB).create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=other_org,
            is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=other_user, role=other_role, organization=other_org, is_active=True,
        )
        mod_core, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True},
        )
        sub_users, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=other_role, module=mod_core, defaults={'can_access': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=other_role, submodule=sub_users, defaults={'can_access': True},
        )
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core,
            submodule=sub_users,
            action='view',
            defaults={'permission_code': 'core.users.view', 'label': 'View Users'},
        )
        pset = RolePermissionSet.objects.using(DB).create(role=other_role, name='Other View')
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=pset, permission=perm_view, granted=True,
        )

        self._auth(other_user, tenant=self.tenant, alias=DB)
        resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_plans_prefers_selling_base_price_over_mrp_display_price(self):
        """
        plans() must return selling price (base_price), never crossed-out MRP (display_price),
        even when falling back to an older version's price.
        """
        pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            code='PIL_ONE',
            name='1 session',
            status='ACTIVE',
        )
        ver = PackageVersion.objects.using(DB).create(
            package=pkg,
            version_number=1,
            name_snapshot='1 session v1',
            duration_value=5,
            duration_unit='DAY',
            total_days=5,
            validity_days=5,
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=ver,
            currency='INR',
            base_price=Decimal('1499.00'),
            display_price=Decimal('2499.00'),
            prices_include_tax=True,
            tax_percent=Decimal('5.000'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=ver,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('1.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

        # Price-less v4 Trial session falling back to v1 (base_price=0, display_price=500 MRP)
        trial_pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            code='BOOT_TRIAL',
            name='Trial session',
            status='ACTIVE',
        )
        trial_v1 = PackageVersion.objects.using(DB).create(
            package=trial_pkg,
            version_number=1,
            name_snapshot='Trial session v1',
            duration_value=90,
            duration_unit='DAY',
            total_days=90,
            validity_days=90,
            is_trial=True,
            is_trial_package=True,
            effective_from=timezone.now(),
            status='RETIRED',
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=trial_v1,
            currency='INR',
            base_price=Decimal('0.00'),
            display_price=Decimal('500.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        trial_v4 = PackageVersion.objects.using(DB).create(
            package=trial_pkg,
            version_number=4,
            name_snapshot='Trial session v4',
            duration_value=30,
            duration_unit='DAY',
            total_days=30,
            validity_days=30,
            is_trial=True,
            is_trial_package=True,
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=trial_v4,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('1.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

        self._auth(self.admin_user)
        resp = self.client.get('/api/v1/tenant/members/plans/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {row['name']: row for row in resp.data}
        self.assertEqual(rows['1 session']['price'], 1499.0)
        self.assertEqual(rows['Trial session']['price'], 0.0)
        self.assertEqual(rows['Trial session']['total_sessions'], 1)
        self.assertEqual(rows['Trial session']['validity_days'], 30)

    def test_purchase_renew_rejoin_never_fabricate_5000_when_version_has_no_price(self):
        """
        Purchasing, renewing, or rejoining on a price-less version (e.g. Bootcamp Trial v4)
        must resolve prior active price or 0.00 — never a hardcoded 5000.00 placeholder.
        """
        perm_edit, _ = Permission.objects.using(DB).get_or_create(
            module=ModuleCatalog.objects.using(DB).get(module_code='core'),
            submodule=SubmoduleCatalog.objects.using(DB).get(submodule_code='users'),
            action='edit',
            defaults={'permission_code': 'core.users.edit', 'label': 'Edit Users'},
        )
        pset = RolePermissionSet.objects.using(DB).filter(role__code='ORG_ADMIN').first()
        RolePermissionSetItem.objects.using(DB).get_or_create(
            permission_set=pset, permission=perm_edit, defaults={'granted': True},
        )

        member_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='member@plans.test',
            first_name='Trial',
            last_name='Member',
            status='ACTIVE',
            user_type='MEMBER',
        )
        profile = UserProfile.objects.using(DB).create(
            user=member_user,
            member_number='MEM-PLAN-001',
            preferred_branch=self.branch,
            member_status='EXPIRED',
        )

        # Package 1: price-less ACTIVE v4 with v1 priced at 0.00 (MRP 500.00)
        trial_pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            code='BOOT_TRIAL_NOPRICE',
            name='Trial session',
            status='ACTIVE',
        )
        trial_v1 = PackageVersion.objects.using(DB).create(
            package=trial_pkg,
            version_number=1,
            name_snapshot='Trial session v1',
            duration_value=90,
            duration_unit='DAY',
            total_days=90,
            validity_days=90,
            effective_from=timezone.now(),
            status='RETIRED',
            created_by_user=self.admin_user,
        )
        PackagePrice.objects.using(DB).create(
            package_version=trial_v1,
            currency='INR',
            base_price=Decimal('0.00'),
            display_price=Decimal('500.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        trial_v4 = PackageVersion.objects.using(DB).create(
            package=trial_pkg,
            version_number=4,
            name_snapshot='Trial session v4',
            duration_value=30,
            duration_unit='DAY',
            total_days=30,
            validity_days=30,
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=trial_v4,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('1.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

        # Package 2: completely price-less package -> must default to 0.00, never 5000.00
        noprice_pkg, _ = self._create_package_with_version(
            name='Complimentary Trial',
            code='COMP_TRIAL',
            entitlements=[('HOME_BRANCH_SESSION', Decimal('1.00'), False)],
        )
        PackagePrice.objects.using(DB).filter(package_version__package=noprice_pkg).delete()

        self._auth(self.admin_user)

        # 1. purchase-package on price-less v4 -> order total must be 0.00, not 5000.00
        res_purchase = self.client.post(
            f'/api/v1/tenant/members/{profile.id}/purchase-package/',
            {
                'package_id': str(trial_pkg.id),
                'branch_id': str(self.branch.id),
                'payment_method': 'CASH',
                'payment_provider': 'CASH',
                'payment_amount': 0,
            },
            format='json',
        )
        self.assertEqual(res_purchase.status_code, status.HTTP_201_CREATED)
        order_p = Order.objects.using(DB).get(id=res_purchase.data['order_id'])
        self.assertEqual(order_p.total_amount, Decimal('0.00'))

        # 2. renew on completely price-less package -> order total must be 0.00, not 5000.00
        res_renew = self.client.post(
            f'/api/v1/tenant/members/{profile.id}/renew/',
            {
                'package_id': str(noprice_pkg.id),
                'payment_method': 'CASH',
                'payment_provider': 'CASH',
                'payment_amount': 0,
            },
            format='json',
        )
        self.assertEqual(res_renew.status_code, status.HTTP_201_CREATED)
        order_rn = Order.objects.using(DB).filter(user_profile=profile).order_by('-created_at').first()
        self.assertEqual(order_rn.total_amount, Decimal('0.00'))

        # 3. rejoin on price-less v4 -> order total must be 0.00, not 5000.00
        res_rejoin = self.client.post(
            f'/api/v1/tenant/members/{profile.id}/rejoin/',
            {
                'package_id': str(trial_pkg.id),
                'branch_id': str(self.branch.id),
                'payment_method': 'CASH',
                'payment_provider': 'CASH',
                'payment_amount': 0,
            },
            format='json',
        )
        self.assertEqual(res_rejoin.status_code, status.HTTP_201_CREATED)
        order_rj = Order.objects.using(DB).filter(user_profile=profile).order_by('-created_at').first()
        self.assertEqual(order_rj.total_amount, Decimal('0.00'))
        Membership.objects.using(DB).filter(user_profile=profile).exists()
