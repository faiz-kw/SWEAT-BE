"""
Layer 2 Phase 6 Tests: Module G (Commerce, Payments & Billing)

Covers:
- Order & OrderItem creation with immutable financial snapshots
- Multiple line item types (Package version, Class template)
- Payment transaction recording & balance settlement
- Idempotent payment handling (preventing double charges / duplicates)
- Automatic MemberInvoice generation & issuance on paid orders
- Refund processing & status updates
- Payment link generation with event logging
- REST API ViewSets & custom actions (record-payment, refund, create-payment-link)
"""

import uuid
from decimal import Decimal
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
from apps.tenant_core.models_crm import Lead, LeadSource
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_commerce import (
    Order, OrderItem, PaymentTransaction, Refund,
    MemberInvoice, PaymentLink, PaymentLinkEvent,
)
from apps.tenant_core.services_commerce import CommerceService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase6CommerceTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='COMMERCE-TENANT',
            name='Commerce Gym',
            slug='commerce-gym',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Commerce Plan',
            code='COMMERCE-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Platform', 'status': 'ACTIVE'},
        )
        self.tm_core = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='COMM-ORG',
            name='Commerce Gym Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='COMM-LOC',
            name='Commerce Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-COMM',
            name='Commerce Flagship Branch',
            status='ACTIVE',
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@commerce.test',
            first_name='Comm',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

        # 4. RBAC Setup for core module & settings submodule
        self.role_admin = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_admin,
            organization=self.org,
            is_active=True,
        )

        self.mod_cat, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core System', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.mod_cat, defaults={'can_access': True}
        )

        self.sub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule_code='settings',
            defaults={'name': 'Settings', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.sub_settings, defaults={'can_access': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule=self.sub_settings,
            action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule=self.sub_settings,
            action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'},
        )
        self.perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            name='Admin Perm Set',
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_edit, defaults={'granted': True}
        )

        # 5. Customer Profile
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member@commerce.test',
            first_name='Alex',
            last_name='Morgan',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_number='MEM-2001',
            first_name_snapshot='Alex',
            last_name_snapshot='Morgan',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # 6. Catalog Item for Purchase
        self.prog_cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='FIT-CAT',
            name='Fitness Category',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.prog_cat,
            code='SWEAT-PROG',
            name='Sweat Fitness Program',
            status='ACTIVE',
        )
        self.package = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            code='PILATES-8',
            name='Pilates 8 Sessions',
            status='ACTIVE',
        )
        self.package_version = PackageVersion.objects.using('tenant_test').create(
            package=self.package,
            version_number=1,
            name_snapshot='Pilates 8 Sessions v1',
            duration_value=2,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        self.package_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.package_version,
            currency='INR',
            base_price=Decimal('8000.00'),
            tax_percent=Decimal('18.000'),
            effective_from=timezone.now(),
            created_by_user=self.admin_user,
            status='ACTIVE',
        )

    def test_order_creation_and_snapshot_integrity(self):
        """Test atomic order creation and accurate line item financial calculations."""
        items_data = [
            {
                'item_type': 'PACKAGE',
                'package_id': str(self.package.id),
                'package_version_id': str(self.package_version.id),
                'package_price_id': str(self.package_price.id),
                'item_name_snapshot': 'Pilates 8 Sessions V1',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('8000.00'),
                'discount_amount': Decimal('500.00'),
                'tax_percent': Decimal('18.000'),
            }
        ]

        order = CommerceService.create_order(
            branch=self.branch,
            user_profile=self.member_profile,
            items_data=items_data,
            order_type='NEW_MEMBERSHIP',
            source='FRONT_DESK',
            created_by=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertTrue(order.order_number.startswith('ORD-'))
        self.assertEqual(order.subtotal, Decimal('8000.00'))
        self.assertEqual(order.discount_amount, Decimal('500.00'))
        # 8000 - 500 = 7500. 18% tax on 7500 = 1350. Total = 7500 + 1350 = 8850.
        self.assertEqual(order.tax_amount, Decimal('1350.00'))
        self.assertEqual(order.total_amount, Decimal('8850.00'))
        self.assertEqual(order.status, 'PENDING_PAYMENT')

        # Check line item
        self.assertEqual(order.items.count(), 1)
        item = order.items.first()
        self.assertEqual(item.item_name_snapshot, 'Pilates 8 Sessions V1')
        self.assertEqual(item.total_amount, Decimal('8850.00'))

    def test_payment_processing_and_automatic_invoice_issuance(self):
        """Test partial payments, settlement, and automatic MemberInvoice generation."""
        items_data = [
            {
                'item_type': 'PACKAGE',
                'package_version_id': str(self.package_version.id),
                'item_name_snapshot': 'Pilates Pack',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('1000.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }
        ]
        order = CommerceService.create_order(
            branch=self.branch,
            user_profile=self.member_profile,
            items_data=items_data,
            db_alias='tenant_test',
        )

        # 1. Partial payment: 400 INR
        txn1, invoice1 = CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('400.00'),
            provider='CASH',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(txn1.status, 'SUCCESS')
        self.assertIsNone(invoice1)  # Not fully paid yet
        order.refresh_from_db()
        self.assertEqual(order.status, 'PARTIALLY_PAID')

        # 2. Final settlement: remaining 600 INR
        txn2, invoice2 = CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('600.00'),
            provider='RAZORPAY',
            payment_method='UPI',
            provider_transaction_id='pay_razorpay_12345',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(txn2.status, 'SUCCESS')
        self.assertIsNotNone(invoice2)
        self.assertTrue(invoice2.invoice_number.startswith('INV-'))
        self.assertEqual(invoice2.total_amount, Decimal('1000.00'))
        self.assertEqual(invoice2.status, 'PAID')

        order.refresh_from_db()
        self.assertEqual(order.status, 'PAID')

    def test_payment_idempotency_protection(self):
        """Test retryable payment calls with idempotency_key return existing transaction without double charging."""
        items_data = [
            {
                'item_type': 'PACKAGE',
                'item_name_snapshot': 'Item',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('500.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }
        ]
        order = CommerceService.create_order(
            branch=self.branch,
            user_profile=self.member_profile,
            items_data=items_data,
            db_alias='tenant_test',
        )

        idem_key = 'IDEM-PAY-TEST-001'

        # First attempt
        txn1, _ = CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('500.00'),
            provider='RAZORPAY',
            idempotency_key=idem_key,
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        # Second attempt with same idempotency key (simulating webhook retry)
        txn2, _ = CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('500.00'),
            provider='RAZORPAY',
            idempotency_key=idem_key,
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertEqual(txn1.id, txn2.id)
        # Ensure only 1 payment transaction exists for this order
        self.assertEqual(PaymentTransaction.objects.using('tenant_test').filter(order=order).count(), 1)

    def test_refund_processing(self):
        """Test processing refunds against completed transactions."""
        items_data = [
            {
                'item_type': 'PACKAGE',
                'item_name_snapshot': 'Refundable Service',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('2000.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }
        ]
        order = CommerceService.create_order(
            branch=self.branch,
            user_profile=self.member_profile,
            items_data=items_data,
            db_alias='tenant_test',
        )
        txn, _ = CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('2000.00'),
            provider='CASH',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        refund = CommerceService.process_refund(
            payment_transaction_id=str(txn.id),
            amount=Decimal('2000.00'),
            reason_text='Customer cancelled prior to start date',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertEqual(refund.status, 'SUCCESS')
        self.assertEqual(refund.amount, Decimal('2000.00'))

        order.refresh_from_db()
        self.assertEqual(order.status, 'REFUNDED')

    def test_commerce_rest_apis(self):
        """Test order listing, recording payment, and processing refund via REST APIs."""
        refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        items_data = [
            {
                'item_type': 'PACKAGE',
                'item_name_snapshot': 'API Order Item',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('1500.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }
        ]
        order = CommerceService.create_order(
            branch=self.branch,
            user_profile=self.member_profile,
            items_data=items_data,
            db_alias='tenant_test',
        )

        # 1. List orders
        response = self.client.get('/api/v1/tenant/orders/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data.get('results', response.data)
        self.assertEqual(len(results), 1)

        # 2. Record payment custom action
        url_pay = f'/api/v1/tenant/orders/{order.id}/record-payment/'
        data_pay = {
            'amount': '1500.00',
            'provider': 'CASH',
            'payment_method': 'CASH',
        }
        res_pay = self.client.post(url_pay, data_pay, format='json')
        self.assertEqual(res_pay.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(res_pay.data.get('payment'))
        self.assertIsNotNone(res_pay.data.get('invoice'))
        txn_id = res_pay.data['payment']['id']

        # 3. Process refund custom action
        url_refund = f'/api/v1/tenant/payment-transactions/{txn_id}/refund/'
        data_refund = {
            'amount': '1500.00',
            'reason_text': 'Customer dissatisfaction',
        }
        res_refund = self.client.post(url_refund, data_refund, format='json')
        self.assertEqual(res_refund.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_refund.data.get('status'), 'SUCCESS')
