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

class CashProvisionalWorkflowTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        self.tenant = Tenant.objects.using('default').create(
            code='CASH-TENANT', name='Cash Test Gym', slug='cash-gym', status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code='ENT-CASH', tier='ENTERPRISE', status='ACTIVE',
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
            code='CASH-ORG', name='Cash Org', status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='CASH-LOC', name='Cash Location', status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-CASH', name='Cash Branch Bandra', status='ACTIVE',
        )

        self.recorder = TenantUser.objects.using(DB).create(
            organization=self.org, email='cashier@cashgym.test',
            first_name='Cashier', last_name='Alice', status='ACTIVE',
        )
        self.recorder.set_password('Secret123!')
        self.recorder.save(using=DB)
        self.approver = TenantUser.objects.using(DB).create(
            organization=self.org, email='manager@cashgym.test',
            first_name='Manager', last_name='Bob', status='ACTIVE',
        )
        self.approver.set_password('Secret123!')
        self.approver.save(using=DB)

        # RBAC Setup
        self.role_admin = Role.objects.using(DB).create(
            name='Org Admin', code='ORG_ADMIN', is_system_role=True,
            scope='ORG', organization=self.org, is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.recorder, role=self.role_admin, organization=self.org, is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.approver, role=self.role_admin, organization=self.org, is_active=True,
        )

        mod_core, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core System', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, module=mod_core, defaults={'can_access': True}
        )
        sub_settings, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, submodule=sub_settings, defaults={'can_access': True}
        )

        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        perm_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'},
        )

        mod_fin, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='finance', defaults={'name': 'Finance Module', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, module=mod_fin, defaults={'can_access': True}
        )
        sub_pay, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_fin, submodule_code='payments', defaults={'name': 'Payments Submodule', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, submodule=sub_pay, defaults={'can_access': True}
        )
        perm_pay, _ = Permission.objects.using(DB).get_or_create(
            module=mod_fin, submodule=sub_pay, action='create',
            defaults={'permission_code': 'finance.payments.create', 'label': 'Create Payment'},
        )

        mod_crm, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM Module', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, module=mod_crm, defaults={'can_access': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_crm, submodule_code='leads', defaults={'name': 'Leads Submodule', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, submodule=sub_leads, defaults={'can_access': True}
        )
        perm_convert, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='convert',
            defaults={'permission_code': 'crm.leads.convert', 'label': 'Convert Lead'},
        )

        perm_set = RolePermissionSet.objects.using(DB).create(
            role=self.role_admin, name='Admin Perm Set',
        )
        for perm in [perm_view, perm_edit, perm_pay, perm_convert]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=perm_set, permission=perm, defaults={'granted': True}
            )

        self.program = Program.objects.using(DB).create(
            organization=self.org, name='SWEAT Pilates', code='SW-PIL', status='ACTIVE',
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, name='12 Sessions Monthly', code='SW-12', status='ACTIVE',
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='12 Sessions Monthly v1', status='ACTIVE',
            duration_value=30, duration_unit='DAY', effective_from=timezone.now(), created_by_user=self.recorder,
        )
        self.package_price = PackagePrice.objects.using(DB).create(
            package_version=self.package_version,
            currency='INR',
            base_price=Decimal('12000.00'),
            tax_percent=Decimal('18.00'),
            prices_include_tax=False,
            effective_from=timezone.now(),
            created_by_user=self.recorder,
            status='ACTIVE',
        )
        self.ent_def = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.package_version,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('12.00'),
            is_unlimited=False,
            status='ACTIVE',
        )

    def test_cash_conversion_provisional_membership_flow(self):
        """
        Verify:
        1. Staff Cash creates PENDING PaymentTransaction
        2. ApprovalRequest created with receiving agent & branch
        3. Membership created in provisional state
        4. Configured provisional session limit (4) applied
        5. Member can consume sessions up to provisional limit
        6. Reaching limit blocks further sessions with PAYMENT_APPROVAL_PENDING_LIMIT_REACHED
        7. Self-approval is rejected (segregation of duties)
        8. Authorized manager approval succeeds
        9. Full 12 sessions restored upon approval
        10. Finance recognizes cash revenue after approval
        """
        lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Kavita',
            last_name='Sharma',
            email_normalized='kavita@test.com',
            phone_normalized='+919876543210',
            current_status='INTERESTED',
        )

        res = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('14160.00'),
            actor_user=self.recorder,
            db_alias=DB,
        )

        self.assertEqual(res['status'], 'PENDING_APPROVAL')
        self.assertEqual(res['provisional_sessions'], 4)
        self.assertIn('membership_id', res)
        self.assertIn('approval_request_id', res)

        # 1. Check Transaction
        txn = PaymentTransaction.objects.using(DB).get(id=res['transaction_id'])
        self.assertEqual(txn.status, 'PENDING')
        self.assertEqual(txn.metadata['cash_recorded_by'], str(self.recorder.id))
        self.assertEqual(txn.metadata['branch_id'], str(self.branch.id))

        # 2. Check Membership & Capped Entitlements
        membership = Membership.objects.using(DB).get(id=res['membership_id'])
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertIn('PROVISIONAL_CASH_PENDING:limit=4', membership.legacy_reference)

        ent = membership.entitlements.using(DB).first()
        self.assertEqual(ent.allocated_units, Decimal('4.00'))
        self.assertEqual(ent.consumed_units, Decimal('0.00'))

        # 3. Consume 3 sessions (allowed)
        for _ in range(3):
            MembershipLifecycleService.consume_entitlement(
                membership=membership,
                entitlement_type='HOME_BRANCH_SESSION',
                units=Decimal('1.00'),
                db_alias=DB,
            )

        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.consumed_units, Decimal('3.00'))
        self.assertEqual(ent.remaining_units, Decimal('1.00'))

        # 4. Consume 4th session (reaches limit)
        MembershipLifecycleService.consume_entitlement(
            membership=membership,
            entitlement_type='HOME_BRANCH_SESSION',
            units=Decimal('1.00'),
            db_alias=DB,
        )
        membership.refresh_from_db(using=DB)
        self.assertIn('PROVISIONAL_CASH_LIMIT_REACHED', membership.legacy_reference)

        # 5. 5th session attempt must be BLOCKED with PAYMENT_APPROVAL_PENDING_LIMIT_REACHED
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            MembershipLifecycleService.consume_entitlement(
                membership=membership,
                entitlement_type='HOME_BRANCH_SESSION',
                units=Decimal('1.00'),
                db_alias=DB,
            )
        self.assertIn('PAYMENT_APPROVAL_PENDING_LIMIT_REACHED', str(ctx.exception))

        # 6. Self-approval must be BLOCKED
        req = ApprovalRequest.objects.using(DB).get(id=res['approval_request_id'])
        with self.assertRaises(ValidationError) as ctx_self:
            AdminApprovalService.process_action(
                approval_request=req,
                action='APPROVED',
                approver_user=self.recorder,  # Recorder trying to self-approve!
                db_alias=DB,
            )
        self.assertIn('Segregation of duties', str(ctx_self.exception))

        # 7. Rejection without comment must be BLOCKED
        with self.assertRaises(ValidationError) as ctx_rej:
            AdminApprovalService.process_action(
                approval_request=req,
                action='REJECTED',
                approver_user=self.approver,
                comment='',  # Empty rejection reason!
                db_alias=DB,
            )
        self.assertIn('Rejection reason is required', str(ctx_rej.exception))

        # 8. Authorized Manager Approval succeeds
        AdminApprovalService.process_action(
            approval_request=req,
            action='APPROVED',
            approver_user=self.approver,
            comment='Verified cash received in register',
            db_alias=DB,
        )

        # 9. Verify Post-Approval state
        txn.refresh_from_db(using=DB)
        self.assertEqual(txn.status, 'SUCCESS')
        self.assertEqual(txn.metadata['approval_status'], 'APPROVED')

        membership.refresh_from_db(using=DB)
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertEqual(membership.legacy_reference, 'CASH_APPROVED')

        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.allocated_units, Decimal('12.00'))  # Full 12 sessions restored!
        self.assertEqual(ent.consumed_units, Decimal('4.00'))  # 4 provisional consumed preserved!
        self.assertEqual(ent.remaining_units, Decimal('8.00'))  # 8 remaining!

        # 10. Can now consume 5th session successfully!
        MembershipLifecycleService.consume_entitlement(
            membership=membership,
            entitlement_type='HOME_BRANCH_SESSION',
            units=Decimal('1.00'),
            db_alias=DB,
        )
        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.consumed_units, Decimal('5.00'))
        self.assertEqual(ent.remaining_units, Decimal('7.00'))

    def test_cash_rejection_cancels_provisional_membership(self):
        """Rejection cancels membership, retains consumed ledger for audit, and excludes from revenue"""
        lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Rohan',
            last_name='Verma',
            email_normalized='rohan@test.com',
            phone_normalized='+919876543211',
            current_status='INTERESTED',
        )

        res = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('14160.00'),
            actor_user=self.recorder,
            db_alias=DB,
        )

        req = ApprovalRequest.objects.using(DB).get(id=res['approval_request_id'])
        AdminApprovalService.process_action(
            approval_request=req,
            action='REJECTED',
            approver_user=self.approver,
            comment='Cash discrepancy in till',
            db_alias=DB,
        )

        membership = Membership.objects.using(DB).get(id=res['membership_id'])
        self.assertEqual(membership.status, 'CANCELLED')
        self.assertIn('CASH_REJECTED', membership.legacy_reference)

        txn = PaymentTransaction.objects.using(DB).get(id=res['transaction_id'])
        self.assertEqual(txn.status, 'CANCELLED')
        self.assertEqual(txn.metadata['approval_status'], 'REJECTED')
        self.assertEqual(txn.metadata['rejection_reason'], 'Cash discrepancy in till')

    def test_cash_report_endpoint(self):
        """Cash report API endpoint aggregates physical, approved, pending, rejected by branch & agent"""
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-REP-1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('5000.00'),
            total_amount=Decimal('5000.00'),
            currency='INR',
            status='SETTLED',
        )
        PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('5000.00'),
            currency='INR',
            provider='CASH',
            status='SUCCESS',
            metadata={'cash_recorded_by': str(self.recorder.id), 'recorded_by_name': 'Cashier Alice'},
        )
        PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('2000.00'),
            currency='INR',
            provider='CASH',
            status='PENDING',
            metadata={'cash_recorded_by': str(self.recorder.id), 'recorded_by_name': 'Cashier Alice'},
        )

        refresh = _build_tenant_token(user=self.approver, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        resp = self.client.get(f'/api/v1/tenant/payment-transactions/cash-report/?branch_id={self.branch.id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(Decimal(data['total_physical_cash_recorded']), Decimal('7000.00'))
        self.assertEqual(Decimal(data['approved_cash']), Decimal('5000.00'))
        self.assertEqual(Decimal(data['pending_approval_cash']), Decimal('2000.00'))
        self.assertEqual(len(data['agent_collections']), 1)
        self.assertEqual(data['agent_collections'][0]['agent_name'], 'Cashier Alice')
