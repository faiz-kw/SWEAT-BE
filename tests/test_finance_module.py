import uuid
from decimal import Decimal
from datetime import date
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role, RoleAssignment,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice, Refund
from apps.tenant_core.models_approvals import ApprovalRequest

DB = 'tenant_test'

class FinanceModuleTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # 1. Master DB setup
        self.tenant = Tenant.objects.using('default').create(
            code='FIN-TENANT', name='Finance Test Gym', slug='fin-gym', status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code='ENT-PLAN', tier='ENTERPRISE', status='ACTIVE',
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

        # 2. Tenant DB: Org, Branch, Admin User
        self.org = Organization.objects.using(DB).create(
            code='FIN-ORG', name='Finance Org', status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='FIN-LOC', name='Main Location', status='ACTIVE',
        )
        self.branch1 = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-FIN-1', name='Branch Downtown', status='ACTIVE',
        )
        self.branch2 = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-FIN-2', name='Branch Uptown', status='ACTIVE',
        )

        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org, email='finance@testgym.com',
            first_name='Finance', last_name='Director', status='ACTIVE',
        )
        self.admin.set_password('Secret123!')
        self.admin.save(using=DB)

        # 3. RBAC setup for core module & settings submodule
        self.role_admin = Role.objects.using(DB).create(
            name='Org Admin', code='ORG_ADMIN', is_system_role=True,
            scope='ORG', organization=self.org, is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.admin, role=self.role_admin, organization=self.org, is_active=True,
        )

        self.mod_cat, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core System', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, module=self.mod_cat, defaults={'can_access': True}
        )

        self.sub_settings, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=self.mod_cat, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=self.role_admin, submodule=self.sub_settings, defaults={'can_access': True}
        )

        self.perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=self.mod_cat, submodule=self.sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        self.perm_edit, _ = Permission.objects.using(DB).get_or_create(
            module=self.mod_cat, submodule=self.sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'},
        )

        self.perm_set = RolePermissionSet.objects.using(DB).create(
            role=self.role_admin, name='Admin Perm Set',
        )
        RolePermissionSetItem.objects.using(DB).get_or_create(
            permission_set=self.perm_set, permission=self.perm_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using(DB).get_or_create(
            permission_set=self.perm_set, permission=self.perm_edit, defaults={'granted': True}
        )

        # 4. Auth token
        refresh = _build_tenant_token(
            user=self.admin,
            tenant=self.tenant,
            db_alias=DB,
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_finance_summary_kpi_aggregate(self):
        """Test authoritative KPI summary calculations from /api/v1/tenant/orders/summary/"""
        # Order 1: Branch 1, Settled (Total 10,000, Paid 10,000 via Razorpay)
        order1 = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-001',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            currency='INR',
            status='SETTLED',
        )
        PaymentTransaction.objects.using(DB).create(
            order=order1,
            amount=Decimal('10000.00'),
            currency='INR',
            provider='RAZORPAY',
            status='SUCCESS',
            provider_transaction_id='pay_1001',
        )

        # Order 2: Branch 1, Partially Paid (Total 5,000, Paid 2,000 via Cash, Due 3,000)
        order2 = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-002',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('5000.00'),
            total_amount=Decimal('5000.00'),
            currency='INR',
            status='PARTIALLY_PAID',
        )
        PaymentTransaction.objects.using(DB).create(
            order=order2,
            amount=Decimal('2000.00'),
            currency='INR',
            provider='CASH',
            status='SUCCESS',
            provider_transaction_id='cash_ref_1002',
        )

        # Order 3: Branch 1, Pending Payment (Total 4,000, Paid 0)
        Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-003',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('4000.00'),
            total_amount=Decimal('4000.00'),
            currency='INR',
            status='PENDING_PAYMENT',
        )

        url = '/api/v1/tenant/orders/summary/'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Check total orders count
        self.assertEqual(data['total_orders'], 3)
        self.assertEqual(data['settled_orders'], 1)
        # Pending orders should include both PENDING_PAYMENT and PARTIALLY_PAID
        self.assertEqual(data['pending_orders'], 2)

        # Monetary aggregates
        self.assertEqual(Decimal(str(data['total_settled_amount'])), Decimal('12000.00'))
        # Total Outstanding: (5000 - 2000) + 4000 = 7000.00
        self.assertEqual(Decimal(str(data['total_outstanding_amount'])), Decimal('7000.00'))
        # Gross revenue: 10000 (online) + 2000 (cash) = 12000.00
        self.assertEqual(Decimal(str(data['gross_revenue'])), Decimal('12000.00'))
        self.assertEqual(Decimal(str(data['online_collected'])), Decimal('10000.00'))
        self.assertEqual(Decimal(str(data['cash_collected'])), Decimal('2000.00'))
        self.assertEqual(Decimal(str(data['net_revenue'])), Decimal('12000.00'))

    def test_cash_approval_lifecycle(self):
        """Cash pending approval does not count towards revenue until approved"""
        order = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-CASH-1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('8000.00'),
            total_amount=Decimal('8000.00'),
            currency='INR',
            status='PENDING_PAYMENT',
        )

        txn = PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('8000.00'),
            currency='INR',
            provider='CASH',
            status='PENDING',
            provider_transaction_id='cash_audit_001',
        )

        app_req = ApprovalRequest.objects.using(DB).create(
            organization=self.org,
            request_type='CASH_PAYMENT',
            entity_type='PAYMENT_TRANSACTION',
            entity_id=txn.id,
            requested_by_user=self.admin,
            status='PENDING',
        )

        # Before approval:
        resp = self.client.get('/api/v1/tenant/orders/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(Decimal(str(data['gross_revenue'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(data['cash_collected'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(data['pending_approval_cash_amount'])), Decimal('8000.00'))

        # Approve the cash payment
        app_req.status = 'APPROVED'
        app_req.resolved_at = timezone.now()
        app_req.save(using=DB)

        txn.status = 'SUCCESS'
        txn.save(using=DB)

        order.status = 'SETTLED'
        order.save(using=DB)

        # After approval:
        resp = self.client.get('/api/v1/tenant/orders/summary/')
        data = resp.json()
        self.assertEqual(Decimal(str(data['gross_revenue'])), Decimal('8000.00'))
        self.assertEqual(Decimal(str(data['cash_collected'])), Decimal('8000.00'))
        self.assertEqual(Decimal(str(data['pending_approval_cash_amount'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(data['total_outstanding_amount'])), Decimal('0.00'))

    def test_rejected_cash_does_not_count(self):
        """Cash rejected retains audit history but does not contribute to revenue or balance"""
        order = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-REJ-1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('3000.00'),
            total_amount=Decimal('3000.00'),
            currency='INR',
            status='PENDING_PAYMENT',
        )
        txn = PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('3000.00'),
            currency='INR',
            provider='CASH',
            status='FAILED',
            provider_transaction_id='cash_rej_001',
        )
        ApprovalRequest.objects.using(DB).create(
            organization=self.org,
            request_type='CASH_PAYMENT',
            entity_type='PAYMENT_TRANSACTION',
            entity_id=txn.id,
            requested_by_user=self.admin,
            status='REJECTED',
        )

        resp = self.client.get('/api/v1/tenant/orders/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(Decimal(str(data['gross_revenue'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(data['cash_collected'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(data['total_outstanding_amount'])), Decimal('3000.00'))

    def test_partial_razorpay_installments(self):
        """One order with multiple transactions: 1 success, 1 failure, 1 final success"""
        order = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-RZP-PARTIAL',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('15000.00'),
            total_amount=Decimal('15000.00'),
            currency='INR',
            status='PARTIALLY_PAID',
        )

        # 1. Partial success: 5000
        PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('5000.00'),
            currency='INR',
            provider='RAZORPAY',
            status='SUCCESS',
            provider_transaction_id='pay_part_1',
        )

        # 2. Failed attempt: 5000
        PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('5000.00'),
            currency='INR',
            provider='RAZORPAY',
            status='FAILED',
            provider_transaction_id='pay_part_failed',
        )

        # Check order detail via API
        resp = self.client.get(f'/api/v1/tenant/orders/{order.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order_data = resp.json()
        self.assertEqual(Decimal(str(order_data['paid_amount'])), Decimal('5000.00'))
        self.assertEqual(Decimal(str(order_data['outstanding_balance'])), Decimal('10000.00'))

        # 3. Final success: 10,000
        set_tenant_db_alias(DB)
        PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('10000.00'),
            currency='INR',
            provider='RAZORPAY',
            status='SUCCESS',
            provider_transaction_id='pay_part_2',
        )
        order.status = 'SETTLED'
        order.save(using=DB)

        # Check order detail again
        resp = self.client.get(f'/api/v1/tenant/orders/{order.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order_data = resp.json()
        self.assertEqual(Decimal(str(order_data['paid_amount'])), Decimal('15000.00'))
        self.assertEqual(Decimal(str(order_data['outstanding_balance'])), Decimal('0.00'))
        self.assertEqual(order_data['status'], 'SETTLED')

    def test_refund_financial_deduction(self):
        """Processed refund reduces net revenue"""
        order = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-REF-1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('6000.00'),
            total_amount=Decimal('6000.00'),
            currency='INR',
            status='SETTLED',
        )
        txn = PaymentTransaction.objects.using(DB).create(
            order=order,
            amount=Decimal('6000.00'),
            currency='INR',
            provider='RAZORPAY',
            status='SUCCESS',
            provider_transaction_id='pay_ref_base',
        )

        Refund.objects.using(DB).create(
            order=order,
            payment_transaction=txn,
            amount=Decimal('2000.00'),
            reason_code='CANCELLED',
            reason_text='Customer requested partial cancellation',
            status='SUCCESS',
        )

        resp = self.client.get('/api/v1/tenant/orders/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(Decimal(str(data['gross_revenue'])), Decimal('6000.00'))
        self.assertEqual(Decimal(str(data['refunded_amount'])), Decimal('2000.00'))
        self.assertEqual(Decimal(str(data['net_revenue'])), Decimal('4000.00'))

    def test_branch_scoping(self):
        """Branch filtering restricts summary and orders to the specified branch"""
        Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-B1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('5000.00'),
            total_amount=Decimal('5000.00'),
            currency='INR',
            status='SETTLED',
        )
        Order.objects.using(DB).create(
            branch=self.branch2,
            order_number='ORD-B2',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('3000.00'),
            total_amount=Decimal('3000.00'),
            currency='INR',
            status='SETTLED',
        )

        resp = self.client.get('/api/v1/tenant/orders/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['total_orders'], 2)

        resp_b1 = self.client.get(f'/api/v1/tenant/orders/summary/?branch_id={self.branch1.id}')
        self.assertEqual(resp_b1.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_b1.json()['total_orders'], 1)

        resp_b2 = self.client.get(f'/api/v1/tenant/orders/summary/?branch_id={self.branch2.id}')
        self.assertEqual(resp_b2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_b2.json()['total_orders'], 1)

    def test_invoice_snapshot_immutability(self):
        """MemberInvoice snapshots immutable monetary amounts from order"""
        order = Order.objects.using(DB).create(
            branch=self.branch1,
            order_number='ORD-INV-1',
            order_type='NEW_MEMBERSHIP',
            subtotal=Decimal('12000.00'),
            discount_amount=Decimal('2000.00'),
            tax_amount=Decimal('1800.00'),
            total_amount=Decimal('11800.00'),
            currency='INR',
            status='SETTLED',
        )

        inv = MemberInvoice.objects.using(DB).create(
            order=order,
            branch=self.branch1,
            invoice_number='INV-2026-0001',
            subtotal=Decimal('12000.00'),
            discount_amount=Decimal('2000.00'),
            tax_amount=Decimal('1800.00'),
            total_amount=Decimal('11800.00'),
            currency='INR',
            status='PAID',
        )

        resp = self.client.get(f'/api/v1/tenant/member-invoices/{inv.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(Decimal(str(data['subtotal'])), Decimal('12000.00'))
        self.assertEqual(Decimal(str(data['discount_amount'])), Decimal('2000.00'))
        self.assertEqual(Decimal(str(data['tax_amount'])), Decimal('1800.00'))
        self.assertEqual(Decimal(str(data['total_amount'])), Decimal('11800.00'))
        self.assertEqual(data['order_number'], 'ORD-INV-1')
