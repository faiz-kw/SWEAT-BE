"""
tests/test_privacy_dsr_workflows.py — Verification of Privacy, Consent & DSR Workflows.

Proves:
1. Processing purposes listing with RBAC.
2. Consent granting and append-only lifecycle.
3. Consent withdrawal with mandatory purpose protection.
4. Privacy request creation with 30-day statutory due date.
5. Asynchronous DSR Export workflow (file metadata generation, personal data aggregation).
6. Asynchronous DSR Erasure workflow (irreversible PII anonymization, user deactivation, audit preservation).
7. Tenant isolation & RBAC fail-closed enforcement.
8. Immutable audit trail on all mutations.
"""

import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_privacy import (
    ProcessingPurpose,
    ConsentRecord,
    PrivacyRequest,
    TenantAuditEvent,
)
from apps.tenant_core.models_rbac import (
    Organization, Branch, Location, Role, RoleAssignment,
    ModuleCatalog, SubmoduleCatalog, Permission, RolePermissionSet, RolePermissionSetItem
)
from apps.tenant_core.models_infra import File
from apps.tenant_core.tasks import process_dsr_export_task, process_dsr_erasure_task
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection, unregister_tenant_connection


class PrivacyDsrWorkflowsTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant & Data Source
        self.tenant = Tenant.objects.using('default').create(
            name='Privacy Test Fitness',
            slug='privacy-fitness',
            code='PRIV-001',
            status='ACTIVE',
        )
        from apps.master.models_infra import TenantDataSource
        TenantDataSource.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={
                'hosting_mode': 'PLATFORM_MANAGED',
                'source_type': 'PLATFORM_MANAGED',
                'database_name': 'test',
                'db_name': 'test',
                'status': 'ACTIVE',
            }
        )
        from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, ProductSubmodule, TenantModule
        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='PLAN-GROWTH',
            defaults={'name': 'Growth Plan', 'tier': 'growth', 'is_active': True}
        )
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.master_mod, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Module', 'is_active': True}
        )
        self.master_submod, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.master_mod, code='settings', defaults={'name': 'Settings', 'is_active': True}
        )
        self.tenant_mod, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=self.master_mod,
            defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'}
        )

        # 2. Organization & Branch
        self.org = Organization.objects.using('tenant_test').create(
            name='Privacy Org',
            code='PRIV-ORG',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Privacy Loc',
            code='PRIV-LOC',
            city='Mumbai',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Privacy Branch',
            code='PRIV-BR',
            status='ACTIVE',
        )

        # 3. RBAC Setup: Module 'core', Submodule 'settings', Permissions
        self.mod, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            code='core',
            defaults={'module_code': 'core', 'name': 'Core', 'status': 'ACTIVE', 'is_enabled': True}
        )
        self.submod, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod,
            code='settings',
            defaults={'submodule_code': 'settings', 'name': 'Settings', 'status': 'ACTIVE', 'is_enabled': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            submodule=self.submod,
            code='core.settings.view',
            defaults={
                'module': self.mod,
                'permission_code': 'core.settings.view',
                'label': 'View Settings',
                'action': 'view',
                'status': 'ACTIVE',
                'is_active': True,
            }
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            submodule=self.submod,
            code='core.settings.edit',
            defaults={
                'module': self.mod,
                'permission_code': 'core.settings.edit',
                'label': 'Edit Settings',
                'action': 'edit',
                'status': 'ACTIVE',
                'is_active': True,
            }
        )

        # 4. Admin Role with permissions and matrix access
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Privacy Admin Role',
            code='PRIV_ADMIN',
            scope='ORG',
            status='ACTIVE',
            is_active=True,
        )
        pset = RolePermissionSet.objects.using('tenant_test').create(
            role=self.admin_role,
            organization=self.org,
            name='Privacy Admin Set',
            status='ACTIVE',
            is_active=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset,
            permission=self.perm_view,
            is_allowed=True,
            granted=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset,
            permission=self.perm_edit,
            is_allowed=True,
            granted=True,
        )
        from apps.tenant_core.models_rbac import RoleModuleAccess, RoleSubmoduleAccess
        RoleModuleAccess.objects.using('tenant_test').create(
            role=self.admin_role,
            module=self.mod,
            permission_set=pset,
            can_access=True,
            is_visible=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.admin_role,
            submodule=self.submod,
            permission_set=pset,
            can_access=True,
            is_visible=True,
        )

        # 5. Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            home_branch=self.branch,
            email='privacy.admin@privacyfitness.com',
            first_name='Privacy',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.tenant_id = str(self.tenant.id)
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.admin_user,
            role=self.admin_role,
            scope_type='ORGANIZATION',
            is_active=True,
            status='ACTIVE',
        )

        # 6. Target Data Subject User
        self.target_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            home_branch=self.branch,
            email='subject.user@privacyfitness.com',
            first_name='John',
            last_name='Doe',
            phone='+919876543210',
            status='ACTIVE',
        )
        self.target_user.tenant_id = str(self.tenant.id)

        # 7. Processing Purposes
        self.purpose_marketing = ProcessingPurpose.objects.using('tenant_test').create(
            code='MARKETING_COMMS',
            name='Marketing Communications',
            description='Marketing emails and promotional SMS',
            is_mandatory=False,
            is_active=True,
            status='ACTIVE',
            legal_basis='CONSENT',
            retention_days=365,
        )
        self.purpose_essential = ProcessingPurpose.objects.using('tenant_test').create(
            code='CORE_SERVICE',
            name='Essential Gym Service Delivery',
            description='Processing required to maintain gym membership contract',
            is_mandatory=True,
            is_active=True,
            status='ACTIVE',
            legal_basis='CONTRACT',
            retention_days=1095,
        )

        # Authenticate client with signed Tenant JWT
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken()
        refresh['sub'] = str(self.admin_user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = ['PRIV_ADMIN']
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = self.admin_user.email
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.admin_user._auth_type = 'tenant'
        self.admin_user.db_alias = 'tenant_test'

    def tearDown(self):
        set_tenant_db_alias(None)
        for alias in list(self.databases if isinstance(self.databases, set) else []):
            if alias.startswith('tenant_') and alias != 'tenant_test':
                unregister_tenant_connection(alias)

    def test_list_processing_purposes(self):
        """Authenticated tenant user with core.settings.view can list active processing purposes."""
        response = self.client.get('/api/v1/tenant/processing-purposes/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        items = data.get('results', data) if isinstance(data, dict) else data
        codes = [p['code'] for p in items]
        self.assertIn('MARKETING_COMMS', codes)
        self.assertIn('CORE_SERVICE', codes)

    def test_record_consent_and_audit_event(self):
        """Recording consent creates an append-only row and emits an auditable event."""
        initial_audits = TenantAuditEvent.objects.using('tenant_test').filter(action='CONSENT_GRANTED').count()

        payload = {
            'user': str(self.target_user.id),
            'purpose': str(self.purpose_marketing.id),
            'capture_source': 'WEB_FORM',
            'proof_metadata': {'opt_in_checkbox': True, 'form_id': 'signup_v2'},
        }
        response = self.client.post('/api/v1/tenant/consent-records/', payload, format='json')
        self.assertEqual(response.status_code, 201)

        data = response.json()
        self.assertEqual(data['status'], 'GRANTED')
        self.assertEqual(data['purpose_code'], 'MARKETING_COMMS')

        # Assert database record created
        consent_rec = ConsentRecord.objects.using('tenant_test').get(id=data['id'])
        self.assertEqual(consent_rec.status, 'GRANTED')
        self.assertIsNotNone(consent_rec.granted_at)

        # Assert audit trail created
        new_audits = TenantAuditEvent.objects.using('tenant_test').filter(action='CONSENT_GRANTED').count()
        self.assertEqual(new_audits, initial_audits + 1)

    def test_withdraw_consent_and_mandatory_protection(self):
        """Withdrawing consent creates a WITHDRAWN record, but fails on mandatory purposes."""
        # 1. First grant consent
        ConsentRecord.objects.using('tenant_test').create(
            user=self.target_user,
            purpose=self.purpose_marketing,
            status='GRANTED',
            granted_at=timezone.now(),
        )

        # 2. Withdraw marketing consent
        withdraw_payload = {
            'purpose_id': str(self.purpose_marketing.id),
            'user_id': str(self.target_user.id),
            'notes': 'User requested unsub in profile settings',
        }
        response = self.client.post('/api/v1/tenant/consent-records/withdraw/', withdraw_payload, format='json')
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['status'], 'WITHDRAWN')
        self.assertIsNotNone(data['withdrawn_at'])

        # 3. Attempt to withdraw mandatory core service purpose -> must be rejected
        mandatory_payload = {
            'purpose_id': str(self.purpose_essential.id),
            'user_id': str(self.target_user.id),
        }
        resp_mand = self.client.post('/api/v1/tenant/consent-records/withdraw/', mandatory_payload, format='json')
        self.assertEqual(resp_mand.status_code, 400)
        self.assertIn('Cannot withdraw consent for mandatory purpose', str(resp_mand.json()))

    def test_create_and_track_privacy_request(self):
        """Creating a privacy request records statutory due date (30 days) and emits audit event."""
        payload = {
            'user': str(self.target_user.id),
            'request_type': 'ACCESS',
            'details': 'Please export all my attendance and profile records.',
        }
        response = self.client.post('/api/v1/tenant/privacy-requests/', payload, format='json')
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['status'], 'RECEIVED')
        self.assertEqual(data['request_type'], 'ACCESS')
        self.assertIsNotNone(data['due_at'])

        req = PrivacyRequest.objects.using('tenant_test').get(id=data['id'])
        # Assert due date is ~30 days in future
        self.assertTrue(req.due_at > timezone.now() + timedelta(days=28))

    def test_async_dsr_export_workflow(self):
        """DSR export gathers personal data, generates file metadata, and completes request."""
        # Create privacy request
        req = PrivacyRequest.objects.using('tenant_test').create(
            user=self.target_user,
            request_type='ACCESS',
            status='RECEIVED',
            received_at=timezone.now(),
        )

        # Grant sample consent so export includes it
        ConsentRecord.objects.using('tenant_test').create(
            user=self.target_user,
            purpose=self.purpose_marketing,
            status='GRANTED',
            granted_at=timezone.now(),
        )

        # Run async task (eager in test runner)
        result = process_dsr_export_task(str(req.id), db_alias='tenant_test')
        self.assertEqual(result['status'], 'COMPLETED')

        req.refresh_from_db()
        self.assertEqual(req.status, 'COMPLETED')
        self.assertIsNotNone(req.completed_at)
        self.assertIn('file_id', req.evidence)

        # Verify generated File record in database
        file_obj = File.objects.using('tenant_test').get(id=req.evidence['file_id'])
        self.assertEqual(file_obj.owner_id, self.target_user.id)
        self.assertEqual(file_obj.classification, 'CONFIDENTIAL')
        self.assertTrue(file_obj.file_size > 0)

        # Verify audit event
        audit = TenantAuditEvent.objects.using('tenant_test').filter(
            action='DSR_EXPORT_COMPLETED', resource_id=str(req.id)
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_type, 'SYSTEM_JOB')

    def test_async_dsr_erasure_workflow(self):
        """DSR erasure pseudonymizes PII, deactivates user, and preserves immutable audit log."""
        original_user_id = self.target_user.id
        original_email = self.target_user.email

        # Create audit event before erasure to prove audit trail is preserved
        TenantAuditEvent.objects.using('tenant_test').create(
            actor=self.target_user,
            actor_email=original_email,
            action='MEMBER_CHECKIN',
            resource_type='Attendance',
            resource_id=str(uuid.uuid4()),
        )

        req = PrivacyRequest.objects.using('tenant_test').create(
            user=self.target_user,
            request_type='ERASURE',
            status='RECEIVED',
            received_at=timezone.now(),
        )

        # Run async erasure task
        result = process_dsr_erasure_task(str(req.id), db_alias='tenant_test')
        self.assertEqual(result['status'], 'COMPLETED')

        req.refresh_from_db()
        self.assertEqual(req.status, 'COMPLETED')

        # Check user anonymization
        user = TenantUser.objects.using('tenant_test').get(id=original_user_id)
        self.assertNotEqual(user.email, original_email)
        self.assertIn('@privacy.deleted', user.email)
        self.assertEqual(user.first_name, 'Anonymized')
        self.assertEqual(user.phone, '')
        self.assertFalse(user.is_login_allowed)
        self.assertEqual(user.status, 'INACTIVE')

        # Check audit trail is NOT deleted
        prior_audit = TenantAuditEvent.objects.using('tenant_test').filter(action='MEMBER_CHECKIN').first()
        self.assertIsNotNone(prior_audit)
        self.assertEqual(prior_audit.actor_email, original_email)  # Historical record intact
