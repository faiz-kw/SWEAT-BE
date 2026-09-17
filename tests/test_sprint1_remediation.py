"""
Sprint 1 Remediation Automated Test Suite.
Tests the 3 verified Sprint 1 blockers:
1. Frontend Tenant Login & Universal Login Routing
2. Refresh Token Rotation (RTR) & Logout Blacklisting
3. Tenant Database Fail-Closed Routing & Master DB Protection
"""

import json
from django.test import TestCase, Client
from rest_framework import status
from apps.master.models_iam import PlatformUser
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_org import Organization
from apps.tenant_core.views import TenantDBMixin
from apps.tenant_core.serializers import TenantUserCreateSerializer
from apps.authentication.views import _build_platform_token
from config.routers import (
    TenantRouter, MasterRouter, TenantRoutingError,
    get_tenant_db_alias, set_tenant_db_alias,
)
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.tokens import RefreshToken


class TenantLoginTests(TestCase):
    """Blocker 1: Frontend & Universal Tenant Login tests."""

    def setUp(self):
        self.client = Client()
        self.platform_user = PlatformUser.objects.create(
            email='admin@performanceos.io',
            first_name='Platform',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=True,
        )
        self.platform_user.set_password('Admin@Test1234!')
        self.platform_user.save()

        self.tenant = Tenant.objects.create(
            name='Cult Fit',
            slug='cult-fit',
            code='CULT-FIT-001',
            status='ACTIVE',
        )

    def test_platform_login_without_slug_succeeds(self):
        """Platform user login without tenant_slug succeeds against Master DB."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'email': 'admin@performanceos.io',
                'password': 'Admin@Test1234!',
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data.get('user_type'), 'platform')
        self.assertIn('access', data)
        self.assertNotIn('refresh', data)
        self.assertIn('refresh_token', response.cookies)

    def test_tenant_login_wrong_slug_rejected(self):
        """Wrong tenant_slug -> 401 Unauthorized without leaking tenant existence."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'email': 'admin@cultfit.in',
                'password': 'TenantAdmin@123!',
                'tenant_slug': 'wrong-nonexistent-slug',
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        data = response.json()
        self.assertIn('error', data)

    def test_tenant_user_missing_slug_fails_closed_without_scan(self):
        """
        When tenant_slug is omitted, request strictly routes to Master DB platform login.
        Tenant DBs are NOT scanned (O(1) behavior preserved).
        Non-platform user receives 401.
        """
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'email': 'admin@cultfit.in',
                'password': 'TenantAdmin@123!',
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        data = response.json()
        self.assertEqual(data.get('error'), 'Invalid username/email or password.')

    def test_empty_credentials_rejected(self):
        """Missing email or password returns 400 Bad Request."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'email': '', 'password': ''}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RefreshTokenRotationTests(TestCase):
    """Blocker 2: Refresh Token Rotation (RTR) & Logout Blacklisting tests."""

    def setUp(self):
        self.client = Client()
        self.p_user = PlatformUser.objects.create(
            email='admin@performanceos.io',
            first_name='Platform',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=True,
        )
        self.initial_refresh = _build_platform_token(self.p_user)
        self.initial_refresh_str = str(self.initial_refresh)

    def test_refresh_token_rotation_flow(self):
        """
        Complete RTR test flow:
        1. refresh #1 -> success + new refresh token + HttpOnly cookie
        2. reuse old refresh token -> 401 Unauthorized
        3. new refresh token -> success
        4. logout -> refresh token blacklisted and rejected
        """
        # Step 1: refresh #1 -> success + new refresh token
        res1 = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': self.initial_refresh_str}),
            content_type='application/json'
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        data1 = res1.json()
        self.assertIn('access', data1)
        self.assertNotIn('refresh', data1)
        self.assertIn('refresh_token', res1.cookies)
        rotated_refresh_str = res1.cookies['refresh_token'].value
        self.assertNotEqual(self.initial_refresh_str, rotated_refresh_str)

        # Step 2: reuse old refresh token -> 401 Unauthorized
        res2 = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': self.initial_refresh_str}),
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('blacklisted', res2.json().get('detail', '').lower())

        # Step 3: new rotated refresh token -> success
        res3 = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': rotated_refresh_str}),
            content_type='application/json'
        )
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        data3 = res3.json()
        self.assertIn('access', data3)
        self.assertNotIn('refresh', data3)
        self.assertIn('refresh_token', res3.cookies)
        newest_refresh_str = res3.cookies['refresh_token'].value

        # Step 4: logout -> refresh token rejected
        res4 = self.client.post(
            '/api/v1/auth/logout/',
            data=json.dumps({'refresh': newest_refresh_str}),
            content_type='application/json'
        )
        self.assertEqual(res4.status_code, status.HTTP_200_OK)

        # Attempt to use blacklisted token after logout -> 401
        res5 = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': newest_refresh_str}),
            content_type='application/json'
        )
        self.assertEqual(res5.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_refresh_token_returns_400(self):
        """Calling refresh without token returns 400."""
        res = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class TenantFailClosedRoutingTests(TestCase):
    """Blocker 3: Tenant Database Fail-Closed Routing & Master DB Protection."""

    def setUp(self):
        set_tenant_db_alias(None)
        self.router = TenantRouter()
        self.master_router = MasterRouter()

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_tenant_router_read_fails_closed_when_context_missing(self):
        """TenantRouter.db_for_read must raise TenantRoutingError when context is missing."""
        set_tenant_db_alias(None)
        with self.assertRaises(TenantRoutingError) as ctx:
            self.router.db_for_read(Organization)
        self.assertIn("Tenant database routing failed for read", str(ctx.exception))

    def test_tenant_router_write_fails_closed_when_context_missing(self):
        """TenantRouter.db_for_write must raise TenantRoutingError when context is missing."""
        set_tenant_db_alias(None)
        with self.assertRaises(TenantRoutingError) as ctx:
            self.router.db_for_write(Organization)
        self.assertIn("Tenant database routing failed for write", str(ctx.exception))

    def test_tenant_router_routes_to_alias_when_context_present(self):
        """TenantRouter routes to the active alias when context is set."""
        set_tenant_db_alias('tenant_sample_db')
        self.assertEqual(self.router.db_for_read(Organization), 'tenant_sample_db')
        self.assertEqual(self.router.db_for_write(Organization), 'tenant_sample_db')

    def test_master_db_operations_route_to_default(self):
        """Master DB models (PlatformUser, Tenant) route to 'default' and query cleanly."""
        set_tenant_db_alias(None)
        self.assertEqual(self.master_router.db_for_read(PlatformUser), 'default')
        self.assertEqual(self.master_router.db_for_write(PlatformUser), 'default')
        self.assertEqual(self.master_router.db_for_read(Tenant), 'default')
        self.assertEqual(self.master_router.db_for_write(Tenant), 'default')

    def test_cross_database_relations_blocked(self):
        """TenantRouter and MasterRouter disallow relations between Master and Tenant DB models."""
        self.assertFalse(self.router.allow_relation(PlatformUser(), Organization()))
        self.assertFalse(self.master_router.allow_relation(PlatformUser(), Organization()))

    def test_tenant_migrations_prohibited_on_default(self):
        """TenantRouter strictly disallows migrating tenant_core models onto the default Master DB."""
        self.assertFalse(self.router.allow_migrate('default', 'tenant_core'))
        self.assertTrue(self.router.allow_migrate('tenant_sample', 'tenant_core'))

    def test_tenant_db_mixin_fails_closed_without_context(self):
        """TenantDBMixin.get_db() raises PermissionDenied when tenant context is missing."""
        set_tenant_db_alias(None)
        mixin = TenantDBMixin()
        with self.assertRaises(PermissionDenied) as ctx:
            mixin.get_db()
        self.assertIn("Tenant context is not active", str(ctx.exception))

    def test_tenant_db_mixin_returns_alias_when_active(self):
        """TenantDBMixin.get_db() returns active alias when context is present."""
        set_tenant_db_alias('tenant_active_alias')
        mixin = TenantDBMixin()
        self.assertEqual(mixin.get_db(), 'tenant_active_alias')

    def test_tenant_user_create_serializer_fails_closed_without_context(self):
        """TenantUserCreateSerializer raises PermissionDenied if db_alias is missing from context."""
        serializer = TenantUserCreateSerializer(context={})
        with self.assertRaises(PermissionDenied) as ctx:
            serializer.create({'email': 'test@tenant.com', 'password': 'pass', 'first_name': 'A', 'last_name': 'B'})
        self.assertIn("Tenant context is not active", str(ctx.exception))
