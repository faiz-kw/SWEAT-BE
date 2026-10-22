"""
backend/tests/test_inventory_crud.py — Automated Tests for Retail Products & Inventory CRUD

Covers:
1. Product REST API creation with media (images array, cover URL, video URL).
2. Product REST API partial update (price, stock adjustment).
3. Product REST API deletion.
4. Fail-closed tenant isolation (TenantRoutingError outside tenant context).
5. Data persistence in tenant-isolated database.
"""

import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from config.routers import (
    set_tenant_db_alias,
    TenantRoutingError,
)
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_inventory import Product
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser


class InventoryProductCrudTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.alias = 'tenant_test'
        set_tenant_db_alias(self.alias)

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code=f"INVENTORY-TEST-{uuid.uuid4().hex[:6]}",
            name='Inventory Test Tenant',
            slug='inventory-test',
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
            name='Inventory Test Plan',
            code=f"PLAN-{uuid.uuid4().hex[:6]}",
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Second Tenant for Tenant Isolation Verification
        self.tenant_b = Tenant.objects.using('default').create(
            code=f"OTHER-GYM-{uuid.uuid4().hex[:6]}",
            name='Other Gym Tenant',
            slug='other-gym',
            status='ACTIVE',
        )

        # 3. Tenant DB Org Setup
        self.org = Organization.objects.using(self.alias).create(
            code='SWEAT-ORG',
            name='SWEAT Studio',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using(self.alias).create(
            code='OTHER-ORG',
            name='Other Studio',
            status='ACTIVE',
        )
        self.loc = Location.objects.using(self.alias).create(
            organization=self.org,
            code='IND-BLR',
            name='Bangalore',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(self.alias).create(
            organization=self.org,
            location=self.loc,
            code='BLR-KOR',
            name='Koramangala',
            status='ACTIVE',
        )

        # 4. Staff User
        self.staff_user = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='staff@sweat.test',
            first_name='Faiz',
            last_name='Staff',
            home_branch=self.branch,
            status='ACTIVE',
        )
        self.staff_user.set_password('TestPass123!')
        self.staff_user.save(using=self.alias)

        # API Client with Tenant JWT
        self.client = APIClient()
        token = str(_build_tenant_token(self.staff_user, self.tenant, self.alias).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_tenant_isolation_fail_closed(self):
        """Verifies that accessing Product without tenant context fails closed."""
        set_tenant_db_alias(None)
        with self.assertRaises(TenantRoutingError):
            Product.objects.count()

    def test_api_create_product_success(self):
        """Verifies that POST /api/v1/tenant/products/ creates and persists a product."""
        payload = {
            'name': 'Pro Pilates Grip Socks',
            'category': 'Accessories',
            'product_type': 'Physical Goods',
            'sku': 'SOCK-PIL-001',
            'price': '499.00',
            'cost_price': '180.00',
            'stock_quantity': 30,
            'image_url': 'https://example.com/cover.jpg',
            'images': ['https://example.com/cover.jpg', 'https://example.com/gallery1.jpg'],
            'video_url': 'https://example.com/demo.mp4',
            'description': 'Anti-skid grip socks for reformer sessions.',
            'status': 'In Stock',
        }

        response = self.client.post('/api/v1/tenant/products/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        
        product_id = response.data['id']
        self.assertEqual(response.data['name'], 'Pro Pilates Grip Socks')
        self.assertEqual(response.data['stock_quantity'], 30)
        self.assertEqual(len(response.data['images']), 2)

        # Verify actual persistence in tenant database
        p = Product.objects.using(self.alias).filter(id=product_id).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.name, 'Pro Pilates Grip Socks')
        self.assertEqual(p.stock_quantity, 30)
        self.assertEqual(p.sku, 'SOCK-PIL-001')
        self.assertEqual(p.video_url, 'https://example.com/demo.mp4')
        self.assertEqual(len(p.images), 2)
        self.assertEqual(p.organization_id, self.org.id)

    def test_api_update_product_stock_and_price(self):
        """Verifies that PATCH /api/v1/tenant/products/{id}/ updates stock and price cleanly."""
        p = Product.objects.using(self.alias).create(
            organization=self.org,
            name='Pre-Workout Energy Tub',
            category='Supplements',
            product_type='Supplement / Nutrition',
            price=Decimal('1899.00'),
            cost_price=Decimal('1100.00'),
            stock_quantity=15,
            status='In Stock'
        )
        product_id = str(p.id)

        update_payload = {
            'stock_quantity': 12,
            'price': '1799.00',
            'status': 'In Stock'
        }

        response = self.client.patch(f'/api/v1/tenant/products/{product_id}/', update_payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['stock_quantity'], 12)
        self.assertEqual(Decimal(str(response.data['price'])), Decimal('1799.00'))

        # Verify database reflection
        updated = Product.objects.using(self.alias).get(id=product_id)
        self.assertEqual(updated.stock_quantity, 12)
        self.assertEqual(updated.price, Decimal('1799.00'))

    def test_api_delete_product(self):
        """Verifies that DELETE /api/v1/tenant/products/{id}/ removes product from tenant DB."""
        p = Product.objects.using(self.alias).create(
            organization=self.org,
            name='Temporary Discontinued Item',
            category='Other',
            price=Decimal('99.00'),
            stock_quantity=0,
            status='Discontinued'
        )
        product_id = str(p.id)

        response = self.client.delete(f'/api/v1/tenant/products/{product_id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertFalse(Product.objects.using(self.alias).filter(id=product_id).exists())

    def test_api_list_scoping_by_organization(self):
        """Verifies that listing products strictly scopes to the user's active organization."""
        # Product belonging to Staff's Org
        p1 = Product.objects.using(self.alias).create(
            organization=self.org,
            name='Our Org Whey Protein',
            category='Supplements',
            price=Decimal('2500.00'),
            stock_quantity=10,
        )
        # Product belonging to Org B
        p2 = Product.objects.using(self.alias).create(
            organization=self.org_b,
            name='Other Org Yoga Mat',
            category='Accessories',
            price=Decimal('1200.00'),
            stock_quantity=5,
        )

        response = self.client.get('/api/v1/tenant/products/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        result_names = [item['name'] for item in response.data.get('results', [])]
        self.assertIn('Our Org Whey Protein', result_names)
        self.assertNotIn('Other Org Yoga Mat', result_names)
