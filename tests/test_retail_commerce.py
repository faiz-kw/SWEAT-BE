"""
backend/tests/test_retail_commerce.py — Comprehensive Automated Tests for Retail Commerce,
Stock Reservations, Payment Safety & Pickup Fulfillment
"""

import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework import status

from config.routers import set_tenant_db_alias, TenantRoutingError
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.authentication.views import _build_tenant_token

from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_inventory import Product, StockReservation, InventoryMovement
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice
from apps.tenant_core.services_retail import (
    RetailCommerceService,
    InsufficientStockError,
    OrderStateError,
    FulfillmentTransitionError,
)
from apps.tenant_core.services_razorpay import RazorpayVerificationError, RazorpayService


class RetailCommerceTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.alias = 'tenant_test'
        set_tenant_db_alias(self.alias)

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code=f"RETAIL-TENANT-{uuid.uuid4().hex[:6]}",
            name='Retail SWEAT Tenant',
            slug='sweat-retail',
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
            name='Enterprise Plan',
            code=f"PLAN-{uuid.uuid4().hex[:6]}",
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Second Tenant for Isolation Tests
        self.tenant_b = Tenant.objects.using('default').create(
            code=f"TENANT-B-{uuid.uuid4().hex[:6]}",
            name='Other Tenant B',
            slug='other-b',
            status='ACTIVE',
        )

        # 3. Organization and Branches
        self.org = Organization.objects.using(self.alias).create(
            code='SWEAT-HQ',
            name='SWEAT Fitness',
            status='ACTIVE',
        )
        self.loc = Location.objects.using(self.alias).create(
            organization=self.org,
            code='LOC-BLR',
            name='Bangalore Central',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using(self.alias).create(
            organization=self.org,
            location=self.loc,
            code='BLR-IND',
            name='Indiranagar Branch',
            status='ACTIVE',
        )
        self.branch_b = Branch.objects.using(self.alias).create(
            organization=self.org,
            location=self.loc,
            code='BLR-KOR',
            name='Koramangala Branch',
            status='ACTIVE',
        )

        # 4. Staff User
        self.staff_user = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='staff@sweat.test',
            first_name='Staff',
            last_name='Manager',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        self.staff_user.set_password('SecretPass123!')
        self.staff_user.save(using=self.alias)

        # 5. Member User & Profile
        self.member_user = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='member@sweat.test',
            first_name='John',
            last_name='Doe',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using(self.alias).create(
            user=self.member_user,
            member_number='MEM-1001',
            first_name_snapshot='John',
            last_name_snapshot='Doe',
            preferred_branch=self.branch_a,
            member_status='ACTIVE',
            member_type='MEMBER',
            acquisition_source='MOBILE_APP',
        )

        # 6. Retail Products
        self.prod_whey = Product.objects.using(self.alias).create(
            organization=self.org,
            branch=self.branch_a,
            name='Gold Standard Whey Protein 2kg',
            category='Supplements',
            product_type='Supplement / Nutrition',
            sku='SUP-WHEY-001',
            price=Decimal('3500.00'),
            cost_price=Decimal('2200.00'),
            stock_quantity=10,
            status='In Stock',
            is_active=True,
        )
        self.prod_shaker = Product.objects.using(self.alias).create(
            organization=self.org,
            branch=self.branch_a,
            name='SWEAT Pro Stainless Shaker Bottle',
            category='Accessories',
            product_type='Physical Goods',
            sku='ACC-SHK-002',
            price=Decimal('750.00'),
            cost_price=Decimal('350.00'),
            stock_quantity=5,
            status='In Stock',
            is_active=True,
        )

        # 7. DRF API Client with Tenant JWT
        self.client = APIClient()
        token = str(_build_tenant_token(self.staff_user, self.tenant, self.alias).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    # =========================================================================
    # Group 1: Order and Purchase History
    # =========================================================================

    def test_01_create_retail_order_with_multiple_products_and_snapshots(self):
        """1-3: Creates retail order, verifies member/branch, immutable snapshots, and totals."""
        items = [
            {'product_id': self.prod_whey.id, 'quantity': 2},
            {'product_id': self.prod_shaker.id, 'quantity': 3},
        ]
        order, reservations = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=items,
            source='MOBILE_APP',
            notes='Mobile App pickup',
            db_alias=self.alias,
        )

        self.assertIsNotNone(order.id)
        self.assertTrue(order.order_number.startswith('RET-'))
        self.assertEqual(order.order_type, 'RETAIL_PURCHASE')
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.assertEqual(order.fulfillment_status, 'UNFULFILLED')
        self.assertEqual(order.user_profile, self.member_profile)
        self.assertEqual(order.branch, self.branch_a)

        # Expected total: (3500 * 2) + (750 * 3) = 7000 + 2250 = 9250.00
        expected_total = Decimal('9250.00')
        self.assertEqual(order.total_amount, expected_total)
        self.assertEqual(order.subtotal, expected_total)

        # Verify line items snapshots
        line_items = list(order.items.using(self.alias).all())
        self.assertEqual(len(line_items), 2)
        whey_item = next(it for it in line_items if it.product_id == self.prod_whey.id)
        self.assertEqual(whey_item.item_type, 'RETAIL')
        self.assertEqual(whey_item.sku_snapshot, 'SUP-WHEY-001')
        self.assertEqual(whey_item.item_name_snapshot, 'Gold Standard Whey Protein 2kg')
        self.assertEqual(whey_item.quantity, Decimal('2.00'))
        self.assertEqual(whey_item.unit_price_snapshot, Decimal('3500.00'))
        self.assertEqual(whey_item.total_amount, Decimal('7000.00'))

        # Verify stock reservation held
        self.assertEqual(len(reservations), 2)
        # Physical stock unchanged at this point
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 10)
        # Authoritative available stock reduced
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 8)
        self.assertEqual(self.prod_shaker.get_available_stock(using=self.alias), 2)

    def test_02_crm_list_and_detail_apis_return_retail_orders(self):
        """4: Verify CRM retail order list and detail APIs return full purchase history fields."""
        items = [{'product_id': self.prod_whey.id, 'quantity': 1}]
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=items,
            db_alias=self.alias,
        )

        # List endpoint
        res = self.client.get('/api/v1/tenant/retail-orders/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data)
        self.assertTrue(len(results) >= 1)
        found = next((o for o in results if str(o['id']) == str(order.id)), None)
        self.assertIsNotNone(found)
        self.assertEqual(found['order_type'], 'RETAIL_PURCHASE')
        self.assertEqual(found['fulfillment_status'], 'UNFULFILLED')
        self.assertEqual(found['branch_name'], 'Indiranagar Branch')
        self.assertEqual(found['member_name'], 'John Doe')

        # Detail endpoint
        detail_res = self.client.get(f'/api/v1/tenant/retail-orders/{order.id}/')
        self.assertEqual(detail_res.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_res.data['order_number'], order.order_number)
        self.assertEqual(len(detail_res.data['items']), 1)
        self.assertEqual(detail_res.data['items'][0]['item_name_snapshot'], 'Gold Standard Whey Protein 2kg')

    def test_03_existing_membership_package_orders_continue_to_work(self):
        """5: Verify existing membership/package orders continue to work safely."""
        pkg_order = Order.objects.using(self.alias).create(
            order_number=f"PKG-{uuid.uuid4().hex[:8].upper()}",
            user_profile=self.member_profile,
            branch=self.branch_a,
            order_type='NEW_MEMBERSHIP',
            status='PAID',
            subtotal=Decimal('15000.00'),
            total_amount=Decimal('15000.00'),
        )
        pkg_item = OrderItem.objects.using(self.alias).create(
            order=pkg_order,
            item_type='PACKAGE',
            product=None,  # Nullable for package orders
            item_name_snapshot='12 Month Unlimited Sweat Pass',
            quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('15000.00'),
            total_amount=Decimal('15000.00'),
        )

        # Verify querying works unchanged
        fetched = Order.objects.using(self.alias).get(id=pkg_order.id)
        self.assertEqual(fetched.order_type, 'NEW_MEMBERSHIP')
        self.assertIsNone(pkg_item.product)
        self.assertEqual(fetched.items.first().item_type, 'PACKAGE')

    # =========================================================================
    # Group 2: Stock and Reservations
    # =========================================================================

    def test_04_insufficient_stock_rejected(self):
        """6-7: Reject request when requested stock exceeds available stock."""
        items = [{'product_id': self.prod_whey.id, 'quantity': 15}]  # Available is 10
        with self.assertRaises(InsufficientStockError):
            RetailCommerceService.create_retail_order(
                user_profile=self.member_profile,
                branch=self.branch_a,
                items=items,
                db_alias=self.alias,
            )

    def test_05_reject_invalid_quantities_and_inactive_products(self):
        """8: Reject zero/negative quantities and inactive products."""
        # Zero quantity
        with self.assertRaises(ValidationError):
            RetailCommerceService.create_retail_order(
                user_profile=self.member_profile,
                branch=self.branch_a,
                items=[{'product_id': self.prod_whey.id, 'quantity': 0}],
                db_alias=self.alias,
            )

        # Negative quantity
        with self.assertRaises(ValidationError):
            RetailCommerceService.create_retail_order(
                user_profile=self.member_profile,
                branch=self.branch_a,
                items=[{'product_id': self.prod_whey.id, 'quantity': -2}],
                db_alias=self.alias,
            )

        # Inactive product
        self.prod_whey.is_active = False
        self.prod_whey.save(using=self.alias)
        with self.assertRaises(ValidationError):
            RetailCommerceService.create_retail_order(
                user_profile=self.member_profile,
                branch=self.branch_a,
                items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
                db_alias=self.alias,
            )

    def test_06_release_stock_after_payment_failure_or_cancellation(self):
        """10: Release stock reservation after payment failure or cancellation."""
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 3}],
            db_alias=self.alias,
        )
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 7)

        # Release reservation
        res = RetailCommerceService.release_retail_reservation(
            order_id=order.id,
            reason='PAYMENT_GATEWAY_FAILURE',
            db_alias=self.alias,
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['released_count'], 1)

        # Available stock fully restored to 10
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 10)
        self.assertEqual(self.prod_whey.stock_quantity, 10)

        # Order transitioned to CANCELLED
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(order.fulfillment_status, 'CANCELLED')

    def test_07_duplicate_release_does_not_double_restore_stock(self):
        """12: Verify duplicate release requests do not restore stock twice."""
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 2}],
            db_alias=self.alias,
        )
        # Release 1
        RetailCommerceService.release_retail_reservation(order.id, 'CANCELLED', db_alias=self.alias)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 10)

        # Release 2 (duplicate)
        res2 = RetailCommerceService.release_retail_reservation(order.id, 'CANCELLED', db_alias=self.alias)
        self.assertEqual(res2['released_count'], 0)  # 0 released again
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 10)  # Still 10, not 12!

    @override_settings(DEBUG=True)
    def test_08_successful_payment_converts_reservation_and_decrements_stock_once(self):
        """13-14: Successful payment converts reservation to sale and decrements physical stock exactly once."""
        rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 4}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )

        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        confirm_res = RetailCommerceService.confirm_retail_payment(
            order_id=order.id,
            payment_id=payment_id,
            razorpay_order_id=rzp_order_id,
            signature='test_signature',
            db_alias=self.alias,
            is_sandbox_simulation=True,
        )

        self.assertTrue(confirm_res['success'])
        self.assertFalse(confirm_res['already_processed'])

        # Physical stock decremented from 10 to 6
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 6)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 6)

        # Order status is PAID, fulfillment_status is UNFULFILLED
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PAID')
        self.assertEqual(order.fulfillment_status, 'UNFULFILLED')

        # PaymentTransaction recorded
        txns = list(PaymentTransaction.objects.using(self.alias).filter(order=order))
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].provider_transaction_id, payment_id)
        self.assertEqual(txns[0].status, 'SUCCESS')

        # Inventory Movement audit log recorded
        mov = InventoryMovement.objects.using(self.alias).filter(order=order, movement_type='SALE').first()
        self.assertIsNotNone(mov)
        self.assertEqual(mov.quantity_change, -4)
        self.assertEqual(mov.balance_after, 6)

    # =========================================================================
    # Group 3: Payment Idempotency
    # =========================================================================

    @override_settings(DEBUG=True)
    def test_09_duplicate_payment_callback_idempotency(self):
        """18: Process same successful callback twice; verify one payment and one stock deduction."""
        rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 3}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"

        # Callback 1
        res1 = RetailCommerceService.confirm_retail_payment(
            order_id=order.id,
            payment_id=payment_id,
            razorpay_order_id=rzp_order_id,
            signature='test_sig',
            db_alias=self.alias,
            is_sandbox_simulation=True,
        )
        self.assertFalse(res1['already_processed'])
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 7)

        # Callback 2 (duplicate replay)
        res2 = RetailCommerceService.confirm_retail_payment(
            order_id=order.id,
            payment_id=payment_id,
            razorpay_order_id=rzp_order_id,
            signature='test_sig',
            db_alias=self.alias,
            is_sandbox_simulation=True,
        )
        self.assertTrue(res2['already_processed'])

        # Stock must NOT be deducted again (still 7, not 4!)
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 7)

        # Only one payment transaction recorded
        self.assertEqual(
            PaymentTransaction.objects.using(self.alias).filter(order=order).count(),
            1
        )

    @override_settings(DEBUG=False)
    @patch.object(RazorpayService, 'verify_payment_signature', return_value=False)
    def test_10_reject_invalid_signature(self, mock_verify_sig):
        """15: Reject invalid payment signatures."""
        rzp_order_id = 'order_invalid_456'
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )
        with self.assertRaises(RazorpayVerificationError):
            RetailCommerceService.confirm_retail_payment(
                order_id=order.id,
                payment_id='pay_invalid_123',
                razorpay_order_id=rzp_order_id,
                signature='forged_bad_signature',
                db_alias=self.alias,
                is_sandbox_simulation=False,  # Enforces signature check
            )

        # Ensure order is not paid and stock is not deducted
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 10)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 9)

        # API endpoint call rejects invalid signature
        res = self.client.post(
            f'/api/v1/tenant/retail-orders/{order.id}/confirm-payment/',
            {
                'payment_id': 'pay_invalid_123',
                'razorpay_order_id': rzp_order_id,
                'razorpay_signature': 'forged_bad_signature',
                'is_sandbox_simulation': False,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'PAYMENT_VERIFICATION_FAILED')

    @override_settings(DEBUG=False)
    def test_10b_simulation_rejected_when_debug_false(self):
        """Verify simulation is strictly rejected when DEBUG is False, at both service and API levels."""
        rzp_order_id = 'order_sim_exploit_001'
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 2}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )
        initial_stock = self.prod_whey.stock_quantity  # 10
        initial_avail = self.prod_whey.get_available_stock(using=self.alias)  # 8

        # 1. Direct Service Call with simulation must raise ValidationError
        with self.assertRaises(ValidationError) as ctx:
            RetailCommerceService.confirm_retail_payment(
                order_id=order.id,
                payment_id='pay_sim_exploit_001',
                razorpay_order_id=rzp_order_id,
                signature='fake_sig',
                db_alias=self.alias,
                is_sandbox_simulation=True,
            )
        self.assertIn("Payment simulation is strictly forbidden when DEBUG is False.", str(ctx.exception))

        # State must remain untouched: order unpaid, stock not decremented
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, initial_stock)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), initial_avail)
        self.assertEqual(PaymentTransaction.objects.using(self.alias).filter(order=order).count(), 0)

        # 2. View/API Endpoint Call with simulation must return HTTP 403 Forbidden
        res = self.client.post(
            f'/api/v1/tenant/retail-orders/{order.id}/confirm-payment/',
            {
                'payment_id': 'pay_sim_exploit_002',
                'razorpay_order_id': rzp_order_id,
                'razorpay_signature': 'fake_sig',
                'is_sandbox_simulation': True,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'SIMULATION_NOT_ALLOWED')

        # State must still remain untouched
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, initial_stock)

    @override_settings(DEBUG=False)
    @patch.object(RazorpayService, 'fetch_and_verify_payment')
    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_10c_genuine_payment_verification_success(self, mock_verify_sig, mock_fetch_verify):
        """Genuine payment path with valid Razorpay HMAC signature succeeds when DEBUG is False."""
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
        signature = "valid_hmac_sha256_signature_hex"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 3}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )

        mock_fetch_verify.return_value = {
            'id': payment_id,
            'order_id': rzp_order_id,
            'status': 'captured',
            'amount': 1050000,
            'currency': 'INR',
        }

        # 1. API endpoint call with valid signature and NO simulation flag
        res = self.client.post(
            f'/api/v1/tenant/retail-orders/{order.id}/confirm-payment/',
            {
                'payment_id': payment_id,
                'razorpay_order_id': rzp_order_id,
                'razorpay_signature': signature,
                'is_sandbox_simulation': False,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['success'])

        mock_verify_sig.assert_called_once_with(
            razorpay_order_id=rzp_order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature,
            db_alias=self.alias,
        )
        mock_fetch_verify.assert_called_once()

        # State verification: Order PAID, stock decremented exactly once
        set_tenant_db_alias(self.alias)
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PAID')
        self.assertEqual(order.fulfillment_status, 'UNFULFILLED')

        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 7)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 7)

        # PaymentTransaction recorded
        txns = list(PaymentTransaction.objects.using(self.alias).filter(order=order))
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].provider_transaction_id, payment_id)
        self.assertEqual(txns[0].status, 'SUCCESS')

    @override_settings(DEBUG=True)
    def test_11_late_payment_oversell_protection(self):
        """20: Safe recovery policy when late payment succeeds after reservation released."""
        rzp_order_id = f"order_late_{uuid.uuid4().hex[:10]}"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 5}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )
        # Reservation released
        RetailCommerceService.release_retail_reservation(order.id, 'EXPIRED', db_alias=self.alias)

        # Another customer buys stock, leaving only 2 units available
        self.prod_whey.stock_quantity = 2
        self.prod_whey.save(using=self.alias)

        # Late payment callback arrives for 5 units!
        confirm_res = RetailCommerceService.confirm_retail_payment(
            order_id=order.id,
            payment_id=f"pay_late_{uuid.uuid4().hex[:10]}",
            razorpay_order_id=rzp_order_id,
            signature='test_sig',
            db_alias=self.alias,
            is_sandbox_simulation=True,
        )

        # Oversell is prevented: stock does not become negative!
        self.assertTrue(confirm_res['late_payment_exception'])
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 2)  # Unaltered!
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.fulfillment_status, 'CANCELLED')
        self.assertIn('OVERSELL_PREVENTED', order.notes)

    # =========================================================================
    # Group 4: Permissions and Fulfillment Lifecycle
    # =========================================================================

    def test_12_unpaid_order_cannot_be_marked_fulfilled(self):
        """25: Verify unpaid orders cannot be marked collected or ready for pickup."""
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            db_alias=self.alias,
        )
        self.assertEqual(order.status, 'PENDING_PAYMENT')

        # Direct service call
        with self.assertRaises(FulfillmentTransitionError):
            RetailCommerceService.update_fulfillment_status(
                order_id=order.id,
                new_status='FULFILLED',
                staff_user=self.staff_user,
                db_alias=self.alias,
            )

        # API endpoint call
        res = self.client.post(f'/api/v1/tenant/retail-orders/{order.id}/fulfill/', {
            'fulfillment_status': 'FULFILLED',
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('must be PAID', res.data.get('detail', ''))

    @override_settings(DEBUG=True)
    def test_13_staff_fulfillment_workflow(self):
        """Authorized staff marks paid order as READY_FOR_PICKUP, then FULFILLED."""
        rzp_order_id = f"order_{uuid.uuid4().hex[:12]}"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )
        RetailCommerceService.confirm_retail_payment(
            order_id=order.id,
            payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            razorpay_order_id=rzp_order_id,
            signature='test_sig',
            db_alias=self.alias,
            is_sandbox_simulation=True,
        )

        # 1. Transition to READY_FOR_PICKUP
        res1 = self.client.post(f'/api/v1/tenant/retail-orders/{order.id}/fulfill/', {
            'fulfillment_status': 'READY_FOR_PICKUP',
        })
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data['fulfillment_status'], 'READY_FOR_PICKUP')

        # 2. Transition to FULFILLED (collected)
        res2 = self.client.post(f'/api/v1/tenant/retail-orders/{order.id}/fulfill/', {
            'fulfillment_status': 'FULFILLED',
        })
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['fulfillment_status'], 'FULFILLED')
        self.assertIsNotNone(res2.data['fulfilled_at'])
        self.assertEqual(res2.data['fulfilled_by_name'], 'Staff Manager')

    def test_14_tenant_isolation_enforced(self):
        """23: Tenant A cannot access or mutate Tenant B's orders or products."""
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            db_alias=self.alias,
        )

        # Build token for Tenant B
        user_b = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='tenantb_user@other.test',
            first_name='TenantB',
            last_name='User',
            status='ACTIVE',
        )
        token_b = str(_build_tenant_token(user_b, self.tenant_b, 'tenant_other').access_token)
        client_b = APIClient()
        client_b.credentials(HTTP_AUTHORIZATION=f'Bearer {token_b}')

        # Request to view Tenant A's order from Tenant B fails closed or returns empty
        res = client_b.get(f'/api/v1/tenant/retail-orders/{order.id}/')
        self.assertIn(res.status_code, [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN, status.HTTP_503_SERVICE_UNAVAILABLE])

    # =========================================================================
    # Group 5: Razorpay Checkout Connection & Association Safety (Sprint 2.3)
    # =========================================================================

    @patch.object(RazorpayService, 'create_order')
    def test_15_retail_order_creation_returns_gateway_order_and_key(self, mock_create_order):
        """Sprint 2.3: Order creation generates Razorpay gateway order with server-calculated amount and returns client-safe checkout fields."""
        rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
        mock_create_order.return_value = {
            'id': rzp_order_id,
            'amount': 700000,
            'currency': 'INR',
            'status': 'created',
        }

        # 2 x Gold Standard Whey Protein @ 3500.00 each = 7000.00 INR (700000 paise)
        res = self.client.post(
            '/api/v1/tenant/retail-orders/',
            {
                'user_profile_id': str(self.member_profile.id),
                'branch_id': str(self.branch_a.id),
                'items': [{'product_id': str(self.prod_whey.id), 'quantity': 2}],
                'notes': 'Mobile App genuine checkout',
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # 1. Response contains client-safe checkout fields
        self.assertEqual(res.data['razorpay_order_id'], rzp_order_id)
        self.assertTrue(res.data['razorpay_key_id'].startswith('rzp_test_'))
        self.assertNotIn('secret', res.data)
        self.assertNotIn('key_secret', res.data)
        self.assertEqual(res.data['amount'], 7000.0)
        self.assertEqual(res.data['amount_paise'], 700000)
        self.assertEqual(res.data['currency'], 'INR')

        # 2. Server calculates authoritative amount and passes integer paise to gateway
        order_id = res.data['id']
        order = Order.objects.using(self.alias).get(id=order_id)
        self.assertEqual(order.total_amount, Decimal('7000.00'))
        mock_create_order.assert_called_once_with(
            amount_paise=700000,
            receipt=order.order_number,
            currency='INR',
            notes={
                'internal_order_id': str(order.id),
                'user_id': str(self.member_profile.user_id),
                'order_number': order.order_number,
            },
            db_alias=self.alias,
        )

        # 3. External gateway order ID is persisted on the Order model
        self.assertEqual(order.gateway_order_id, rzp_order_id)

        # 4. INITIATED PaymentTransaction recorded
        txn = PaymentTransaction.objects.using(self.alias).filter(order=order, status='INITIATED').first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.idempotency_key, f"rzp_order_{rzp_order_id}")
        self.assertEqual(txn.metadata.get('razorpay_order_id'), rzp_order_id)

    @patch.object(RazorpayService, 'create_order')
    def test_16_gateway_order_creation_failure_releases_stock_reservations(self, mock_create_order):
        """Sprint 2.3: Gateway order creation failure safely cancels order and releases held stock reservations."""
        mock_create_order.side_effect = RazorpayVerificationError("Gateway order creation failed: network error")

        initial_available = self.prod_whey.get_available_stock(using=self.alias)  # 10

        res = self.client.post(
            '/api/v1/tenant/retail-orders/',
            {
                'user_profile_id': str(self.member_profile.id),
                'branch_id': str(self.branch_a.id),
                'items': [{'product_id': str(self.prod_whey.id), 'quantity': 3}],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Gateway order creation failed', res.data.get('detail', ''))

        # Restore tenant DB context after client API call
        set_tenant_db_alias(self.alias)

        # Reservations released, stock fully restored
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), initial_available)
        self.assertEqual(self.prod_whey.stock_quantity, 10)

        # Verify the created order was cancelled and not left pending
        failed_order = Order.objects.using(self.alias).filter(
            user_profile=self.member_profile,
            order_type='RETAIL_PURCHASE',
        ).latest('created_at')
        self.assertEqual(failed_order.status, 'CANCELLED')
        self.assertEqual(failed_order.fulfillment_status, 'CANCELLED')
        reservations = StockReservation.objects.using(self.alias).filter(order=failed_order)
        self.assertTrue(all(r.status == 'RELEASED' for r in reservations))

    @override_settings(DEBUG=True)
    def test_17_reject_mismatched_gateway_order_id(self):
        """Sprint 2.3: Reject payment confirmation if submitted razorpay_order_id does not match persisted gateway order ID."""
        rzp_order_1 = 'order_rzp_correct_111'
        rzp_order_2 = 'order_rzp_other_222'

        order_1, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 2}],
            gateway_order_id=rzp_order_1,
            db_alias=self.alias,
        )

        initial_stock = self.prod_whey.stock_quantity  # 10
        initial_avail = self.prod_whey.get_available_stock(using=self.alias)  # 8

        # 1. Direct service call with mismatched gateway order ID must raise ValidationError
        with self.assertRaises(ValidationError) as ctx:
            RetailCommerceService.confirm_retail_payment(
                order_id=order_1.id,
                payment_id='pay_some_payment_123',
                razorpay_order_id=rzp_order_2,  # Mismatched!
                signature='some_sig',
                db_alias=self.alias,
                is_sandbox_simulation=True,
            )
        self.assertIn("Submitted razorpay_order_id does not match the gateway order ID", str(ctx.exception))

        # 2. API endpoint call with mismatched gateway order ID returns HTTP 400
        res = self.client.post(
            f'/api/v1/tenant/retail-orders/{order_1.id}/confirm-payment/',
            {
                'payment_id': 'pay_some_payment_123',
                'razorpay_order_id': rzp_order_2,  # Mismatched!
                'razorpay_signature': 'some_sig',
                'is_sandbox_simulation': True,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Submitted razorpay_order_id does not match the gateway order ID", res.data.get('detail', ''))

        # State must remain untouched: order unpaid, stock not deducted, no invoice
        set_tenant_db_alias(self.alias)
        order_1.refresh_from_db(using=self.alias)
        self.assertEqual(order_1.status, 'PENDING_PAYMENT')
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, initial_stock)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), initial_avail)
        self.assertEqual(MemberInvoice.objects.using(self.alias).filter(order=order_1).count(), 0)

    @override_settings(DEBUG=True)
    def test_18_reject_missing_gateway_order_id(self):
        """Sprint 2.3: Reject payment confirmation when order has no associated gateway order ID or submitted ID is missing."""
        # Order created without gateway order ID
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            gateway_order_id=None,
            db_alias=self.alias,
        )

        with self.assertRaises(ValidationError) as ctx:
            RetailCommerceService.confirm_retail_payment(
                order_id=order.id,
                payment_id='pay_test_123',
                razorpay_order_id='order_test_123',
                signature='test_sig',
                db_alias=self.alias,
                is_sandbox_simulation=True,
            )
        self.assertIn("does not have an associated gateway order ID", str(ctx.exception))

        # Rejects missing razorpay_order_id parameter
        order.gateway_order_id = 'order_test_valid'
        order.save(using=self.alias)
        with self.assertRaises(ValidationError) as ctx2:
            RetailCommerceService.confirm_retail_payment(
                order_id=order.id,
                payment_id='pay_test_123',
                razorpay_order_id='',
                signature='test_sig',
                db_alias=self.alias,
                is_sandbox_simulation=True,
            )
        self.assertIn("razorpay_order_id is required", str(ctx2.exception))

    @override_settings(DEBUG=False)
    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    @patch.object(RazorpayService, 'fetch_and_verify_payment')
    def test_19_invalid_gateway_fetch_verification_fails_closed(self, mock_fetch, mock_verify_sig):
        """Sprint 2.3: Gateway fetch_and_verify_payment mismatch strictly fails closed and does not mark order paid."""
        rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
        order, _ = RetailCommerceService.create_retail_order(
            user_profile=self.member_profile,
            branch=self.branch_a,
            items=[{'product_id': self.prod_whey.id, 'quantity': 1}],
            gateway_order_id=rzp_order_id,
            db_alias=self.alias,
        )

        # Mock gateway verification raising RAZORPAY_ORDER_MISMATCH
        mock_fetch.side_effect = RazorpayVerificationError(
            "Payment order mismatch: payment belongs to order order_foreign_999, not expected order.",
            code="RAZORPAY_ORDER_MISMATCH"
        )

        with self.assertRaises(RazorpayVerificationError):
            RetailCommerceService.confirm_retail_payment(
                order_id=order.id,
                payment_id='pay_fake_999',
                razorpay_order_id=rzp_order_id,
                signature='valid_sig',
                db_alias=self.alias,
                is_sandbox_simulation=False,
            )

        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.prod_whey.refresh_from_db(using=self.alias)
        self.assertEqual(self.prod_whey.stock_quantity, 10)
        self.assertEqual(self.prod_whey.get_available_stock(using=self.alias), 9)
