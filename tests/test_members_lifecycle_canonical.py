"""
backend/tests/test_members_lifecycle_canonical.py — Canonical Members Lifecycle & Sensitive Health Permission Tests

Covers:
1. Real renewal creates new membership, preserves old membership, links Order/Payment, provisions entitlements.
2. Upgrade executes canonical quote_and_apply_change, creates MembershipPackageHistory (UPGRADE), preserves prior package.
3. Rejoin uses canonical commercial flow, preserves same UserProfile/member_number, activates new membership contract.
4. Invalid branch transfer rejected (inactive branch, program unavailable, package disabled).
5. Valid branch transfer accepted (MembershipBranchHistory created, home_branch updated).
6. Sensitive health answers protected by cs.member-health.view (redacted for ordinary sales rep, visible for authorized user).
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageEntitlementDefinition, ProgramBranchAvailability, PackageBranchAvailability
)
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
)
from apps.tenant_core.models_crm import IntakeForm, IntakeQuestion, IntakeSubmission, IntakeAnswer
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.services_commerce import CommerceService


class MembersLifecycleCanonicalTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master Tenant Setup
        self.tenant, _ = Tenant.objects.using('default').get_or_create(
            name="Lifecycle Gym",
            slug="lifecycle-gym",
            status="ACTIVE",
            defaults={"code": "LC-GYM"}
        )
        self.ds, _ = TenantDataSource.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={"db_name": "test_fitness_tenant", "database_name": "test_fitness_tenant", "status": "ACTIVE"}
        )
        if self.ds.db_name != 'test_fitness_tenant' or self.ds.database_name != 'test_fitness_tenant':
            self.ds.db_name = 'test_fitness_tenant'
            self.ds.database_name = 'test_fitness_tenant'
            self.ds.status = 'ACTIVE'
            self.ds.save(using='default')

        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='MEMBERSHIP-PLAN',
            defaults={'name': 'Membership Plan', 'tier': 'ENTERPRISE', 'status': 'ACTIVE'}
        )
        self.sub, _ = TenantSubscription.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={'plan': self.plan, 'status': 'ACTIVE'}
        )
        if self.sub.status != 'ACTIVE':
            self.sub.status = 'ACTIVE'
            self.sub.save(using='default')

        # 2. Modules in Master & Tenant
        for code, name in [
            ('core', 'Core Platform'),
            ('members', 'Member Operations'),
            ('memberships', 'Memberships & Changes'),
            ('commerce', 'Commerce & Billing'),
            ('finance', 'Finance & Payments'),
            ('crm', 'CRM & Pipeline'),
            ('cs', 'Customer Success & Health'),
        ]:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=code, defaults={'name': name, 'status': 'ACTIVE', 'is_core': True}
            )
            tm, _ = TenantModule.objects.using('default').get_or_create(
                tenant=self.tenant, module=pm, defaults={'status': 'ENABLED', 'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'}
            )
            if not tm.is_enabled or tm.status != 'ENABLED':
                tm.status = 'ENABLED'
                tm.is_enabled = True
                tm.save(using='default')
            ModuleCatalog.objects.using('tenant_test').get_or_create(
                module_code=code, defaults={'name': name, 'source_module_id': uuid.uuid4(), 'is_enabled': True, 'status': 'ACTIVE'}
            )

        # 3. Tenant Org & Branches
        self.org, _ = Organization.objects.using('tenant_test').get_or_create(
            name="Lifecycle Organization",
            defaults={"code": "LC_ORG", "status": "ACTIVE"}
        )
        self.loc, _ = Location.objects.using('tenant_test').get_or_create(
            organization=self.org,
            name="Main Campus",
            defaults={"code": "MAIN"}
        )
        self.branch1, _ = Branch.objects.using('tenant_test').get_or_create(
            organization=self.org,
            location=self.loc,
            name="Downtown Branch",
            defaults={"code": "DT", "status": "ACTIVE"}
        )
        self.branch2, _ = Branch.objects.using('tenant_test').get_or_create(
            organization=self.org,
            location=self.loc,
            name="Uptown Branch",
            defaults={"code": "UT", "status": "ACTIVE"}
        )
        self.branch_inactive, _ = Branch.objects.using('tenant_test').get_or_create(
            organization=self.org,
            location=self.loc,
            name="Closed Branch",
            defaults={"code": "CL", "status": "INACTIVE"}
        )

        # 4. Users
        self.admin_user, _ = TenantUser.objects.using('tenant_test').get_or_create(
            email="admin@lifecycle.test",
            defaults={"username": "lifecycle_admin", "status": "ACTIVE", "organization": self.org}
        )
        self.sales_user, _ = TenantUser.objects.using('tenant_test').get_or_create(
            email="sales@lifecycle.test",
            defaults={"username": "lifecycle_sales", "status": "ACTIVE", "organization": self.org}
        )

        # 5. Catalog Setup
        self.category, _ = ProgramCategory.objects.using('tenant_test').get_or_create(
            organization=self.org, name="Fitness", defaults={"code": "FIT"}
        )
        self.prog1, _ = Program.objects.using('tenant_test').get_or_create(
            organization=self.org,
            category=self.category,
            name="Pilates Program",
            defaults={"code": "PIL", "status": "ACTIVE"}
        )
        self.pkg1, _ = Package.objects.using('tenant_test').get_or_create(
            organization=self.org,
            program=self.prog1,
            name="Monthly Pilates",
            defaults={"code": "PIL-M", "status": "ACTIVE"}
        )
        self.pkg1_v1, _ = PackageVersion.objects.using('tenant_test').get_or_create(
            package=self.pkg1,
            version_number=1,
            defaults={"duration_value": 30, "duration_unit": "DAY", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )
        self.pkg1_price, _ = PackagePrice.objects.using('tenant_test').get_or_create(
            package_version=self.pkg1_v1,
            defaults={"base_price": Decimal("5000.00"), "currency": "INR", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )

        self.pkg2, _ = Package.objects.using('tenant_test').get_or_create(
            organization=self.org,
            program=self.prog1,
            name="Annual VIP Pilates",
            defaults={"code": "PIL-VIP", "status": "ACTIVE"}
        )
        self.pkg2_v1, _ = PackageVersion.objects.using('tenant_test').get_or_create(
            package=self.pkg2,
            version_number=1,
            defaults={"duration_value": 365, "duration_unit": "DAY", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )
        self.pkg2_price, _ = PackagePrice.objects.using('tenant_test').get_or_create(
            package_version=self.pkg2_v1,
            defaults={"base_price": Decimal("35000.00"), "currency": "INR", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )

        # Entitlement definitions
        PackageEntitlementDefinition.objects.using('tenant_test').get_or_create(
            package_version=self.pkg1_v1,
            entitlement_type="HOME_BRANCH_SESSION",
            defaults={"allocated_units": Decimal("12.00"), "is_unlimited": False}
        )
        PackageEntitlementDefinition.objects.using('tenant_test').get_or_create(
            package_version=self.pkg2_v1,
            entitlement_type="HOME_BRANCH_SESSION",
            defaults={"allocated_units": Decimal("150.00"), "is_unlimited": False}
        )

        # Change Policy
        self.change_policy, _ = MembershipChangePolicy.objects.using('tenant_test').get_or_create(
            organization=self.org,
            policy_name="Lifecycle Change Policy",
            defaults={"version_number": 1, "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )
        self.change_rule, _ = MembershipChangePolicyRule.objects.using('tenant_test').get_or_create(
            membership_change_policy=self.change_policy,
            change_type="UPGRADE",
            defaults={
                "rule_name": "Standard Upgrade Rule",
                "effective_mode": "IMMEDIATE",
                "pricing_mode": "DIFFERENCE_ONLY",
                "unused_session_handling": "CARRY_FORWARD",
            }
        )

        # Submodules & Permissions in catalog
        core_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='core')
        cs_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='cs')
        members_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='members')
        crm_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='crm')
        comm_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='commerce')
        mship_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='memberships')
        fin_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='finance')

        sub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=core_mod, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True}
        )
        sub_payments, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=fin_mod, submodule_code='payments', defaults={'name': 'Payments', 'is_enabled': True}
        )
        sub_health, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=cs_mod, submodule_code='member-health', defaults={'name': 'Member Health', 'is_enabled': True}
        )
        sub_360, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=members_mod, submodule_code='client-360', defaults={'name': 'Member 360', 'is_enabled': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=crm_mod, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )

        perm_core_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.view",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.view", "action": "view", "label": "View Users", "is_active": True}
        )
        perm_core_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.edit",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.edit", "action": "edit", "label": "Edit Users", "is_active": True}
        )
        perm_core_create, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.create",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.create", "action": "create", "label": "Create Users", "is_active": True}
        )
        self.health_perm, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="cs.member-health.view",
            defaults={"module": cs_mod, "submodule": sub_health, "source_permission_id": uuid.uuid4(), "code": "cs.member-health.view", "action": "view", "label": "Can view Member Health Index", "is_active": True}
        )
        self.members_perm, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="members.client-360.view",
            defaults={"module": members_mod, "submodule": sub_360, "source_permission_id": uuid.uuid4(), "code": "members.client-360.view", "action": "view", "label": "Can view Member 360", "is_active": True}
        )
        self.leads_perm, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="crm.leads.view",
            defaults={"module": crm_mod, "submodule": sub_leads, "source_permission_id": uuid.uuid4(), "code": "crm.leads.view", "action": "view", "label": "Can view Leads", "is_active": True}
        )
        self.perm_fin_pay, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="finance.payments.create",
            defaults={"module": fin_mod, "submodule": sub_payments, "source_permission_id": uuid.uuid4(), "code": "finance.payments.create", "action": "create", "label": "Create Payments", "is_active": True}
        )

        # 1. Org Admin Role Setup (full access)
        self.org_admin_role, _ = Role.objects.using('tenant_test').get_or_create(
            organization=self.org,
            code="ORG_ADMIN",
            defaults={"name": "Org Admin", "scope": "ORG", "is_system_role": True, "is_active": True}
        )
        admin_ps, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.org_admin_role, name="Admin PSet", defaults={"is_active": True}
        )
        for m in [core_mod, members_mod, crm_mod, cs_mod, comm_mod, mship_mod, fin_mod]:
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=self.org_admin_role, module=m, defaults={"permission_set": admin_ps, "can_access": True, "is_visible": True}
            )
        for s in [sub_users, sub_health, sub_360, sub_leads, sub_payments]:
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=self.org_admin_role, submodule=s, defaults={"permission_set": admin_ps, "can_access": True, "is_visible": True}
            )
        for p in [perm_core_view, perm_core_edit, perm_core_create, self.health_perm, self.members_perm, self.leads_perm, self.perm_fin_pay]:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=admin_ps, permission=p, defaults={"granted": True, "is_allowed": True}
            )
        RoleAssignment.objects.using('tenant_test').get_or_create(
            user=self.admin_user, role=self.org_admin_role, defaults={"organization": self.org, "is_active": True, "status": "ACTIVE"}
        )

        # 2. Sales Rep Role Setup (has members.client-360.view, crm.leads.view, core.users.view, but NOT cs.member-health.view)
        self.sales_role, _ = Role.objects.using('tenant_test').get_or_create(
            organization=self.org,
            code="SALES_REP",
            defaults={"name": "Sales Rep", "scope": "BRANCH", "is_system_role": True, "is_active": True}
        )
        sales_ps, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.sales_role, name="Sales Permissions", defaults={"is_active": True}
        )
        for m in [core_mod, members_mod, crm_mod]:
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=self.sales_role, module=m, defaults={"permission_set": sales_ps, "can_access": True, "is_visible": True}
            )
        for s in [sub_users, sub_360, sub_leads]:
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=self.sales_role, submodule=s, defaults={"permission_set": sales_ps, "can_access": True, "is_visible": True}
            )
        for p in [perm_core_view, self.members_perm, self.leads_perm]:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=sales_ps, permission=p, defaults={"granted": True, "is_allowed": True}
            )
        RoleAssignment.objects.using('tenant_test').get_or_create(
            user=self.sales_user, role=self.sales_role, defaults={"organization": self.org, "is_active": True, "status": "ACTIVE"}
        )

        # Auth Tokens
        admin_refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test'
        )
        self.admin_token = str(admin_refresh.access_token)

        sales_refresh = _build_tenant_token(
            user=self.sales_user,
            tenant=self.tenant,
            db_alias='tenant_test'
        )
        self.sales_token = str(sales_refresh.access_token)

        # Member UserProfile
        self.member_user, _ = TenantUser.objects.using('tenant_test').get_or_create(
            email="canonical.member@lifecycle.test",
            defaults={"username": "canonical_member", "first_name": "Canonical", "last_name": "Member", "status": "ACTIVE", "organization": self.org}
        )
        self.member_profile, _ = UserProfile.objects.using('tenant_test').get_or_create(
            user=self.member_user,
            defaults={"member_number": "MEM-CANON-001", "preferred_branch": self.branch1, "member_status": "ACTIVE", "member_type": "MEMBER"}
        )

    def _create_active_membership(self):
        today = timezone.now().date()
        order = CommerceService.create_order(
            branch=self.branch1,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': self.pkg1.id,
                'package_version_id': self.pkg1_v1.id,
                'package_price_id': self.pkg1_price.id,
                'item_name_snapshot': "Monthly Pilates (v1)",
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('5000.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }],
            user_profile=self.member_profile,
            order_type='NEW_MEMBERSHIP',
            source='FRONT_DESK',
            currency='INR',
            db_alias='tenant_test',
        )
        CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('5000.00'),
            provider='CASH',
            payment_method='CASH',
            db_alias='tenant_test',
        )
        mem = MembershipLifecycleService.activate_membership_from_order(
            order=order,
            order_item=order.items.first(),
            start_date=today,
            db_alias='tenant_test',
        )
        return mem, order

    # =========================================================================
    # 1. RENEWAL TEST
    # =========================================================================
    def test_real_renewal_creates_new_membership_and_preserves_old(self):
        old_mem, initial_order = self._create_active_membership()
        old_mem_id = old_mem.id
        old_end_date = old_mem.end_date

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/renew/",
            {"package_id": str(self.pkg1.id), "payment_amount": 5000.00, "reason": "Annual contract renewal"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertTrue(data['success'])

        new_mem_id = data['new_membership_id']
        self.assertNotEqual(str(old_mem_id), str(new_mem_id))

        # Verify old membership remains historically preserved and unchanged
        old_mem.refresh_from_db(using='tenant_test')
        self.assertEqual(old_mem.end_date, old_end_date)
        self.assertEqual(old_mem.id, old_mem_id)

        # Verify new membership created with active entitlements and snapshot
        new_mem = Membership.objects.using('tenant_test').select_related('user_profile').get(id=new_mem_id)
        self.assertEqual(new_mem.start_date, old_end_date + timedelta(days=1))
        self.assertEqual(new_mem.user_profile.id, self.member_profile.id)
        self.assertIsNotNone(new_mem.contract_snapshot)
        self.assertTrue(new_mem.entitlements.filter(status='ACTIVE').exists())

        # Verify order & payment linked
        new_order = Order.objects.using('tenant_test').get(id=data['order_id'])
        self.assertEqual(new_order.order_type, 'RENEWAL')
        self.assertEqual(new_order.status, 'PAID')

    # =========================================================================
    # 2. UPGRADE TEST
    # =========================================================================
    def test_upgrade_creates_package_history_and_applies_policy(self):
        mem, _ = self._create_active_membership()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/upgrade/",
            {"package_id": str(self.pkg2.id), "reason": "Upgraded to VIP Annual"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['success'])

        # Verify package upgraded
        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.package_id, self.pkg2.id)
        self.assertEqual(mem.package_version_id, self.pkg2_v1.id)

        # Verify MembershipPackageHistory entry
        pkg_hist = MembershipPackageHistory.objects.using('tenant_test').filter(
            membership=mem, change_type='UPGRADE'
        ).first()
        self.assertIsNotNone(pkg_hist)
        self.assertEqual(pkg_hist.from_package_id, self.pkg1.id)
        self.assertEqual(pkg_hist.to_package_id, self.pkg2.id)

        # Verify MembershipChangeRequest created
        self.assertTrue(MembershipChangeRequest.objects.using('tenant_test').filter(membership=mem, change_type='UPGRADE').exists())

    # =========================================================================
    # 3. CANONICAL REJOIN TEST
    # =========================================================================
    def test_canonical_rejoin_uses_same_profile_with_new_contract(self):
        old_mem, _ = self._create_active_membership()
        old_mem.status = 'CANCELLED'
        old_mem.save(using='tenant_test', update_fields=['status'])
        self.member_profile.member_status = 'CANCELLED'
        self.member_profile.save(using='tenant_test', update_fields=['member_status'])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/rejoin/",
            {
                "package_id": str(self.pkg1.id),
                "package_version_id": str(self.pkg1_v1.id),
                "branch_id": str(self.branch1.id),
                "payment_amount": 5000.00,
                "reason": "Member returned for autumn season"
            },
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertTrue(data['success'])

        # Verify identity preserved
        self.member_profile.refresh_from_db(using='tenant_test')
        self.assertEqual(self.member_profile.member_number, "MEM-CANON-001")
        self.assertEqual(self.member_profile.member_status, "ACTIVE")

        # Verify both old and new memberships exist under the same profile
        m_count = self.member_profile.memberships.using('tenant_test').count()
        self.assertGreaterEqual(m_count, 2)

        # Verify new contract snapshot and entitlements created
        new_mem = Membership.objects.using('tenant_test').get(id=data['membership_id'])
        self.assertNotEqual(new_mem.id, old_mem.id)
        self.assertIsNotNone(new_mem.contract_snapshot)
        self.assertTrue(new_mem.entitlements.filter(status='ACTIVE').exists())

    # =========================================================================
    # 4. TRANSFER BRANCH ELIGIBILITY TESTS
    # =========================================================================
    def test_invalid_transfer_branch_rejected(self):
        mem, _ = self._create_active_membership()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")

        # Case A: Inactive branch
        resp_inactive = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/transfer/",
            {"branch_id": str(self.branch_inactive.id)},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertIn(resp_inactive.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])
        error_msg = resp_inactive.json().get('error') or resp_inactive.json().get('detail') or ''
        self.assertIn("inactive", error_msg.lower())

        # Case B: Program disabled at target branch
        ProgramBranchAvailability.objects.using('tenant_test').update_or_create(
            program=self.prog1,
            branch=self.branch2,
            defaults={"is_active": False}
        )
        resp_prog = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/transfer/",
            {"branch_id": str(self.branch2.id)},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_prog.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not operationally active", resp_prog.json()['error'].lower())

        # Reset program availability
        ProgramBranchAvailability.objects.using('tenant_test').update_or_create(
            program=self.prog1, branch=self.branch2, defaults={"is_active": True}
        )

        # Case C: Package disabled at target branch
        PackageBranchAvailability.objects.using('tenant_test').update_or_create(
            package=self.pkg1,
            branch=self.branch2,
            defaults={"status": "DISABLED"}
        )
        resp_pkg = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/transfer/",
            {"branch_id": str(self.branch2.id)},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_pkg.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("disabled", resp_pkg.json()['error'].lower())

    def test_valid_transfer_branch_accepted(self):
        mem, _ = self._create_active_membership()

        # Ensure active availability
        ProgramBranchAvailability.objects.using('tenant_test').update_or_create(
            program=self.prog1, branch=self.branch2, defaults={"is_active": True}
        )
        PackageBranchAvailability.objects.using('tenant_test').update_or_create(
            package=self.pkg1, branch=self.branch2, defaults={"status": "ENABLED"}
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/transfer/",
            {"branch_id": str(self.branch2.id), "reason": "Relocated closer to Uptown"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['success'])

        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.home_branch_id, self.branch2.id)

        # Verify MembershipBranchHistory record
        hist = MembershipBranchHistory.objects.using('tenant_test').filter(membership=mem).order_by('-effective_at').first()
        self.assertIsNotNone(hist)
        self.assertEqual(hist.from_branch_id, self.branch1.id)
        self.assertEqual(hist.to_branch_id, self.branch2.id)

    # =========================================================================
    # 5. SENSITIVE HEALTH PERMISSIONS TEST
    # =========================================================================
    def test_health_answers_permission_protected(self):
        # Create an intake form with sensitive medical questions
        form, _ = IntakeForm.objects.using('tenant_test').get_or_create(
            organization=self.org,
            name="PAR-Q Medical Health Questionnaire",
            defaults={"form_type": "PAR_Q", "status": "ACTIVE"}
        )
        q1, _ = IntakeQuestion.objects.using('tenant_test').get_or_create(
            intake_form=form,
            question_text="Do you have a heart condition or high blood pressure?",
            defaults={"question_type": "BOOLEAN", "category": "MEDICAL", "is_sensitive": True, "is_required": True}
        )
        q2, _ = IntakeQuestion.objects.using('tenant_test').get_or_create(
            intake_form=form,
            question_text="Current medications",
            defaults={"question_type": "TEXT", "category": "MEDICAL", "is_sensitive": True, "is_required": False}
        )

        sub, _ = IntakeSubmission.objects.using('tenant_test').get_or_create(
            intake_form=form,
            user_profile=self.member_profile,
            defaults={"submitted_at": timezone.now()}
        )
        IntakeAnswer.objects.using('tenant_test').get_or_create(
            submission=sub, question=q1, defaults={"boolean_value": True}
        )
        IntakeAnswer.objects.using('tenant_test').get_or_create(
            submission=sub, question=q2, defaults={"text_value": "Beta blockers"}
        )

        # 1. Ordinary Sales Rep (has crm.leads.view & members.client-360.view, lacks cs.member-health.view)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.sales_token}")
        resp_sales = self.client.get(f"/api/v1/tenant/members/{self.member_profile.id}/360/")
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_sales.status_code, status.HTTP_200_OK)
        data_sales = resp_sales.json()

        health_sales = data_sales['health_and_forms']['submissions']
        self.assertEqual(len(health_sales), 1)
        sub_sales = health_sales[0]
        # Sensitive data MUST be restricted: answers redacted to []
        self.assertTrue(sub_sales.get('sensitive_data_restricted'))
        self.assertEqual(sub_sales['answers'], [])
        # Non-sensitive indicator remains intact
        self.assertEqual(sub_sales['status'], 'COMPLETED')
        self.assertEqual(sub_sales['form_title'], 'PAR-Q Medical Health Questionnaire')

        # 2. Authorized Health Admin (has cs.member-health.view via ORG_ADMIN wildcard)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        resp_admin = self.client.get(f"/api/v1/tenant/members/{self.member_profile.id}/360/")
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_admin.status_code, status.HTTP_200_OK)
        data_admin = resp_admin.json()

        health_admin = data_admin['health_and_forms']['submissions']
        self.assertEqual(len(health_admin), 1)
        sub_admin = health_admin[0]
        self.assertFalse(sub_admin.get('sensitive_data_restricted', False))
        self.assertEqual(len(sub_admin['answers']), 2)
        answers_dict = {a['question_text']: a['answer'] for a in sub_admin['answers']}
        self.assertEqual(answers_dict["Do you have a heart condition or high blood pressure?"], "True")
        self.assertEqual(answers_dict["Current medications"], "Beta blockers")

    # =========================================================================
    # 7. CASH RENEWAL REQUIRES APPROVAL (Action A - Gated)
    # =========================================================================
    def test_cash_renewal_requires_manager_approval(self):
        old_mem, _ = self._create_active_membership()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/renew/",
            {
                "package_id": str(self.pkg1.id),
                "payment_amount": 5000.00,
                "payment_method": "CASH",
                "payment_provider": "CASH",
                "reason": "Cash renewal front desk"
            },
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('submitted for manager approval', data['message'])

        # Verify PaymentTransaction is PENDING
        from apps.tenant_core.models_commerce import PaymentTransaction
        txn = PaymentTransaction.objects.using('tenant_test').filter(order_id=data['order_id']).first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.status, 'PENDING')
        self.assertEqual(txn.payment_method, 'CASH')

        # Verify ApprovalRequest created
        from apps.tenant_core.models_approvals import ApprovalRequest
        appr = ApprovalRequest.objects.using('tenant_test').filter(entity_id=txn.id).first()
        self.assertIsNotNone(appr)
        self.assertEqual(appr.request_type, 'CASH_PAYMENT_APPROVAL')
        self.assertEqual(appr.status, 'PENDING')

        # Verify provisional membership is PENDING_PAYMENT with INACTIVE entitlements
        prov_mem = Membership.objects.using('tenant_test').get(id=data['new_membership_id'])
        self.assertEqual(prov_mem.status, 'PENDING_PAYMENT')
        for ent in prov_mem.entitlements.using('tenant_test').all():
            self.assertEqual(ent.status, 'INACTIVE')

    # =========================================================================
    # 8. EXTEND VALIDITY (Action C)
    # =========================================================================
    def test_extend_validity_updates_dates_and_preserves_sessions(self):
        mem, _ = self._create_active_membership()
        initial_end = mem.end_date
        ent = mem.entitlements.using('tenant_test').first()
        initial_rem = ent.remaining_units

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/extend/",
            {"days": 14, "reason": "Medical extension verified"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['success'])

        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.end_date, initial_end + timedelta(days=14))

        # Sessions preserved, not reset
        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.remaining_units, initial_rem)

        # Status history logged
        hist = MembershipStatusHistory.objects.using('tenant_test').filter(
            membership=mem, reason_code='EXTENSION_APPLIED'
        ).first()
        self.assertIsNotNone(hist)

    # =========================================================================
    # 9. FREEZE & UNFREEZE (Action D)
    # =========================================================================
    def test_freeze_and_unfreeze_flow(self):
        mem, _ = self._create_active_membership()
        today = timezone.now().date()
        freeze_start = today
        freeze_end = today + timedelta(days=9)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        resp_freeze = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/freeze/",
            {
                "freeze_from": freeze_start.isoformat(),
                "freeze_until": freeze_end.isoformat(),
                "reason": "Travel out of country"
            },
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_freeze.status_code, status.HTTP_200_OK)

        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.status, 'FROZEN')

        # Unfreeze
        resp_unfreeze = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/unfreeze/",
            {"reason": "Early return from travel"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_unfreeze.status_code, status.HTTP_200_OK)

        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.status, 'ACTIVE')

    # =========================================================================
    # 10. CANCEL MEMBERSHIP & FORFEIT ENTITLEMENTS (Action F)
    # =========================================================================
    def test_cancel_membership_and_forfeits_entitlements(self):
        mem, _ = self._create_active_membership()
        ent = mem.entitlements.using('tenant_test').first()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/cancel/",
            {"reason": "Moving to a different city"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        mem.refresh_from_db(using='tenant_test')
        self.assertEqual(mem.status, 'CANCELLED')

        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.status, 'EXPIRED')

        # Forfeiture ledger entry appended
        expiry_ledger = MembershipEntitlementLedger.objects.using('tenant_test').filter(
            membership_entitlement=ent,
            transaction_type='EXPIRY',
            reason_code='MEMBERSHIP_CANCELLED'
        ).first()
        self.assertIsNotNone(expiry_ledger)

    # =========================================================================
    # 11. ADJUST SESSIONS BOUNDS & IDEMPOTENCY (Action G)
    # =========================================================================
    def test_adjust_sessions_bounds_and_idempotency(self):
        mem, _ = self._create_active_membership()
        ent = mem.entitlements.using('tenant_test').first()
        initial_alloc = ent.allocated_units

        # 1. Unauthorized actor -> 403
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.sales_token}")
        resp_unauth = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/adjust-entitlement/",
            {"entitlement_id": str(ent.id), "delta": 2, "reason": "Bonus sessions"},
            format='json'
        )
        self.assertEqual(resp_unauth.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Empty reason -> 400
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        resp_no_reason = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/adjust-entitlement/",
            {"entitlement_id": str(ent.id), "delta": 2, "reason": "  "},
            format='json'
        )
        self.assertEqual(resp_no_reason.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Valid adjustment with idempotency key
        idem_key = f"ADJ-TEST-{uuid.uuid4().hex[:8]}"
        resp_valid = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/adjust-entitlement/",
            {"entitlement_id": str(ent.id), "delta": 3, "reason": "Customer goodwill credit", "idempotency_key": idem_key},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_valid.status_code, status.HTTP_200_OK)
        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.allocated_units, initial_alloc + Decimal('3.00'))

        # 4. Replay with identical idempotency key does not double-adjust
        resp_replay = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/adjust-entitlement/",
            {"entitlement_id": str(ent.id), "delta": 3, "reason": "Customer goodwill credit", "idempotency_key": idem_key},
            format='json'
        )
        self.assertEqual(resp_replay.status_code, status.HTTP_200_OK)
        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.allocated_units, initial_alloc + Decimal('3.00'))

    # =========================================================================
    # 12. COLLECT OUTSTANDING DEBT & CASH APPROVAL (Action H)
    # =========================================================================
    def test_collect_outstanding_debt_and_cash_approval(self):
        mem, _ = self._create_active_membership()
        # Create an unpaid order of 2500
        order = Order.objects.using('tenant_test').create(
            order_number=f"ORD-TEST-{uuid.uuid4().hex[:6]}",
            user_profile=self.member_profile,
            branch=self.branch1,
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('2500.00'),
            total_amount=Decimal('2500.00'),
            currency='INR',
            status='PENDING_PAYMENT',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")

        # 1. Over-collection rejected (requesting 5000 on 2500 debt)
        resp_over = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/collect-outstanding/",
            {"order_id": str(order.id), "amount": 5000.00, "payment_method": "CASH"},
            format='json'
        )
        self.assertEqual(resp_over.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cannot exceed order outstanding balance', resp_over.json()['error'])

        # 2. Cash collection -> 202 ACCEPTED with ApprovalRequest
        resp_cash = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/collect-outstanding/",
            {"order_id": str(order.id), "amount": 1000.00, "payment_method": "CASH"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_cash.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn('submitted for manager approval', resp_cash.json()['message'])

        # 3. Partial Card collection (1500 of 2500) -> PARTIALLY_PAID (cash remains unsettled)
        resp_card1 = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/collect-outstanding/",
            {"order_id": str(order.id), "amount": 1500.00, "payment_method": "CARD"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_card1.status_code, status.HTTP_201_CREATED)
        order.refresh_from_db(using='tenant_test')
        self.assertEqual(order.status, 'PARTIALLY_PAID')

        # 4. Final Card collection (remaining 1000) -> PAID
        resp_card2 = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/collect-outstanding/",
            {"order_id": str(order.id), "amount": 1000.00, "payment_method": "CARD"},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_card2.status_code, status.HTTP_201_CREATED)
        order.refresh_from_db(using='tenant_test')
        self.assertEqual(order.status, 'PAID')

    # =========================================================================
    # 13. CHECK-IN: FACILITY VS CLASS & DEBOUNCE (Action I)
    # =========================================================================
    def test_check_in_facility_vs_class_and_debounce(self):
        mem, _ = self._create_active_membership()
        ent = mem.entitlements.using('tenant_test').first()
        initial_consumed = ent.consumed_units

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")

        # 1. Facility entry -> Does NOT consume sessions!
        resp_fac = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/check-in/",
            {"record_type": "FACILITY", "method": "FRONT_DESK", "branch_id": str(self.branch1.id)},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_fac.status_code, status.HTTP_201_CREATED)
        self.assertIn('Facility Entry', resp_fac.json()['message'])
        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.consumed_units, initial_consumed)

        # 2. Duplicate facility check-in within 15 min debounced
        resp_dup = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/check-in/",
            {"record_type": "FACILITY", "method": "FRONT_DESK", "branch_id": str(self.branch1.id)},
            format='json'
        )
        self.assertEqual(resp_dup.status_code, status.HTTP_200_OK)
        self.assertIn('already checked in', resp_dup.json()['message'])

        # 3. Class check-in -> Consumes 1 session unit!
        resp_class = self.client.post(
            f"/api/v1/tenant/members/{self.member_profile.id}/check-in/",
            {"record_type": "CLASS", "consume_session": True, "method": "FRONT_DESK", "branch_id": str(self.branch1.id)},
            format='json'
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp_class.status_code, status.HTTP_201_CREATED)
        self.assertIn('Class Session', resp_class.json()['message'])
        ent.refresh_from_db(using='tenant_test')
        self.assertEqual(ent.consumed_units, initial_consumed + Decimal('1.00'))

    # =========================================================================
    # 14. MEMBER 360 ALL SIX TABS PAYLOAD & WORKSPACES
    # =========================================================================
    def test_member_360_all_six_tabs_payload(self):
        mem, _ = self._create_active_membership()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        resp = self.client.get(f"/api/v1/tenant/members/{self.member_profile.id}/360/")
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Tab 1: Overview
        self.assertIn('overview', data)
        self.assertIn('contact', data['overview'])
        self.assertIn('entitlement_summary', data['overview'])
        self.assertIn('current_membership', data['overview'])

        # Tab 2: Timeline
        self.assertIn('timeline', data)
        self.assertIsInstance(data['timeline'], list)

        # Tab 3: Memberships & Passbook
        self.assertIn('memberships', data)
        self.assertIn('passbook', data)
        self.assertIn('balances', data['passbook'])
        self.assertIn('ledger', data['passbook'])

        # Tab 4: Bookings & Attendance
        self.assertIn('bookings_and_attendance', data)
        self.assertIn('upcoming_bookings', data['bookings_and_attendance'])
        self.assertIn('past_bookings', data['bookings_and_attendance'])
        self.assertIn('attendance_records', data['bookings_and_attendance'])

        # Tab 5: Finance
        self.assertIn('finance', data)
        self.assertIn('summary', data['finance'])
        self.assertIn('orders', data['finance'])
        self.assertIn('payments', data['finance'])

        # Tab 6: Health & Forms
        self.assertIn('health_and_forms', data)
        self.assertIn('submissions', data['health_and_forms'])

    def test_membership_branch_histories_endpoint(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        resp = self.client.get("/api/v1/tenant/membership-branch-histories/")
        set_tenant_db_alias('tenant_test')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn('results', data)
