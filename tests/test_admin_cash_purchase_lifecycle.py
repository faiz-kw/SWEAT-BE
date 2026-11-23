import uuid
from decimal import Decimal
from datetime import date
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.tenant_core.context import set_tenant_db_alias
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role, RoleAssignment,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from apps.tenant_core.models_commerce import Order, PaymentTransaction, MemberInvoice
from apps.tenant_core.models_memberships import Membership, MembershipEntitlement, MembershipEntitlementLedger
from apps.tenant_core.models_approvals import ApprovalRequest, ApprovalAction
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.services_crm import LeadConversionService
from apps.tenant_core.services_approvals import AdminApprovalService
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.services_payment_policy import PaymentPolicyService

DB = 'tenant_test'


class AdminCashPurchaseLifecycleTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        self.tenant = Tenant.objects.using('default').create(
            code=f'T-CASH-{uuid.uuid4().hex[:6]}', name='Cash Test Gym', slug=f'cash-gym-{uuid.uuid4().hex[:6]}', status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code=f'ENT-{uuid.uuid4().hex[:6]}', tier='ENTERPRISE', status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant, plan=plan, status='ACTIVE',
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Platform', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=self.prod_mod_core, is_enabled=True, availability_mode='ALL_BRANCHES',
        )

        self.org = Organization.objects.using(DB).create(
            code=f'ORG-{uuid.uuid4().hex[:6]}', name='Admin Cash Org', status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code=f'LOC-{uuid.uuid4().hex[:6]}', name='HQ Location', status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code=f'BR-{uuid.uuid4().hex[:6]}', name='Main Branch', status='ACTIVE',
        )

        # 1. Admin user (Authorized approver / org admin)
        self.admin_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='admin@gym.test',
            first_name='Admin', last_name='Owner', status='ACTIVE',
            user_type='ADMIN',
        )
        self.admin_user.set_password('AdminPass123!')
        self.admin_user.save(using=DB)

        # 2. Staff user (Front desk cashier)
        self.staff_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='cashier@gym.test',
            first_name='Cashier', last_name='Sam', status='ACTIVE',
        )
        self.staff_user.set_password('StaffPass123!')
        self.staff_user.save(using=DB)

        # RBAC Roles
        self.role_admin = Role.objects.using(DB).create(
            name='Org Admin', code='ORG_ADMIN', is_system_role=True,
            scope='ORG', organization=self.org, is_active=True,
        )
        self.role_staff = Role.objects.using(DB).create(
            name='Cashier Staff', code='CASHIER', is_system_role=False,
            scope='BRANCH', organization=self.org, is_active=True,
        )

        RoleAssignment.objects.using(DB).create(
            user=self.admin_user, role=self.role_admin, organization=self.org, is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.staff_user, role=self.role_staff, organization=self.org, branch=self.branch, is_active=True,
        )

        # Permissions setup for both roles
        mod_core, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core System', 'is_enabled': True},
        )
        sub_settings, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True},
        )
        sub_users, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True},
        )
        mod_crm, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM Module', 'is_enabled': True},
        )
        sub_leads, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_crm, submodule_code='leads', defaults={'name': 'Leads Submodule', 'is_enabled': True},
        )
        mod_mem, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='memberships', defaults={'name': 'Memberships Module', 'is_enabled': True},
        )
        sub_dir, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_mem, submodule_code='directory', defaults={'name': 'Member Directory', 'is_enabled': True},
        )
        mod_fin, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='finance', defaults={'name': 'Finance Module', 'is_enabled': True},
        )
        sub_pay, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_fin, submodule_code='payments', defaults={'name': 'Payments', 'is_enabled': True},
        )

        for role in [self.role_admin, self.role_staff]:
            for mod in [mod_core, mod_crm, mod_mem, mod_fin]:
                RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod, defaults={'can_access': True})
            for sub in [sub_settings, sub_users, sub_leads, sub_dir, sub_pay]:
                RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub, defaults={'can_access': True})

        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        perm_users_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_users, action='view',
            defaults={'permission_code': 'core.users.view', 'label': 'View Users'},
        )
        perm_users_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_users, action='edit',
            defaults={'permission_code': 'core.users.edit', 'label': 'Edit Users'},
        )
        perm_convert, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='convert',
            defaults={'permission_code': 'crm.leads.convert', 'label': 'Convert Lead'},
        )
        perm_edit_mem, _ = Permission.objects.using(DB).get_or_create(
            module=mod_mem, submodule=sub_dir, action='edit',
            defaults={'permission_code': 'memberships.directory.edit', 'label': 'Edit Member'},
        )
        perm_pay, _ = Permission.objects.using(DB).get_or_create(
            module=mod_fin, submodule=sub_pay, action='create',
            defaults={'permission_code': 'finance.payments.create', 'label': 'Create Payment'},
        )

        for role in [self.role_admin, self.role_staff]:
            perm_set = RolePermissionSet.objects.using(DB).create(role=role, name=f'{role.name} Perm Set')
            for p in [perm_view, perm_users_view, perm_users_edit, perm_convert, perm_edit_mem, perm_pay]:
                RolePermissionSetItem.objects.using(DB).create(permission_set=perm_set, permission=p, granted=True)

        # Catalog setup
        self.program = Program.objects.using(DB).create(
            organization=self.org, name='SWEAT Strength', code='SW-STR', status='ACTIVE',
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, name='10 Pack Strength', code='SW-10', status='ACTIVE',
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='10 Pack Strength v1', status='ACTIVE',
            duration_value=60, duration_unit='DAY', effective_from=timezone.now(), created_by_user=self.admin_user,
        )
        self.package_price = PackagePrice.objects.using(DB).create(
            package_version=self.package_version,
            currency='INR',
            base_price=Decimal('10000.00'),
            tax_percent=Decimal('18.00'),
            prices_include_tax=False,
            effective_from=timezone.now(),
            created_by_user=self.admin_user,
            status='ACTIVE',
        )
        self.ent_def = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.package_version,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('10.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

    def _auth(self, user):
        refresh = _build_tenant_token(user=user, tenant=self.tenant, db_alias=DB)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def test_admin_cash_lead_conversion_activates_directly(self):
        """
        Admin converting a lead with Cash:
        - Must directly activate without asking for approval
        - Must NOT cap entitlements to provisional 4 sessions (gets full 10 sessions)
        - Must NOT create an ApprovalRequest
        - Order status must be PAID
        - MemberInvoice must be generated
        - Membership status must be ACTIVE with legacy_reference = 'CASH_VERIFIED'
        """
        set_tenant_db_alias(DB)
        lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Rahul',
            last_name='Mehta',
            email_normalized='rahul@test.com',
            phone_normalized='+919876543201',
            current_status='INTERESTED',
        )

        res = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('11800.00'),
            actor_user=self.admin_user,
            db_alias=DB,
        )

        # 1. Conversion Result
        self.assertEqual(res['lead_status'], 'CONVERTED')
        self.assertNotIn('approval_request_id', res)
        self.assertNotIn('provisional_sessions', res)

        # 2. No ApprovalRequest created
        self.assertFalse(ApprovalRequest.objects.using(DB).filter(requested_by_user=self.admin_user).exists())

        # 3. Order is PAID and MemberInvoice exists
        order = Order.objects.using(DB).get(id=res['order_id'])
        self.assertEqual(order.status, 'PAID')
        invoice = MemberInvoice.objects.using(DB).get(order=order)
        self.assertEqual(invoice.status, 'PAID')
        self.assertEqual(invoice.total_amount, Decimal('11800.00'))

        # 4. Membership is ACTIVE with full entitlements and clean legacy_reference
        membership = Membership.objects.using(DB).get(id=res['membership_id'])
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertEqual(membership.legacy_reference, 'CASH_VERIFIED')

        ent = MembershipEntitlement.objects.using(DB).filter(membership=membership).first()
        self.assertEqual(ent.status, 'ACTIVE')
        self.assertEqual(ent.allocated_units, Decimal('10.00'))  # Full 10 sessions, NOT 4!
        self.assertEqual(ent.remaining_units, Decimal('10.00'))

    def test_staff_cash_lead_conversion_routes_to_approval(self):
        """
        Staff (cashier) converting a lead with Cash:
        - Maker-checker segregation of duties enforced
        - Returns PENDING_APPROVAL with provisional 4 sessions
        - Creates ApprovalRequest
        """
        set_tenant_db_alias(DB)
        lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Pooja',
            last_name='Nair',
            email_normalized='pooja@test.com',
            phone_normalized='+919876543202',
            current_status='INTERESTED',
        )

        res = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('11800.00'),
            actor_user=self.staff_user,
            db_alias=DB,
        )

        self.assertEqual(res['status'], 'PENDING_APPROVAL')
        self.assertEqual(res['provisional_sessions'], 4)
        self.assertIn('approval_request_id', res)

        membership = Membership.objects.using(DB).get(id=res['membership_id'])
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertIn('PROVISIONAL_CASH_PENDING:limit=4', membership.legacy_reference)
        ent = MembershipEntitlement.objects.using(DB).filter(membership=membership).first()
        self.assertEqual(ent.allocated_units, Decimal('4.00'))

    def test_admin_cash_purchase_package_endpoint(self):
        """
        Admin purchasing a package for an existing member via POST /purchase-package/:
        - Direct activation with 201 CREATED
        - Order status PAID
        - MemberInvoice generated
        - Membership ACTIVE with full entitlements
        """
        set_tenant_db_alias(DB)
        member_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='member1@gym.test',
            first_name='Sunil', last_name='Patel', status='ACTIVE',
        )
        profile = UserProfile.objects.using(DB).create(
            user=member_user, preferred_branch=self.branch,
            first_name_snapshot='Sunil', last_name_snapshot='Patel',
            member_number='MEM-101',
            member_status='ACTIVE',
        )

        self._auth(self.admin_user)
        resp = self.client.post(f'/api/v1/tenant/members/{profile.id}/purchase-package/', {
            'package_id': str(self.package.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_method': 'CASH',
            'payment_amount': '11800.00',
            'start_schedule': 'FIRST_VISIT',
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertTrue(data['success'])
        new_membership_id = data['new_membership_id']

        set_tenant_db_alias(DB)
        membership = Membership.objects.using(DB).get(id=new_membership_id)
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertEqual(membership.legacy_reference, 'CASH_VERIFIED')

        order = Order.objects.using(DB).get(id=data['order_id'])
        self.assertEqual(order.status, 'PAID')
        self.assertTrue(MemberInvoice.objects.using(DB).filter(order=order).exists())

        ent = MembershipEntitlement.objects.using(DB).filter(membership=membership).first()
        self.assertEqual(ent.status, 'ACTIVE')
        self.assertEqual(ent.allocated_units, Decimal('10.00'))

    def test_staff_cash_purchase_package_requires_approval(self):
        """
        Staff (cashier) purchasing a package for a member via Cash:
        - Segregation of duties enforced: returns 202 ACCEPTED with PENDING_APPROVAL
        - ApprovalRequest created
        - Membership status PENDING_PAYMENT
        - Entitlements INACTIVE
        """
        set_tenant_db_alias(DB)
        member_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='member2@gym.test',
            first_name='Ananya', last_name='Roy', status='ACTIVE',
        )
        profile = UserProfile.objects.using(DB).create(
            user=member_user, preferred_branch=self.branch,
            first_name_snapshot='Ananya', last_name_snapshot='Roy',
            member_number='MEM-102',
            member_status='ACTIVE',
        )

        self._auth(self.staff_user)
        resp = self.client.post(f'/api/v1/tenant/members/{profile.id}/purchase-package/', {
            'package_id': str(self.package.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_method': 'CASH',
            'payment_amount': '11800.00',
            'start_schedule': 'FIRST_VISIT',
        })

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        data = resp.json()
        self.assertEqual(data['status'], 'PENDING_APPROVAL')
        self.assertIn('approval_request_id', data)

        set_tenant_db_alias(DB)
        membership = Membership.objects.using(DB).get(id=data['new_membership_id'])
        self.assertEqual(membership.status, 'PENDING_PAYMENT')
        self.assertIn('CASH_APPROVAL:', membership.legacy_reference)
        ent = MembershipEntitlement.objects.using(DB).filter(membership=membership).first()
        self.assertEqual(ent.status, 'INACTIVE')

        # When Admin approves this request:
        req = ApprovalRequest.objects.using(DB).get(id=data['approval_request_id'])
        AdminApprovalService.process_action(
            approval_request=req,
            action='APPROVED',
            approver_user=self.admin_user,
            comment='Verified cash received in till',
            db_alias=DB,
        )

        membership.refresh_from_db(using=DB)
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertEqual(membership.legacy_reference, 'CASH_APPROVED')
        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.status, 'ACTIVE')

    def test_admin_cash_renewal_activates_directly(self):
        """
        Admin renewing an existing member's plan via Cash:
        - Must directly activate with 201 CREATED
        - Must NOT require manager approval
        - Membership status ACTIVE
        - Order PAID
        """
        set_tenant_db_alias(DB)
        member_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='member3@gym.test',
            first_name='Karan', last_name='Kapoor', status='ACTIVE',
        )
        profile = UserProfile.objects.using(DB).create(
            user=member_user, preferred_branch=self.branch,
            first_name_snapshot='Karan', last_name_snapshot='Kapoor',
            member_number='MEM-103',
            member_status='ACTIVE',
        )
        old_m = Membership.objects.using(DB).create(
            user_profile=profile,
            package=self.package,
            package_version=self.package_version,
            home_branch=self.branch,
            purchase_branch=self.branch,
            status='EXPIRED',
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 1),
        )

        self._auth(self.admin_user)
        resp = self.client.post(f'/api/v1/tenant/members/{profile.id}/renew/', {
            'membership_id': str(old_m.id),
            'package_id': str(self.package.id),
            'start_schedule': 'CONTINUOUS',
            'payment_provider': 'CASH',
            'payment_method': 'CASH',
            'payment_amount': '11800.00',
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertTrue(data['success'])

        set_tenant_db_alias(DB)
        new_membership = Membership.objects.using(DB).get(id=data['new_membership_id'])
        self.assertEqual(new_membership.status, 'ACTIVE')
        self.assertEqual(new_membership.legacy_reference, 'CASH_VERIFIED')
        ent = MembershipEntitlement.objects.using(DB).filter(membership=new_membership).first()
        self.assertEqual(ent.status, 'ACTIVE')
