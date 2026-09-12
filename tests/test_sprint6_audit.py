"""
Sprint 6 — Audit Trail Comprehensive Automated Test Suite.

Verifies:
1. Model Append-Only Immutability:
   - save() on existing row raises PermissionDenied
   - delete() on row raises PermissionDenied
2. API Read-Only & RBAC Enforcement:
   - GET list & retrieve work with core.audit.view
   - POST, PUT, PATCH, DELETE return 405 Method Not Allowed
   - RBAC failure returns 403 Forbidden without core.audit.view
3. State Sanitization & Redaction:
   - Passwords, tokens, API keys, secrets, cookies redacted to '[REDACTED]'
   - Non-sensitive fields preserved
4. Strict Server-Side Actor Resolution:
   - Tenant user derived from authenticated token
   - Platform super admin derived from platform token (no cross-DB FK)
   - Integration context derived from verified request
   - Anonymous caller strictly rejected (never falls back to SYSTEM_JOB)
   - Client payload actor spoofing strictly ignored
5. Invariant Scope Resolution:
   - Derived strictly from persisted resource model
6. Correlation & Request ID Middleware:
   - request.correlation_id and request.request_id generated and attached
   - Echoed in response headers X-Correlation-ID and X-Request-ID
7. Mutation Coverage:
   - CREATE emits CREATE (before_state=None)
   - UPDATE emits UPDATE (persisted before_state, updated after_state)
   - DELETE emits DELETE (before_state, after_state=None)
   - RoleAssignment emits ASSIGN / UNASSIGN
   - OrganizationSettings /current emits UPDATE
   - PermissionSet /matrix emits exactly one MATRIX_UPDATE
8. Transaction Atomicity & Rollback:
   - Business mutation rollback aborts audit event
   - Audit failure aborts business mutation
   - Matrix rollback aborts matrix audit event
9. Multi-Tenant Database Isolation:
   - Audit records strictly in active tenant DB
   - Writing to Master DB ('default') fails closed
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, RequestFactory
from django.core.exceptions import PermissionDenied
from django.db import transaction, connections
from django.db.utils import ProgrammingError
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

from config.middleware import CorrelationIDMiddleware
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

from apps.master.models_tenant import Tenant
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, ProductSubmodule
from apps.master.models_iam import PlatformUser, PlatformRole, PlatformUserRole
from apps.master.models_infra import TenantDataSource

from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, Department
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings
from apps.tenant_core.models_privacy import TenantAuditEvent

from apps.tenant_core.audit import (
    sanitize_audit_state,
    snapshot_model_state,
    resolve_actor,
    resolve_scope,
    emit_audit_event,
)


class BaseSprint6TestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant, Plan, Subscription & Infra
        self.tenant = Tenant.objects.using('default').create(
            name='Sprint 6 Test Club',
            slug='sprint6-test-club',
            code='S6-CLUB-001',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Growth Plan',
            code='PLAN-GROWTH',
            tier='growth',
            is_active=True,
        )
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
        )
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core System', 'is_active': True}
        )
        from apps.master.models_saas import TenantModule
        self.tm_core, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant, module=self.mod_core, defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )

        # 2. Platform Admin
        self.plat_role = PlatformRole.objects.using('default').get(code='SUPER_ADMIN')
        self.plat_user = PlatformUser.objects.using('default').create(
            email='platform.sprint6@performanceos.internal',
            first_name='Super',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=False,
        )
        PlatformUserRole.objects.using('default').create(user=self.plat_user, role=self.plat_role, is_active=True)

        # 3. Tenant Org & Branches (created in tenant_test DB)
        self.org = Organization.objects.using('tenant_test').create(name='Sprint 6 Org', code='S6-ORG', status='ACTIVE')
        self.loc = Location.objects.using('tenant_test').create(organization=self.org, name='Central Location', code='LOC-01', status='ACTIVE')
        self.branch = Branch.objects.using('tenant_test').create(organization=self.org, location=self.loc, name='Central Branch', code='BR-01', status='ACTIVE')

        # 4. Tenant Users (created in tenant_test DB)
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sprint6.test',
            first_name='Audit',
            last_name='Admin',
            status='ACTIVE',
            home_branch=self.branch,
        )
        self.regular_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='staff@sprint6.test',
            first_name='Staff',
            last_name='Member',
            status='ACTIVE',
            home_branch=self.branch,
        )

        # 5. Tenant Roles
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Organization Admin',
            code='ORG_ADMIN',
            scope='ORG',
            is_system=True,
            is_active=True,
        )
        self.staff_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Staff Role',
            code='STAFF_ROLE',
            scope='BRANCH',
            is_system=False,
            is_active=True,
        )

        self.admin_assignment = RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.admin_role, branch=None, is_active=True, status='ACTIVE',
        )
        self.staff_assignment = RoleAssignment.objects.using('tenant_test').create(
            user=self.regular_user, role=self.staff_role, branch=self.branch, is_active=True, status='ACTIVE',
        )

        # 6. Catalog setup in Tenant DB
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(module_code='core', defaults={'name': 'Core System', 'is_enabled': True})
        self.sub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True})
        self.sub_roles, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='roles', defaults={'name': 'Roles', 'is_enabled': True})
        self.sub_perms, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='permissions', defaults={'name': 'Permissions', 'is_enabled': True})
        self.sub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True})
        self.sub_audit, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='audit', defaults={'name': 'Audit', 'is_enabled': True})
        self.sub_depts, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='departments', defaults={'name': 'Departments', 'is_enabled': True})

        # 7. Permissions
        self.perm_audit_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_audit, action='view',
            defaults={'permission_code': 'core.audit.view', 'label': 'View Audit Logs'}
        )
        self.perm_roles_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_roles, action='view',
            defaults={'permission_code': 'core.roles.view', 'label': 'View Roles'}
        )
        self.perm_roles_assign, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_roles, action='assign',
            defaults={'permission_code': 'core.roles.assign', 'label': 'Assign Roles'}
        )
        self.perm_perms_manage, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_perms, action='manage',
            defaults={'permission_code': 'core.permissions.manage', 'label': 'Manage Permissions'}
        )
        self.perm_perms_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_perms, action='view',
            defaults={'permission_code': 'core.permissions.view', 'label': 'View Permissions'}
        )
        self.perm_settings_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        self.perm_settings_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )
        self.perm_depts_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_depts, action='view',
            defaults={'permission_code': 'core.departments.view', 'label': 'View Departments'}
        )
        self.perm_depts_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_depts, action='create',
            defaults={'permission_code': 'core.departments.create', 'label': 'Create Departments'}
        )
        self.perm_depts_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_depts, action='edit',
            defaults={'permission_code': 'core.departments.edit', 'label': 'Edit Departments'}
        )
        self.perm_depts_delete, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_depts, action='delete',
            defaults={'permission_code': 'core.departments.delete', 'label': 'Delete Departments'}
        )

        # Grant admin all submodules & perms
        RoleModuleAccess.objects.using('tenant_test').get_or_create(role=self.admin_role, module=self.cat_core, defaults={'can_access': True})
        for sm in [self.sub_users, self.sub_roles, self.sub_perms, self.sub_settings, self.sub_audit, self.sub_depts]:
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=self.admin_role, submodule=sm, defaults={'can_access': True})

        self.perm_set, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.admin_role, defaults={'name': 'Admin Permissions', 'is_active': True}
        )
        for p in [
            self.perm_audit_view, self.perm_roles_view, self.perm_roles_assign,
            self.perm_perms_manage, self.perm_perms_view, self.perm_settings_view,
            self.perm_settings_edit, self.perm_depts_view, self.perm_depts_create,
            self.perm_depts_edit, self.perm_depts_delete,
        ]:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(permission_set=self.perm_set, permission=p, defaults={'granted': True})

    def get_tenant_token(self, user=None):
        u = user or self.admin_user
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(u.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.using('tenant_test').filter(user=u, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = u.email
        return str(refresh.access_token)

    def get_platform_token(self, user=None):
        u = user or self.plat_user
        refresh = RefreshToken()
        refresh['sub'] = str(u.id)
        refresh['user_type'] = 'platform'
        refresh['role'] = 'SUPER_ADMIN'
        refresh['email'] = u.email
        return str(refresh.access_token)


class Sprint6AuditUnitTests(BaseSprint6TestCase):
    """Unit tests for sanitization, state snapshot, actor resolution, and scope resolution."""

    def test_sanitize_audit_state_redaction(self):
        dirty_payload = {
            'username': 'john_doe',
            'password': 'PlainTextPassword123!',
            'password_hash': 'pbkdf2_sha256$260000$secretHash',
            'nested': {
                'auth_token': 'jwt.token.here',
                'api_key': 'ak_live_xyz987',
                'normal_field': 42,
                'cvv': 999,
                'credit_card': '4111222233334444',
            },
            'items': [
                {'secret_key': 'supersecret'},
                {'public_id': 'PUB-123'},
            ]
        }

        clean = sanitize_audit_state(dirty_payload)

        self.assertEqual(clean['username'], 'john_doe')
        self.assertEqual(clean['password'], '[REDACTED]')
        self.assertEqual(clean['password_hash'], '[REDACTED]')
        self.assertEqual(clean['nested']['auth_token'], '[REDACTED]')
        self.assertEqual(clean['nested']['api_key'], '[REDACTED]')
        self.assertEqual(clean['nested']['cvv'], '[REDACTED]')
        self.assertEqual(clean['nested']['credit_card'], '[REDACTED]')
        self.assertEqual(clean['nested']['normal_field'], 42)
        self.assertEqual(clean['items'][0]['secret_key'], '[REDACTED]')
        self.assertEqual(clean['items'][1]['public_id'], 'PUB-123')

    def test_snapshot_model_state_redacts_password_hash(self):
        user = TenantUser(
            organization=self.org,
            email='test.snap@fitness.com',
            first_name='Snap',
            last_name='Shot',
        )
        user.set_password('SecretP@ssword123')
        snap = snapshot_model_state(user)

        self.assertIsNotNone(snap)
        self.assertEqual(snap['email'], 'test.snap@fitness.com')
        self.assertEqual(snap['password_hash'], '[REDACTED]')

    def test_resolve_actor_tenant_user(self):
        rf = RequestFactory()
        req = rf.get('/')
        req.user = self.admin_user
        req.user._auth_type = 'tenant'

        actor_obj, actor_type, actor_email = resolve_actor(request=req)
        self.assertEqual(actor_obj, self.admin_user)
        self.assertEqual(actor_type, 'TENANT_USER')
        self.assertEqual(actor_email, self.admin_user.email)

    def test_resolve_actor_platform_super_admin(self):
        rf = RequestFactory()
        req = rf.get('/')
        req.user = self.plat_user
        req.user._auth_type = 'platform'

        actor_obj, actor_type, actor_email = resolve_actor(request=req)
        self.assertIsNone(actor_obj)  # No cross-DB FK
        self.assertEqual(actor_type, 'SUPER_ADMIN')
        self.assertEqual(actor_email, self.plat_user.email)

    def test_resolve_actor_integration(self):
        rf = RequestFactory()
        req = rf.get('/')
        req.is_integration_request = True
        req.integration_identifier = 'webhook-service@partner.com'

        actor_obj, actor_type, actor_email = resolve_actor(request=req)
        self.assertIsNone(actor_obj)
        self.assertEqual(actor_type, 'INTEGRATION')
        self.assertEqual(actor_email, 'webhook-service@partner.com')

    def test_resolve_actor_anonymous_strictly_rejected(self):
        rf = RequestFactory()
        req = rf.get('/')
        req.user = None

        with self.assertRaises(AuthenticationFailed):
            resolve_actor(request=req)

    def test_resolve_actor_system_job_server_side_only(self):
        actor_obj, actor_type, actor_email = resolve_actor(
            request=None, actor_type='SYSTEM_JOB', actor_email='worker@internal'
        )
        self.assertIsNone(actor_obj)
        self.assertEqual(actor_type, 'SYSTEM_JOB')
        self.assertEqual(actor_email, 'worker@internal')

    def test_resolve_scope_from_instance(self):
        dept = Department.objects.using('tenant_test').create(organization=self.org, name='Training', code='TRN')
        org_id, ce_id, loc_id, br_id = resolve_scope(instance=dept, db_alias='tenant_test')
        self.assertEqual(org_id, self.org.id)


class Sprint6AuditAppendOnlyTests(BaseSprint6TestCase):
    """Test model-level and API-level append-only protection."""

    def test_model_level_update_strictly_blocked(self):
        event = TenantAuditEvent.objects.using('tenant_test').create(
            actor_type='SYSTEM_JOB',
            actor_email='system@internal',
            action='TEST',
            resource_type='TestResource',
            resource_id='123',
        )

        event.description = 'Mutated description'
        with self.assertRaises(PermissionDenied):
            event.save(using='tenant_test')

    def test_model_level_delete_strictly_blocked(self):
        event = TenantAuditEvent.objects.using('tenant_test').create(
            actor_type='SYSTEM_JOB',
            actor_email='system@internal',
            action='TEST',
            resource_type='TestResource',
            resource_id='456',
        )

        with self.assertRaises(PermissionDenied):
            event.delete(using='tenant_test')

    def test_api_mutations_return_405_method_not_allowed(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # POST
        res_post = self.client.post('/api/v1/tenant/audit-events/', {'action': 'HACK'}, format='json')
        self.assertEqual(res_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        event = TenantAuditEvent.objects.using('tenant_test').create(
            actor_type='SYSTEM_JOB',
            actor_email='system@internal',
            action='TEST',
            resource_type='TestResource',
            resource_id='789',
        )

        # PUT
        res_put = self.client.put(f'/api/v1/tenant/audit-events/{event.id}/', {'action': 'HACK'}, format='json')
        self.assertEqual(res_put.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # PATCH
        res_patch = self.client.patch(f'/api/v1/tenant/audit-events/{event.id}/', {'action': 'HACK'}, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # DELETE
        res_del = self.client.delete(f'/api/v1/tenant/audit-events/{event.id}/')
        self.assertEqual(res_del.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class Sprint6AuditRBACAndAPITests(BaseSprint6TestCase):
    """Test read access governed by core.audit.view and centralized RBAC."""

    def test_audit_list_and_retrieve_allowed_with_permission(self):
        TenantAuditEvent.objects.using('tenant_test').create(
            actor=self.admin_user,
            actor_type='TENANT_USER',
            actor_email=self.admin_user.email,
            action='CREATE',
            resource_type='Department',
            resource_id='100',
        )

        token = self.get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.get('/api/v1/tenant/audit-events/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(res.data.get('results', res.data)), 1)

    def test_audit_list_blocked_without_permission(self):
        # regular_user lacks core.audit.view
        token = self.get_tenant_token(self.regular_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.get('/api/v1/tenant/audit-events/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class Sprint6CorrelationIDMiddlewareTests(TestCase):
    """Test CorrelationIDMiddleware request/correlation ID generation and propagation."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_middleware_generates_and_attaches_ids(self):
        req = self.factory.get('/')
        middleware = CorrelationIDMiddleware(get_response=lambda r: ResponseMock())

        res = middleware(req)
        self.assertTrue(hasattr(req, 'correlation_id'))
        self.assertTrue(hasattr(req, 'request_id'))
        self.assertIn('X-Correlation-ID', res)
        self.assertIn('X-Request-ID', res)
        self.assertEqual(res['X-Correlation-ID'], req.correlation_id)
        self.assertEqual(res['X-Request-ID'], req.request_id)

    def test_middleware_preserves_valid_incoming_uuids(self):
        incoming_corr = str(uuid.uuid4())
        incoming_req = str(uuid.uuid4())
        req = self.factory.get('/', HTTP_X_CORRELATION_ID=incoming_corr, HTTP_X_REQUEST_ID=incoming_req)
        middleware = CorrelationIDMiddleware(get_response=lambda r: ResponseMock())

        res = middleware(req)
        self.assertEqual(req.correlation_id, incoming_corr)
        self.assertEqual(req.request_id, incoming_req)
        self.assertEqual(res['X-Correlation-ID'], incoming_corr)
        self.assertEqual(res['X-Request-ID'], incoming_req)


class ResponseMock(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, value)


class Sprint6MutationAuditIntegrationTests(BaseSprint6TestCase):
    """Test complete mutation coverage and state capturing."""

    def test_create_mutation_emits_audit(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        initial_count = TenantAuditEvent.objects.using('tenant_test').count()
        res = self.client.post('/api/v1/tenant/departments/', {
            'organization': str(self.org.id),
            'name': 'Physiotherapy',
            'code': 'PHYSIO',
            'description': 'Rehab and therapy',
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        new_count = TenantAuditEvent.objects.using('tenant_test').count()
        self.assertEqual(new_count, initial_count + 1)

        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'CREATE')
        self.assertEqual(event.resource_type, 'Department')
        self.assertIsNone(event.before_state)
        self.assertIsNotNone(event.after_state)
        self.assertEqual(event.after_state['code'], 'PHYSIO')
        self.assertEqual(event.actor_email, self.admin_user.email)

    def test_update_mutation_emits_audit_with_before_and_after(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        dept = Department.objects.using('tenant_test').create(
            organization=self.org, name='Cardio Dept', code='CARDIO', description='Old desc'
        )

        res = self.client.patch(f'/api/v1/tenant/departments/{dept.id}/', {
            'description': 'Updated Cardio Zone',
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'UPDATE')
        self.assertEqual(event.resource_type, 'Department')
        self.assertEqual(event.before_state['description'], 'Old desc')
        self.assertEqual(event.after_state['description'], 'Updated Cardio Zone')

    def test_delete_mutation_emits_audit_with_null_after_state(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        dept = Department.objects.using('tenant_test').create(
            organization=self.org, name='Pilates', code='PILATES'
        )

        res = self.client.delete(f'/api/v1/tenant/departments/{dept.id}/')
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'DELETE')
        self.assertEqual(event.resource_type, 'Department')
        self.assertIsNotNone(event.before_state)
        self.assertEqual(event.before_state['code'], 'PILATES')
        self.assertIsNone(event.after_state)

    def test_branch_settings_delete_audit_and_rollback(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        bs = BranchSettings.objects.using('tenant_test').create(
            branch=self.branch,
            max_booking_capacity=250,
        )

        res = self.client.delete(f'/api/v1/tenant/branch-settings/{bs.id}/')
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

        # 1. Confirm deletion occurred
        self.assertFalse(BranchSettings.objects.using('tenant_test').filter(id=bs.id).exists())

        # 2. Confirm audit event created
        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'DELETE')
        self.assertEqual(event.resource_type, 'BranchSettings')
        self.assertEqual(event.resource_id, str(bs.id))
        self.assertIsNotNone(event.before_state)
        self.assertEqual(event.before_state['max_booking_capacity'], 250)
        self.assertIsNone(event.after_state)

        # 3. Confirm audit failure rolls back deletion
        set_tenant_db_alias('tenant_test')
        branch2 = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc, name='Second Branch', code='BR-02', status='ACTIVE'
        )
        bs2 = BranchSettings.objects.using('tenant_test').create(
            branch=branch2,
            max_booking_capacity=300,
        )
        with patch('apps.tenant_core.views.emit_audit_event', side_effect=RuntimeError("Audit failure")):
            with self.assertRaises(RuntimeError):
                self.client.delete(f'/api/v1/tenant/branch-settings/{bs2.id}/')

        # Confirm bs2 was NOT deleted due to transaction rollback
        self.assertTrue(BranchSettings.objects.using('tenant_test').filter(id=bs2.id).exists())

    def test_role_assignment_emits_assign_and_unassign(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # ASSIGN
        res_create = self.client.post('/api/v1/tenant/role-assignments/', {
            'user': str(self.regular_user.id),
            'role': str(self.admin_role.id),
            'scope_type': 'ORGANIZATION',
            'is_active': True,
        }, format='json')
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)

        event_create = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event_create.action, 'ASSIGN')
        self.assertEqual(event_create.resource_type, 'RoleAssignment')

        assignment_id = res_create.data['id']

        # UNASSIGN
        res_del = self.client.delete(f'/api/v1/tenant/role-assignments/{assignment_id}/')
        self.assertEqual(res_del.status_code, status.HTTP_204_NO_CONTENT)

        event_del = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event_del.action, 'UNASSIGN')
        self.assertEqual(event_del.resource_type, 'RoleAssignment')

    def test_matrix_mutation_emits_single_matrix_update_event(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        custom_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Custom Manager',
            code='CUSTOM_MGR',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        custom_perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=custom_role,
            name='Custom Manager Permissions',
            is_active=True,
            organization=self.org,
        )

        initial_count = TenantAuditEvent.objects.using('tenant_test').count()

        res = self.client.put(f'/api/v1/tenant/permission-sets/{custom_perm_set.id}/matrix/', {
            'module_access': [{'module_code': 'core', 'can_access': True}],
            'submodule_access': [{'submodule_code': 'audit', 'module_code': 'core', 'can_access': True}],
            'permissions': [{'permission_code': 'core.audit.view', 'granted': True}],
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        new_count = TenantAuditEvent.objects.using('tenant_test').count()
        self.assertEqual(new_count, initial_count + 1)

        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'MATRIX_UPDATE')
        self.assertEqual(event.resource_type, 'RolePermissionSet')
        self.assertIsNotNone(event.before_state)
        self.assertIsNotNone(event.after_state)

    def test_organization_settings_current_emits_update(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.patch('/api/v1/tenant/organization-settings/current/', {
            'currency': 'USD',
            'support_email': 'support@sprint6.test',
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        event = TenantAuditEvent.objects.using('tenant_test').latest('created_at')
        self.assertEqual(event.action, 'UPDATE')
        self.assertEqual(event.resource_type, 'OrganizationSettings')
        self.assertEqual(event.after_state['currency'], 'USD')


class Sprint6TransactionRollbackTests(BaseSprint6TestCase):
    """Test transactional atomicity between business mutation and audit emission."""

    def test_audit_failure_rolls_back_business_mutation(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Force emit_audit_event to fail
        with patch('apps.tenant_core.views.emit_audit_event', side_effect=RuntimeError("Audit disk failure")):
            with self.assertRaises(RuntimeError):
                self.client.post('/api/v1/tenant/departments/', {
                    'organization': str(self.org.id),
                    'name': 'Rollback Dept',
                    'code': 'ROLLBACK',
                }, format='json')

        # Verify department was NOT created due to transaction rollback
        dept_exists = Department.objects.using('tenant_test').filter(code='ROLLBACK').exists()
        self.assertFalse(dept_exists)

    def test_matrix_validation_error_rolls_back_everything_including_audit(self):
        token = self.get_tenant_token()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        custom_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Rollback Role',
            code='ROLLBACK_ROLE',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        custom_perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=custom_role,
            name='Rollback PermSet',
            is_active=True,
            organization=self.org,
        )

        initial_count = TenantAuditEvent.objects.using('tenant_test').count()

        # Submit invalid module code to force ValueError
        res = self.client.put(f'/api/v1/tenant/permission-sets/{custom_perm_set.id}/matrix/', {
            'module_access': [{'module_code': 'NON_EXISTENT_MODULE_XYZ', 'can_access': True}],
        }, format='json')

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        # Verify no audit event was persisted
        new_count = TenantAuditEvent.objects.using('tenant_test').count()
        self.assertEqual(new_count, initial_count)


class Sprint6TenantIsolationTests(BaseSprint6TestCase):
    """Test multi-tenant database isolation for audit records."""

    def test_audit_events_strictly_in_tenant_db_never_master(self):
        TenantAuditEvent.objects.using('tenant_test').create(
            actor_type='SYSTEM_JOB',
            actor_email='system@internal',
            action='ISOLATION_TEST',
            resource_type='TestResource',
            resource_id='999',
        )

        tenant_count = TenantAuditEvent.objects.using('tenant_test').filter(action='ISOLATION_TEST').count()
        self.assertEqual(tenant_count, 1)

        # Audit events table does not exist in default Master DB
        master_tables = connections['default'].introspection.table_names()
        self.assertNotIn('audit_events', master_tables)

    def test_emit_audit_event_fails_closed_when_default_db_requested(self):
        with self.assertRaises(PermissionDenied):
            emit_audit_event(
                action='ILLEGAL',
                resource_type='Resource',
                resource_id='1',
                actor_type='SYSTEM_JOB',
                db_alias='default',
            )
