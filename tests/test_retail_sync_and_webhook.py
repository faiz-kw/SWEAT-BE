"""
backend/tests/test_retail_sync_and_webhook.py — Tests for Dynamic Catalog Sync, Reservation Expiry & Webhook Safety

Tests:
1. Dynamic catalog sync transformation and database upsert (using live/mock store payloads).
2. Clean normalization of product names, image URLs, prices in INR, and categories.
3. Cleanup command for expired retail stock reservations (auto-cancellation).
4. Webhook payment finalization for retail orders via RazorpayPaymentFinalizerService.
"""

import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone
from django.core.management import call_command

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import UserProfile
from apps.tenant_core.models_inventory import Product, StockReservation, InventoryMovement
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice
from apps.tenant_core.services_retail import RetailCommerceService
from apps.tenant_core.services_catalog_sync import ShopCatalogSyncService
from apps.tenant_core.services_razorpay import RazorpayPaymentFinalizerService


class RetailSyncAndWebhookTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.alias = 'tenant_test'
        set_tenant_db_alias(self.alias)

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code=f"SYNC-TEST-{uuid.uuid4().hex[:6]}",
            name='Sync Test Tenant',
            slug='sync-test',
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
            name='Sync Test Plan',
            code=f"PLAN-{uuid.uuid4().hex[:6]}",
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Tenant Core Hierarchy
        self.org = Organization.objects.using(self.alias).create(
            name='Sync Test Organization',
            code=f"ORG-{uuid.uuid4().hex[:6]}",
            status='ACTIVE',
        )
        self.loc = Location.objects.using(self.alias).create(
            organization=self.org,
            name='Sync Main Location',
            city='Bangalore',
        )
        self.branch = Branch.objects.using(self.alias).create(
            organization=self.org,
            location=self.loc,
            name='Sync Main Branch',
            code=f"BR-{uuid.uuid4().hex[:6]}",
        )
        self.member_user = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email=f"member_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Shop',
            last_name='Member',
            home_branch=self.branch,
            status='ACTIVE',
        )
        self.user_profile = UserProfile.objects.using(self.alias).create(
            user=self.member_user,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot='Shop',
            last_name_snapshot='Member',
            preferred_branch=self.branch,
            member_status='ACTIVE',
            member_type='MEMBER',
            acquisition_source='MOBILE_APP',
        )


    def test_01_catalog_sync_transformation_and_upsert(self):
        """Verify dynamic transformation of WooCommerce store payload into SWEAT Product model."""
        mock_raw_products = [
            {
                "id": 5548,
                "name": "Anti-Skid Socks &#8211; Grey (Full Length)",
                "slug": "anti-skid-socks-grey-full-length",
                "sku": "",
                "description": "<p>Stay safe and comfortable with silicone grips.</p>",
                "short_description": "Premium grip socks",
                "prices": {
                    "price": "39900",
                    "regular_price": "99900",
                    "sale_price": "39900",
                    "currency_code": "INR"
                },
                "images": [
                    {"id": 1, "src": "https://sweatfitwellness.com/img1.jpg"},
                    {"id": 2, "src": "https://sweatfitwellness.com/img2.jpg"}
                ],
                "categories": [
                    {"id": 36, "name": "Accessories", "slug": "accessories"}
                ],
                "is_in_stock": True,
                "add_to_cart": {"minimum": 1, "maximum": 26}
            },
            {
                "id": 1219,
                "name": "Keto Principles",
                "slug": "keto-principles",
                "sku": "EBOOK-KETO-01",
                "description": "<p>A comprehensive keto nutritional guide.</p>",
                "prices": {
                    "price": "49900",
                    "regular_price": "49900",
                    "currency_code": "INR"
                },
                "images": [
                    {"id": 3, "src": "https://sweatfitwellness.com/keto.jpg"}
                ],
                "categories": [
                    {"id": 40, "name": "ebook", "slug": "ebook"}
                ],
                "is_in_stock": True,
                "add_to_cart": {"minimum": 1, "maximum": 1}
            }
        ]

        result = ShopCatalogSyncService.sync_to_database(
            db_alias=self.alias,
            organization_id=str(self.org.id),
            products_data=mock_raw_products,
        )

        self.assertEqual(result['total'], 2)
        self.assertEqual(result['created'], 2)
        self.assertEqual(result['updated'], 0)

        # Check socks product in DB
        socks = Product.objects.using(self.alias).get(sku="SFW-ACC-5548")
        self.assertEqual(socks.name, "Anti-Skid Socks - Grey (Full Length)")
        self.assertEqual(socks.category, "Accessories")
        self.assertEqual(socks.product_type, "Physical Goods")
        self.assertEqual(socks.price, Decimal("399.00"))
        self.assertEqual(socks.cost_price, Decimal("239.40"))
        self.assertEqual(socks.stock_quantity, 26)
        self.assertEqual(socks.image_url, "https://sweatfitwellness.com/img1.jpg")
        self.assertEqual(len(socks.images), 2)
        self.assertEqual(socks.status, "In Stock")

        # Check ebook product in DB
        ebook = Product.objects.using(self.alias).get(sku="EBOOK-KETO-01")
        self.assertEqual(ebook.name, "Keto Principles")
        self.assertEqual(ebook.category, "Digital Guides")
        self.assertEqual(ebook.product_type, "Digital Download")
        self.assertEqual(ebook.price, Decimal("499.00"))
        self.assertEqual(ebook.stock_quantity, 9999)

        # Re-run sync to verify idempotency (0 created, 2 updated)
        result2 = ShopCatalogSyncService.sync_to_database(
            db_alias=self.alias,
            organization_id=str(self.org.id),
            products_data=mock_raw_products,
        )
        self.assertEqual(result2['created'], 0)
        self.assertEqual(result2['updated'], 2)

    def test_02_cleanup_expired_retail_reservations_command(self):
        """Verify that abandoned retail orders with expired reservations are cancelled and stock is released."""
        product = Product.objects.using(self.alias).create(
            organization=self.org,
            name="Expiring Shaker Bottle",
            sku="ACC-SHAKE-01",
            category="Accessories",
            price=Decimal("500.00"),
            stock_quantity=10,
            status="In Stock",
        )

        # Create retail order
        order, reservations = RetailCommerceService.create_retail_order(
            user_profile=self.user_profile,
            branch=self.branch,
            items=[{"product_id": str(product.id), "quantity": 3}],
            db_alias=self.alias,
            initiate_gateway=False,
        )

        self.assertEqual(order.status, "PENDING_PAYMENT")
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0].status, "PENDING")

        # Artificially expire the reservation
        past_time = timezone.now() - timedelta(minutes=20)
        StockReservation.objects.using(self.alias).filter(id=reservations[0].id).update(expires_at=past_time)

        # Run cleanup management command
        call_command('cleanup_expired_retail_reservations', tenant='sync-test')

        # Verify order state
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, "CANCELLED")

        # Verify reservation released
        res = StockReservation.objects.using(self.alias).get(id=reservations[0].id)
        self.assertEqual(res.status, "RELEASED")
        self.assertEqual(res.release_reason, "EXPIRED")

        # Verify available stock is restored to physical count (10)
        self.assertEqual(RetailCommerceService.get_available_stock(product.id, db_alias=self.alias), 10)

    def test_03_webhook_finalizes_retail_purchase_safely(self):
        """Verify that asynchronous webhook finalizer safely completes a retail order without membership package."""
        product = Product.objects.using(self.alias).create(
            organization=self.org,
            name="Grip Socks Black",
            sku="ACC-SOCK-BLK",
            category="Accessories",
            price=Decimal("400.00"),
            stock_quantity=15,
            status="In Stock",
        )

        # Create retail order with gateway order ID
        gateway_order_id = "order_test_webhook_retail_123"
        order, reservations = RetailCommerceService.create_retail_order(
            user_profile=self.user_profile,
            branch=self.branch,
            items=[{"product_id": str(product.id), "quantity": 2}],
            db_alias=self.alias,
            initiate_gateway=False,
        )
        order.gateway_order_id = gateway_order_id
        order.save(using=self.alias, update_fields=['gateway_order_id'])

        payment_id = "pay_test_webhook_captured_999"

        # Dispatch via RazorpayPaymentFinalizerService (as called by RazorpayWebhookView)
        with patch('apps.tenant_core.services_razorpay.RazorpayService.is_configured', return_value=False):
            result = RazorpayPaymentFinalizerService.finalize_payment(
                razorpay_order_id=gateway_order_id,
                razorpay_payment_id=payment_id,
                source='WEBHOOK',
                db_alias=self.alias,
                raw_event={'event': 'payment.captured'},
            )

        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['order_type'], 'RETAIL_PURCHASE')
        self.assertEqual(result['order_id'], str(order.id))

        # Verify DB state
        order.refresh_from_db(using=self.alias)
        self.assertEqual(order.status, 'PAID')

        product.refresh_from_db(using=self.alias)
        # 15 - 2 = 13
        self.assertEqual(product.stock_quantity, 13)

        # Reservation fulfilled
        res = StockReservation.objects.using(self.alias).get(order=order)
        self.assertEqual(res.status, 'CONFIRMED')

        # Invoice generated
        invoice = MemberInvoice.objects.using(self.alias).get(order=order)
        self.assertEqual(invoice.status, 'PAID')
        self.assertEqual(invoice.total_amount, Decimal("800.00"))

        # Replay webhook (idempotency check)
        with patch('apps.tenant_core.services_razorpay.RazorpayService.is_configured', return_value=False):
            replay_result = RazorpayPaymentFinalizerService.finalize_payment(
                razorpay_order_id=gateway_order_id,
                razorpay_payment_id=payment_id,
                source='WEBHOOK',
                db_alias=self.alias,
                raw_event={'event': 'payment.captured'},
            )

        self.assertEqual(replay_result['status'], 'SUCCESS')
        self.assertTrue(replay_result['retail_result']['already_processed'])
        # Physical stock was NOT decremented again
        product.refresh_from_db(using=self.alias)
        self.assertEqual(product.stock_quantity, 13)

    def test_04_product_viewset_sync_catalog_api(self):
        """Verify POST /api/v1/tenant/products/sync-catalog/ dynamically updates products."""
        from rest_framework.test import APIRequestFactory
        from apps.tenant_core.views_inventory import ProductViewSet

        factory = APIRequestFactory()
        mock_raw = [
            {
                "id": 9991,
                "name": "Sweat Fitness Band",
                "slug": "sweat-fitness-band",
                "sku": "ACC-BAND-99",
                "description": "Premium durable fitness loop band",
                "prices": {"price": "69900", "currency_code": "INR"},
                "images": [{"id": 1, "src": "https://sweatfitwellness.com/band.jpg"}],
                "categories": [{"id": 36, "name": "Accessories", "slug": "accessories"}],
                "is_in_stock": True,
                "add_to_cart": {"minimum": 1, "maximum": 15}
            }
        ]

        with patch.object(ShopCatalogSyncService, 'fetch_live_products', return_value=mock_raw):
            request = factory.post('/api/v1/tenant/products/sync-catalog/', {})
            request.user = self.member_user
            request.organization = self.org
            request._tenant_db_alias = self.alias

            view = ProductViewSet.as_view({'post': 'sync_catalog'})
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        self.assertEqual(response.data['result']['total'], 1)

        band = Product.objects.using(self.alias).get(sku="ACC-BAND-99")
        self.assertEqual(band.name, "Sweat Fitness Band")
        self.assertEqual(band.price, Decimal("699.00"))

