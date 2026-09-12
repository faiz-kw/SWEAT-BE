"""
Sprint 9 Test Suite — Master Control Plane & Tenant Storage / Privacy / Audit Verification
Tests cover:
- Master Control Plane models, canonical fields, constraints, and backward compatibility:
  * TenantDataSource, TenantDataHostingPolicy, ResourceMetric,
  * SaasPlanResourceLimit, TenantResourceLimit, TenantResourceUsage, TenantUsageAlert,
  * PlatformSetting, PlatformAuditEvent.
- Tenant Storage & Frozen S3 Key Invariant:
  * File canonical fields and compatibility properties.
  * Strict preservation of frozen key pattern: tenants/{tenant_uuid}/files/{file_uuid}.
- Tenant Privacy models, canonical fields, constraints:
  * ProcessingPurpose, ConsentRecord, PrivacyRequest.
- Tenant Audit immutability and UUID conversions:
  * TenantAuditEvent append-only immutability.
  * UUID fields (resource_id, request_id, correlation_id).
  * before_state / before_data and after_state / after_data compatibility.
"""

import uuid
from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    SaasPlan, ResourceMetric, SaasPlanResourceLimit,
    TenantResourceLimit, TenantResourceUsage, TenantUsageAlert
)
from apps.master.models_infra import (
    TenantDataSource, TenantDataHostingPolicy,
    PlatformSetting, PlatformAuditEvent
)
from apps.tenant_core.models_rbac import Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_infra import File
from apps.tenant_core.models_privacy import (
    ProcessingPurpose, ConsentRecord, PrivacyRequest, TenantAuditEvent
)
from config.routers import set_tenant_db_alias


class Sprint9MasterControlPlaneTestCase(TestCase):
    """Test Master Control Plane models, canonical fields, and backward compatibility."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code='CULT_FIT_GOV',
            name='Cult Fit Governance',
            slug='cult-fit-gov',
            status='ACTIVE'
        )
        self.plan = SaasPlan.objects.create(
            code='ENTERPRISE_GOV',
            name='Enterprise Governance Plan',
            tier='ENTERPRISE',
            is_active=True
        )

    def test_tenant_data_source_canonical_and_compatibility(self):
        ds = TenantDataSource.objects.create(
            tenant=self.tenant,
            hosting_mode='DEDICATED_DB',
            database_engine='POSTGRESQL',
            database_name='tenant_cult_fit_gov',
            provider='PLATFORM',
            network_mode='PRIVATE_NETWORK',
            ssl_mode='REQUIRE',
            schema_version='1.0',
            status='ACTIVE',
            secret_reference=None  # Intentionally nullable in Sprint 9
        )
        # Verify canonical fields
        self.assertEqual(ds.hosting_mode, 'DEDICATED_DB')
        self.assertEqual(ds.database_engine, 'POSTGRESQL')
        self.assertEqual(ds.database_name, 'tenant_cult_fit_gov')
        self.assertEqual(ds.provider, 'PLATFORM')
        self.assertEqual(ds.network_mode, 'PRIVATE_NETWORK')
        self.assertEqual(ds.ssl_mode, 'REQUIRE')
        self.assertEqual(ds.schema_version, '1.0')
        self.assertEqual(ds.status, 'ACTIVE')
        self.assertIsNone(ds.secret_reference)

        # Verify backward compatibility properties
        self.assertEqual(ds.source_type, 'DEDICATED_DB')
        self.assertEqual(ds.db_name, 'tenant_cult_fit_gov')
        self.assertEqual(ds.db_schema_version, '1.0')
        self.assertFalse(ds.db_password_secret_ref)

        # Test save sync for backward compatibility
        ds.hosting_mode = 'CUSTOMER_MANAGED'
        ds.database_name = 'tenant_cult_fit_shared'
        ds.save()
        self.assertEqual(ds.source_type, 'CUSTOMER_MANAGED')
        self.assertEqual(ds.db_name, 'tenant_cult_fit_shared')

    def test_tenant_data_hosting_policy(self):
        policy = TenantDataHostingPolicy.objects.create(
            tenant=self.tenant,
            backup_owner='PLATFORM',
            restore_owner='PLATFORM',
            patching_owner='PLATFORM',
            monitoring_owner='PLATFORM',
            migration_owner='PLATFORM',
            encryption_owner='PLATFORM',
            availability_sla='99.9%',
            backup_policy_reference='S3 Daily Backups',
            dr_policy_reference='Cross-region replication',
            preferred_region='IN-MUMBAI',
            backup_retention_days=30
        )
        self.assertEqual(policy.backup_owner, 'PLATFORM')
        self.assertEqual(policy.restore_owner, 'PLATFORM')
        self.assertEqual(policy.patching_owner, 'PLATFORM')
        self.assertEqual(policy.monitoring_owner, 'PLATFORM')
        self.assertEqual(policy.migration_owner, 'PLATFORM')
        self.assertEqual(policy.encryption_owner, 'PLATFORM')
        self.assertEqual(policy.availability_sla, '99.9%')

    def test_resource_metrics_and_limits(self):
        metric = ResourceMetric.objects.create(
            code='ACTIVE_MEMBERS_GOV',
            name='Active Governance Members',
            unit='COUNT',
            aggregation_period='REALTIME',
            is_billable=False,
            status='ACTIVE'
        )
        self.assertEqual(metric.code, 'ACTIVE_MEMBERS_GOV')
        self.assertEqual(metric.aggregation_period, 'REALTIME')
        self.assertFalse(metric.is_billable)
        self.assertTrue(metric.is_active)  # Property check

        # Plan limit
        plan_limit = SaasPlanResourceLimit.objects.create(
            plan=self.plan,
            metric=metric,
            limit_value=Decimal('500.0000'),
            is_unlimited=False,
            warning_percent=Decimal('80.00'),
            enforcement_mode='HARD_BLOCK'
        )
        self.assertEqual(plan_limit.limit_value, Decimal('500.0000'))
        self.assertFalse(plan_limit.is_unlimited)
        self.assertEqual(plan_limit.soft_limit_pct, Decimal('80.00'))  # Property check

        # Tenant limit (unlimited)
        tenant_limit = TenantResourceLimit.objects.create(
            tenant=self.tenant,
            metric=metric,
            limit_value=Decimal('-1.0000'),
            is_unlimited=True,
            warning_threshold_percent=Decimal('85.00')
        )
        self.assertEqual(tenant_limit.limit_value, Decimal('-1.0000'))
        self.assertTrue(tenant_limit.is_unlimited)

    def test_tenant_resource_usage_and_alerts(self):
        metric = ResourceMetric.objects.create(
            code='STORAGE_GB_GOV',
            name='Storage GB Governance',
            unit='GB',
            aggregation_period='MONTHLY',
            is_billable=True,
            status='ACTIVE'
        )
        now = timezone.now()
        usage = TenantResourceUsage.objects.create(
            tenant=self.tenant,
            metric=metric,
            usage_value=Decimal('120.5000'),
            measured_at=now,
            last_calculated_at=now,
            is_platform_billable=True,
            period_start=None,  # Preserved nullable snapshot semantics
            period_end=None
        )
        self.assertEqual(usage.usage_value, Decimal('120.5000'))
        self.assertEqual(usage.current_value, 120)  # Legacy BigInt representation
        self.assertEqual(usage.last_calculated_at, now)  # Property check
        self.assertIsNone(usage.period_start)
        self.assertIsNone(usage.period_end)

        # Alert
        alert = TenantUsageAlert.objects.create(
            tenant=self.tenant,
            metric=metric,
            severity='WARNING',
            threshold_pct=80,
            current_pct=82,
            status='OPEN'
        )
        self.assertEqual(alert.threshold_pct, 80)
        self.assertEqual(alert.status, 'OPEN')
        self.assertEqual(alert.severity, 'WARNING')

    def test_platform_settings_and_audit(self):
        setting = PlatformSetting.objects.create(
            key='GOVERNANCE_PRIVACY_ENABLED',
            value={'enabled': True, 'default_retention_days': 365},
            category='PRIVACY',
            is_active=True
        )
        self.assertEqual(setting.key, 'GOVERNANCE_PRIVACY_ENABLED')
        self.assertTrue(setting.value['enabled'])
        self.assertEqual(setting.category, 'PRIVACY')
        self.assertTrue(setting.is_active)

        audit_id = uuid.uuid4()
        audit = PlatformAuditEvent.objects.create(
            event_name='SETTING_UPDATED',
            action='UPDATE',
            actor_email='superadmin@performanceos.io',
            resource_type='PlatformSetting',
            resource_id=audit_id,
            before_data={'enabled': False},
            after_data={'enabled': True},
            source_application='control_plane'
        )
        self.assertEqual(audit.resource_id, audit_id)
        self.assertEqual(audit.action, 'UPDATE')
        self.assertEqual(audit.event_name, 'SETTING_UPDATED')


class Sprint9TenantStorageTestCase(TestCase):
    """Test Tenant Storage, File model, and frozen S3 key invariant."""
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Storage Test Org',
            code='STORAGE_ORG',
            status='ACTIVE'
        )
        self.user = TenantUser.objects.create(
            organization=self.org,
            email='file_tester@performanceos.io',
            first_name='File',
            last_name='Tester',
            status='ACTIVE'
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_file_canonical_and_compatibility(self):
        file_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        # Frozen key format: tenants/{tenant_uuid}/files/{file_uuid}
        frozen_key = f"tenants/{tenant_id}/files/{file_id}"

        f = File.objects.create(
            id=file_id,
            owner_type='USER_AVATAR',
            owner_id=self.user.id,
            file_name='avatar.png',
            original_file_name='my_original_avatar.png',
            mime_type='image/png',
            file_size=1024,
            storage_provider='ZATA_S3',
            bucket_reference='zata-private-storage',
            object_key=frozen_key,
            checksum='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
            classification='INTERNAL'
        )
        # Verify canonical fields
        self.assertEqual(f.owner_type, 'USER_AVATAR')
        self.assertEqual(f.owner_id, self.user.id)
        self.assertEqual(f.file_name, 'avatar.png')
        self.assertEqual(f.original_file_name, 'my_original_avatar.png')
        self.assertEqual(f.storage_provider, 'ZATA_S3')
        self.assertEqual(f.bucket_reference, 'zata-private-storage')
        self.assertEqual(f.object_key, frozen_key)

        # Verify backward compatibility properties
        self.assertEqual(f.entity_type, 'USER_AVATAR')
        self.assertEqual(f.entity_id, self.user.id)
        self.assertEqual(f.original_filename, 'my_original_avatar.png')
        self.assertEqual(f.bucket_name, 'zata-private-storage')

        # Verify Frozen S3 Key Invariant
        self.assertTrue(f.object_key.startswith(f"tenants/{tenant_id}/files/"))
        self.assertNotIn(f.original_file_name, f.object_key)
        self.assertNotIn(f.owner_type, f.object_key)


class Sprint9TenantPrivacyTestCase(TestCase):
    """Test Tenant Privacy models (ProcessingPurpose, ConsentRecord, PrivacyRequest)."""
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Privacy Test Org',
            code='PRIVACY_ORG',
            status='ACTIVE'
        )
        self.user = TenantUser.objects.create(
            organization=self.org,
            email='privacy_tester@performanceos.io',
            first_name='Privacy',
            last_name='Tester',
            status='ACTIVE'
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_processing_purpose(self):
        purpose = ProcessingPurpose.objects.create(
            code='DIRECT_MARKETING',
            name='Direct Marketing Communications',
            description='Email and SMS promotional messages',
            legal_basis='CONSENT',
            retention_days=730,
            notice_version='1.0',
            status='ACTIVE'
        )
        self.assertEqual(purpose.code, 'DIRECT_MARKETING')
        self.assertEqual(purpose.legal_basis, 'CONSENT')
        self.assertEqual(purpose.retention_days, 730)
        self.assertEqual(purpose.status, 'ACTIVE')

    def test_consent_record(self):
        purpose = ProcessingPurpose.objects.create(
            code='APP_ANALYTICS',
            name='Application Telemetry & Analytics',
            legal_basis='LEGITIMATE_INTERESTS',
            retention_days=365,
            notice_version='1.0',
            status='ACTIVE'
        )
        consent = ConsentRecord.objects.create(
            user=self.user,
            purpose=purpose,
            status='GRANTED',
            consent_method='WEB_FORM',
            notice_version='1.0',
            ip_address='192.168.1.100',
            user_agent='Mozilla/5.0 PerformanceOS'
        )
        self.assertEqual(consent.user, self.user)
        self.assertEqual(consent.purpose, purpose)
        self.assertEqual(consent.status, 'GRANTED')
        self.assertEqual(consent.consent_method, 'WEB_FORM')

    def test_privacy_request(self):
        req = PrivacyRequest.objects.create(
            user=self.user,
            request_type='ERASURE',
            status='RECEIVED',
            subject_email=self.user.email,
            subject_name='Privacy Tester',
            details='Requesting erasure of marketing data',
            assigned_to=self.user,
            resolution=None
        )
        self.assertEqual(req.request_type, 'ERASURE')
        self.assertEqual(req.status, 'RECEIVED')
        self.assertEqual(req.assigned_to, self.user)
        self.assertIsNone(req.resolution)


class Sprint9TenantAuditTestCase(TestCase):
    """Test Tenant Audit immutability, UUID conversion, and backward compatibility."""
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Audit Test Org',
            code='AUDIT_ORG',
            status='ACTIVE'
        )
        self.user = TenantUser.objects.create(
            organization=self.org,
            email='audit_tester@performanceos.io',
            first_name='Audit',
            last_name='Tester',
            status='ACTIVE'
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_tenant_audit_event_canonical_fields(self):
        res_id = uuid.uuid4()
        req_id = uuid.uuid4()
        corr_id = uuid.uuid4()

        event = TenantAuditEvent.objects.create(
            actor=self.user,
            actor_type='TENANT_USER',
            actor_role='ADMIN',
            event_name='USER_PASSWORD_RESET',
            action='UPDATE',
            resource_type='TenantUser',
            resource_id=res_id,
            request_id=req_id,
            correlation_id=corr_id,
            before_state={'status': 'LOCKED'},
            after_state={'status': 'ACTIVE'},
            source_application='web_admin'
        )
        # Verify canonical fields
        self.assertEqual(event.event_name, 'USER_PASSWORD_RESET')
        self.assertEqual(event.resource_id, res_id)
        self.assertEqual(event.request_id, req_id)
        self.assertEqual(event.correlation_id, corr_id)

        # Verify before_data / after_data property aliases (Class B mapping)
        self.assertEqual(event.before_data, {'status': 'LOCKED'})
        self.assertEqual(event.after_data, {'status': 'ACTIVE'})

    def test_tenant_audit_event_immutability(self):
        event = TenantAuditEvent.objects.create(
            actor=self.user,
            actor_type='TENANT_USER',
            action='CREATE',
            resource_type='Role',
            resource_id=uuid.uuid4()
        )
        # Attempt update
        event.action = 'UPDATE'
        with self.assertRaises(PermissionDenied):
            event.save()

        # Attempt delete
        with self.assertRaises(PermissionDenied):
            event.delete()
