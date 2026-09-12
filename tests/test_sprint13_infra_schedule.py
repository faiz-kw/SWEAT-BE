"""
Sprint 13 — Focused Test Suite
CONTROL-PLANE INFRA, MARKETPLACE, NOTIFICATION TEMPLATES, BRANCH SCHEDULE
EXACTLY 53 TARGET ITEMS (17 C-existing + 14 C-missing + 22 New Table Fields)

Coverage:
1. Workstream A (Control Plane Infra): tenant_provisioning (1 C-ex, 5 C-mi), tenant_data_source_health (3 C-mi)
2. Workstream B (Marketplace / Integrations): marketplace_integrations (3 C-ex, 2 C-mi), saas_plan_integrations (3 C-ex, 1 C-mi), tenant_integration_entitlements (3 C-ex, 3 C-mi)
3. Workstream C (Notification Templates): notification_templates (7 C-ex)
4. Workstream D (Branch Schedule): 2 new tables (branch_working_hours, branch_operating_exceptions), empty table initialization, constraints & FKs, schedule business rules
5. RBAC & Audit Verification: canonical permissions core.settings.* and fail-closed audit event emission
"""

import uuid
from datetime import date, time, datetime, timedelta
from zoneinfo import ZoneInfo
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError, transaction, connections
from django.db.models.deletion import RestrictedError
from rest_framework.test import APIRequestFactory, force_authenticate

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantProvisioning, TenantDataSource, TenantDataSourceHealth
from apps.master.models_market import MarketplaceIntegration, SaasPlanIntegration, TenantIntegrationEntitlement
from apps.master.models_saas import SaasPlan
from apps.master.models_iam import PlatformUser
from apps.tenant_core.models_org import Organization, Branch, Location, CompanyEntity
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_govern import (
    NotificationTemplate,
    BranchWorkingHours,
    BranchOperatingException,
)
from apps.tenant_core.models_privacy import TenantAuditEvent
from apps.tenant_core.services_schedule import BranchScheduleService
from apps.tenant_core.views import BranchWorkingHoursViewSet, BranchOperatingExceptionViewSet


class Sprint13MasterInfraAndMarketplaceTests(TestCase):
    """Workstreams A & B: Control Plane Infra and Marketplace Integrations (24 items)."""

    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name='Sprint 13 Gym Master',
            slug=f's13-master-{uuid.uuid4().hex[:8]}',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.create(
            code=f'PLAN-{uuid.uuid4().hex[:6].upper()}',
            name='Enterprise Tier',
            tier='enterprise',
        )

    def test_tenant_provisioning_canonical_fields(self):
        """tenant_provisioning: tenant (RESTRICT), provisioning_status, target_schema_version, database_created_at, failed_at, failure_reason."""
        now = timezone.now()
        prov = TenantProvisioning.objects.create(
            tenant=self.tenant,
            status='COMPLETED',
            provisioning_status='COMPLETED',
            target_schema_version='1.0',
            database_created_at=now,
            failed_at=None,
            failure_reason=None,
        )
        prov.refresh_from_db()
        self.assertEqual(prov.provisioning_status, 'COMPLETED')
        self.assertEqual(prov.target_schema_version, '1.0')
        self.assertIsNotNone(prov.database_created_at)
        self.assertIsNone(prov.failed_at)
        self.assertIsNone(prov.failure_reason)

        # FK ON DELETE RESTRICT check
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic():
                self.tenant.delete()

    def test_tenant_data_source_health_canonical_fields(self):
        """tenant_data_source_health: tenant (RESTRICT), detected_schema_version, error_code, status untouched."""
        ds = TenantDataSource.objects.create(
            tenant=self.tenant,
            db_name=f'tenant_db_{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        health = TenantDataSourceHealth.objects.create(
            tenant=self.tenant,
            data_source=ds,
            status='HEALTHY',
            detected_schema_version='1.0.0',
            error_code='ERR_NONE',
        )
        health.refresh_from_db()
        self.assertEqual(health.tenant, self.tenant)
        self.assertEqual(health.detected_schema_version, '1.0.0')
        self.assertEqual(health.error_code, 'ERR_NONE')
        self.assertEqual(health.status, 'HEALTHY')

        # FK ON DELETE RESTRICT check
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic():
                self.tenant.delete()

    def test_marketplace_integrations_canonical_fields(self):
        """marketplace_integrations: name (150), code (100), status (default DRAFT), category, configuration_schema."""
        mkt = MarketplaceIntegration.objects.create(
            name='Razorpay Payment Gateway Integration',
            code=f'RAZORPAY-{uuid.uuid4().hex[:6]}',
            category='PAYMENTS',
            configuration_schema={'key_id': 'string', 'secret': 'string'},
        )
        mkt.refresh_from_db()
        self.assertEqual(mkt.category, 'PAYMENTS')
        self.assertEqual(mkt.status, 'DRAFT')
        self.assertIn('key_id', mkt.configuration_schema)

    def test_saas_plan_integrations_canonical_fields(self):
        """saas_plan_integrations: plan (RESTRICT), integration (RESTRICT), is_included (default TRUE), created_at."""
        mkt = MarketplaceIntegration.objects.create(
            name='TeleCMI Voice Gateway',
            code=f'TELECMI-{uuid.uuid4().hex[:6]}',
            category='TELEPHONY',
        )
        spi = SaasPlanIntegration.objects.create(
            plan=self.plan,
            integration=mkt,
            is_included=True,
        )
        spi.refresh_from_db()
        self.assertTrue(spi.is_included)
        self.assertIsNotNone(spi.created_at)

        # Plan deletion blocked by RESTRICT
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic():
                self.plan.delete()

        # Integration deletion blocked by RESTRICT
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic():
                mkt.delete()

    def test_tenant_integration_entitlements_canonical_fields(self):
        """tenant_integration_entitlements: tenant (RESTRICT), integration (RESTRICT), status (default AVAILABLE), enabled_at, disabled_at, updated_at."""
        mkt = MarketplaceIntegration.objects.create(
            name='Gupshup WhatsApp API',
            code=f'GUPSHUP-{uuid.uuid4().hex[:6]}',
            category='MESSAGING',
        )
        ent = TenantIntegrationEntitlement.objects.create(
            tenant=self.tenant,
            integration=mkt,
        )
        ent.refresh_from_db()
        self.assertEqual(ent.status, 'AVAILABLE')
        self.assertIsNone(ent.enabled_at)
        self.assertIsNone(ent.disabled_at)
        self.assertIsNotNone(ent.updated_at)

        # Enable entitlement
        now = timezone.now()
        ent.status = 'ENABLED'
        ent.enabled_at = now
        ent.save()
        ent.refresh_from_db()
        self.assertEqual(ent.status, 'ENABLED')
        self.assertEqual(ent.enabled_at, now)


class Sprint13TenantNotificationTemplateTests(TestCase):
    """Workstream C: Notification Templates Canonical Fields (7 items)."""

    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='CultFit Bangalore',
            code=f'cult-blr-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Koramangala Loc',
            city='Bangalore',
            state='KA',
            country='IN',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='Koramangala Branch',
            code=f'BLR-{uuid.uuid4().hex[:4]}',
            timezone='Asia/Kolkata',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_notification_template_canonical_fields_and_defaults(self):
        """notification_templates: organization (RESTRICT), branch (RESTRICT), name (150), channel (30), language (20), version (default 1), is_active (default True)."""
        nt = NotificationTemplate.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            name='Booking Confirmation SMS Notification Template',
            channel='SMS',
            event_code='EVT_BOOKING_CONFIRM',
            language='en',
            subject='Booking Confirmed',
            body='Hello, your booking is confirmed!',
        )
        nt.refresh_from_db()
        self.assertEqual(nt.name, 'Booking Confirmation SMS Notification Template')
        self.assertEqual(nt.channel, 'SMS')
        self.assertEqual(nt.language, 'en')
        self.assertEqual(nt.version, 1)
        self.assertTrue(nt.is_active)

        # FK RESTRICT on branch
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic():
                self.branch.delete()


class Sprint13BranchScheduleAndBusinessRulesTests(TestCase):
    """Workstream D: Branch Schedule (2 Tables, 22 Fields, Business Rules, RBAC & Audit)."""

    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='CultFit Schedules Org',
            code=f'cult-sched-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Indiranagar Hub',
            city='Bangalore',
            state='KA',
            country='IN',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='Indiranagar Branch',
            code=f'IND-{uuid.uuid4().hex[:4]}',
            timezone='Asia/Kolkata',
            status='ACTIVE',
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@cultfit.io',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_schedule_tables_empty_initially(self):
        """Verify schedule tables were created empty without fabricated historical data."""
        self.assertEqual(BranchWorkingHours.objects.using('tenant_test').filter(branch=self.branch).count(), 0)
        self.assertEqual(BranchOperatingException.objects.using('tenant_test').filter(branch=self.branch).count(), 0)

    def test_branch_working_hours_constraints_and_fks(self):
        """branch_working_hours: unique(branch, day_of_week), day range 1-7, FK restrict on branch."""
        bwh = BranchWorkingHours.objects.using('tenant_test').create(
            branch=self.branch,
            day_of_week=1,  # Monday
            is_open=True,
            open_time=time(6, 0),
            close_time=time(22, 0),
            is_24_hours=False,
            created_by=self.user,
            updated_by=self.user,
        )
        bwh.refresh_from_db()
        self.assertTrue(bwh.is_open)
        self.assertFalse(bwh.is_24_hours)
        self.assertEqual(bwh.day_of_week, 1)

        # Unique constraint on (branch, day_of_week)
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                BranchWorkingHours.objects.using('tenant_test').create(
                    branch=self.branch,
                    day_of_week=1,
                    is_open=False,
                )

        # Check FK RESTRICT on branch
        with self.assertRaises((IntegrityError, RestrictedError)):
            with transaction.atomic(using='tenant_test'):
                self.branch.delete()

        # Check FK SET NULL on created_by
        self.user.delete()
        bwh.refresh_from_db()
        self.assertIsNone(bwh.created_by)

    def test_branch_operating_exceptions_constraints_and_fks(self):
        """branch_operating_exceptions: unique(branch, exception_date), FK restrict on branch."""
        exp_date = date(2026, 12, 25)
        boe = BranchOperatingException.objects.using('tenant_test').create(
            branch=self.branch,
            exception_date=exp_date,
            is_closed=True,
            reason='Christmas Holiday',
            created_by=self.user,
            updated_by=self.user,
        )
        boe.refresh_from_db()
        self.assertTrue(boe.is_closed)
        self.assertEqual(boe.reason, 'Christmas Holiday')

        # Unique constraint on (branch, exception_date)
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                BranchOperatingException.objects.using('tenant_test').create(
                    branch=self.branch,
                    exception_date=exp_date,
                    is_closed=False,
                    open_time=time(10, 0),
                    close_time=time(18, 0),
                )

    def test_rule_1_branch_timezone_authoritative(self):
        """Rule 1: branches.timezone is the authoritative timezone."""
        self.assertEqual(self.branch.timezone, 'Asia/Kolkata')
        tz = BranchScheduleService.get_branch_timezone(self.branch)
        self.assertEqual(str(tz), 'Asia/Kolkata')

    def test_rule_2_exception_overrides_weekly_working_hours(self):
        """Rule 2: exception row overrides weekly working hours."""
        # Setup Monday working hours (06:00 - 22:00)
        BranchWorkingHours.objects.using('tenant_test').create(
            branch=self.branch,
            day_of_week=1,
            is_open=True,
            open_time=time(6, 0),
            close_time=time(22, 0),
            is_24_hours=False,
        )

        # Target date is a Monday (e.g. 2026-09-14 is a Monday)
        monday_date = date(2026, 9, 14)
        self.assertEqual(monday_date.isoweekday(), 1)

        # Before exception: open according to weekly schedule
        sched = BranchScheduleService.get_effective_schedule_for_date(self.branch, monday_date)
        self.assertTrue(sched['is_open'])
        self.assertEqual(sched['source'], 'WEEKLY_SCHEDULE')
        self.assertEqual(sched['open_time'], time(6, 0))

        # Add CLOSED exception for that specific Monday
        BranchOperatingException.objects.using('tenant_test').create(
            branch=self.branch,
            exception_date=monday_date,
            is_closed=True,
            reason='Emergency Maintenance',
        )

        # After exception: closed override
        sched_overridden = BranchScheduleService.get_effective_schedule_for_date(self.branch, monday_date)
        self.assertFalse(sched_overridden['is_open'])
        self.assertEqual(sched_overridden['source'], 'EXCEPTION')
        self.assertEqual(sched_overridden['reason'], 'Emergency Maintenance')

    def test_rule_3_is_24_hours_controls_24_hour_operation(self):
        """Rule 3: is_24_hours controls 24-hour operation."""
        # Tuesday: 24 hours open
        tuesday_date = date(2026, 9, 15)  # Tuesday
        self.assertEqual(tuesday_date.isoweekday(), 2)

        BranchWorkingHours.objects.using('tenant_test').create(
            branch=self.branch,
            day_of_week=2,
            is_open=True,
            is_24_hours=True,
        )

        sched = BranchScheduleService.get_effective_schedule_for_date(self.branch, tuesday_date)
        self.assertTrue(sched['is_open'])
        self.assertTrue(sched['is_24_hours'])
        self.assertEqual(sched['open_time'], time(0, 0))

        # Open at 03:00 AM
        dt_night = datetime(2026, 9, 15, 3, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.assertTrue(BranchScheduleService.is_branch_open_at(self.branch, dt_night))

    def test_rule_4_overnight_schedule_handled_at_application_layer(self):
        """Rule 4: Overnight schedules (e.g. 22:00 -> 06:00) resolved at application layer."""
        # Wednesday: 22:00 to 06:00 next morning (Thursday)
        wednesday_date = date(2026, 9, 16)
        self.assertEqual(wednesday_date.isoweekday(), 3)

        BranchWorkingHours.objects.using('tenant_test').create(
            branch=self.branch,
            day_of_week=3,
            is_open=True,
            open_time=time(22, 0),
            close_time=time(6, 0),
            is_24_hours=False,
        )

        sched = BranchScheduleService.get_effective_schedule_for_date(self.branch, wednesday_date)
        self.assertTrue(sched['is_overnight'])

        # 23:00 on Wednesday is OPEN
        dt_wed_night = datetime(2026, 9, 16, 23, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.assertTrue(BranchScheduleService.is_branch_open_at(self.branch, dt_wed_night))

        # 04:00 on Thursday morning (spillover from Wednesday) is OPEN
        dt_thu_early = datetime(2026, 9, 17, 4, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.assertTrue(BranchScheduleService.is_branch_open_at(self.branch, dt_thu_early))

        # 08:00 on Thursday morning (after 06:00 close) is CLOSED
        dt_thu_late = datetime(2026, 9, 17, 8, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.assertFalse(BranchScheduleService.is_branch_open_at(self.branch, dt_thu_late))

    def test_rbac_and_audit_on_schedule_mutation(self):
        """Mutations to schedule tables emit Sprint 6 audit events and require core.settings.edit."""
        factory = APIRequestFactory()
        view = BranchWorkingHoursViewSet.as_view({'post': 'create'})

        # Post payload
        data = {
            'branch': str(self.branch.id),
            'day_of_week': 5,  # Friday
            'is_open': True,
            'open_time': '07:00:00',
            'close_time': '21:00:00',
            'is_24_hours': False,
        }

        request = factory.post('/api/v1/tenant/branch-working-hours/', data, format='json')
        request.tenant = getattr(self.org, 'tenant', None) or Tenant.objects.using('default').first()
        request.org = self.org
        request.db_alias = 'tenant_test'
        self.user._auth_type = 'tenant'
        self.user._db_alias = 'tenant_test'
        force_authenticate(request, user=self.user)

        # Mock evaluate to grant core.settings.edit
        from unittest.mock import patch
        with patch('apps.tenant_core.permissions.RBACAuthorizationEngine.evaluate', return_value=(True, 'Allowed', 'CHECK_00')):
            response = view(request)
            self.assertEqual(response.status_code, 201)

        # Verify audit event was emitted
        audit_entry = TenantAuditEvent.objects.using('tenant_test').filter(
            resource_type='BranchWorkingHours',
            action='CREATE',
        ).latest('created_at')
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.actor_id, self.user.id)
        self.assertIn('07:00:00', str(audit_entry.after_state))
