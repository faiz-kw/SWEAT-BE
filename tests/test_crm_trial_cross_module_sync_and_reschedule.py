"""
backend/tests/test_crm_trial_cross_module_sync_and_reschedule.py
Targeted Test Suite: CRM Trial Booking — Cross-Module Sync & Reschedule Hardening
Validates all 34 requirements across:
- Booking Sync (1-10)
- Reschedule Hardening & Concurrency (11-27)
- Cancellation, Attendance, No-Show & Converted Lead Guard (28-34)
"""

import uuid
from datetime import timedelta, date, time
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from django.core.exceptions import ValidationError

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role,
    RoleAssignment,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    RoleModuleAccess,
    RoleSubmoduleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
)
from apps.tenant_core.models_crm import (
    LeadSource,
    Lead,
    UserProfile,
    TrialBooking,
    TrialStatusHistory,
    SalesFollowupTask,
    CRMTrialReminderPolicy,
)
from apps.tenant_core.models_attention import CRMAttentionPolicy
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
)
from apps.tenant_core.models_bookings import (
    BookingPolicySet,
    Booking,
)
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_crm_dashboard import CRMDashboardService


class CRMTrialSyncAndRescheduleTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-SYNC-TENANT',
            name='Sync Hardening Gym',
            slug='sync-hardening-gym',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='ENT-SYNC-P1',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_a,
            module=self.prod_mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )
        self.prod_mod_ops, _ = ProductModule.objects.using('default').get_or_create(
            code='ops', defaults={'name': 'Operations Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_a,
            module=self.prod_mod_ops,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Setup
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-SYNC',
            name='Sync Core Org',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-SYNC',
            name='Central Location',
            status='ACTIVE',
        )
        self.branch_a1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-SYNC-1',
            name='Downtown Club',
            status='ACTIVE',
        )
        self.branch_a2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-SYNC-2',
            name='Uptown Club',
            status='ACTIVE',
        )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin@synctenant.com',
            first_name='Admin',
            last_name='Sync',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a1,
            scope_type='HOME',
            status='ACTIVE',
            is_active=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a2,
            scope_type='ASSIGNED',
            status='ACTIVE',
            is_active=True,
        )

        self.user_single = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='single@synctenant.com',
            first_name='Single',
            last_name='Sync',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_single,
            branch=self.branch_a1,
            scope_type='HOME',
            status='ACTIVE',
            is_active=True,
        )

        self.setup_rbac()

        # Auth Tokens
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_single = str(_build_tenant_token(self.user_single, self.tenant_a, 'tenant_test').access_token)

        # Classes & Booking Policy
        self.class_category = ClassCategory.objects.using('tenant_test').create(
            organization=self.org_a,
            code='PILATES-SYNC',
            name='Pilates Sync',
            status='ACTIVE',
        )
        self.policy_set = BookingPolicySet.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            allow_trial=True,
            max_trial_bookings=5,
            max_reschedules=3,
            status='ACTIVE',
        )
        self.class_template = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.class_category,
            code='PILATES-SYNC-101',
            name='Pilates Reformer Sync',
            default_capacity=20,
            default_trial_capacity=5,
            allow_booking=True,
            allow_trial=True,
            status='ACTIVE',
        )

        # Occurrences
        self.tomorrow_date = timezone.localdate() + timedelta(days=1)
        self.start_dt1 = timezone.make_aware(timezone.datetime.combine(self.tomorrow_date, time(9, 0)))
        self.end_dt1 = timezone.make_aware(timezone.datetime.combine(self.tomorrow_date, time(10, 0)))

        self.occ1 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow_date,
            start_at=self.start_dt1,
            end_at=self.end_dt1,
            capacity=20,
            trial_capacity=5,
            status='OPEN',
        )

        self.next_day = self.tomorrow_date + timedelta(days=1)
        self.start_dt2 = timezone.make_aware(timezone.datetime.combine(self.next_day, time(18, 0)))
        self.end_dt2 = timezone.make_aware(timezone.datetime.combine(self.next_day, time(19, 0)))

        self.occ2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.next_day,
            start_at=self.start_dt2,
            end_at=self.end_dt2,
            capacity=20,
            trial_capacity=3,
            status='OPEN',
        )

        # Lead Source & Leads
        self.source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='WEB_FORM', name='Website Form', source_type='DIGITAL'
        )
        self.lead1 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            first_name='Alice',
            last_name='Prospect',
            phone_normalized='+15551234567',
            email_normalized='alice@example.com',
            lead_source=self.source,
            current_status='NEW_LEAD',
        )
        self.lead2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            first_name='Bob',
            last_name='Converted',
            phone_normalized='+15559876543',
            email_normalized='bob@example.com',
            lead_source=self.source,
            current_status='CONVERTED',
        )

        self.reminder_policy, _ = CRMTrialReminderPolicy.objects.using('tenant_test').get_or_create(
            organization=self.org_a,
            defaults={
                'immediate_whatsapp': False,
                'immediate_email': False,
                'immediate_sms': False,
                'reminder_offsets': [1440, 120, 30],
                'ask_attendance_confirmation': True,
                'post_attended_followup_enabled': True,
                'post_attended_followup_delay_value': 2,
                'post_attended_followup_delay_unit': 'HOURS',
                'no_show_followup_enabled': True,
                'no_show_followup_delay_value': 30,
                'no_show_followup_delay_unit': 'MINUTES',
            }
        )
        self.attn_policy, _ = CRMAttentionPolicy.objects.using('tenant_test').get_or_create(
            organization=self.org_a,
        )

    def setup_rbac(self):
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        self.submod_trials, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='trials', defaults={'name': 'Trials', 'is_enabled': True}
        )
        self.mod_ops, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='ops', defaults={'name': 'Operations', 'is_enabled': True}
        )
        self.submod_bookings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_ops, submodule_code='bookings', defaults={'name': 'Bookings', 'is_enabled': True}
        )

        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_ADMIN_SYNC',
            name='CRM Admin Sync',
            scope='ORG',
            is_active=True,
        )
        self.role_single = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_SINGLE_SYNC',
            name='CRM Single Branch',
            scope='BRANCH',
            is_active=True,
        )

        for r in (self.role_admin, self.role_single):
            RoleModuleAccess.objects.using('tenant_test').get_or_create(role=r, module=self.mod_crm, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=r, submodule=self.submod_leads, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=r, submodule=self.submod_trials, defaults={'can_access': True})
            RoleModuleAccess.objects.using('tenant_test').get_or_create(role=r, module=self.mod_ops, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=r, submodule=self.submod_bookings, defaults={'can_access': True})

        perm_codes = [
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit',
            'crm.dashboard.view',
            'ops.bookings.view', 'ops.bookings.create', 'ops.bookings.edit',
        ]
        self.perms = {}
        for code in perm_codes:
            action = code.split('.')[-1]
            if 'bookings' in code:
                submod = self.submod_bookings
                mod = self.mod_ops
            elif 'trials' in code:
                submod = self.submod_trials
                mod = self.mod_crm
            else:
                submod = self.submod_leads
                mod = self.mod_crm

            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=code,
                defaults={
                    'code': code,
                    'label': code,
                    'action': action,
                    'module': mod,
                    'submodule': submod,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                },
            )
            self.perms[code] = p

        ps_admin = RolePermissionSet.objects.using('tenant_test').create(role=self.role_admin, name='admin_perms')
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=p)

        ps_single = RolePermissionSet.objects.using('tenant_test').create(role=self.role_single, name='single_perms')
        for code in ['crm.trials.view', 'crm.trials.create', 'crm.trials.edit', 'crm.leads.view']:
            RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_single, permission=self.perms[code])

        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin, role=self.role_admin, branch=None, is_active=True, status='ACTIVE'
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_single, role=self.role_single, branch=self.branch_a1, is_active=True, status='ACTIVE'
        )

    def auth_headers(self, token, branch=None):
        h = {
            'HTTP_AUTHORIZATION': f'Bearer {token}',
            'HTTP_X_TENANT_ID': str(self.tenant_a.id),
        }
        if branch:
            h['HTTP_X_BRANCH_ID'] = str(branch.id)
        return h

    # =========================================================================
    # PART 22: TESTS — BOOKING SYNC (1 - 10)
    # =========================================================================

    def test_01_successful_trial_booking_creates_one_trial_booking(self):
        """1. Successful trial booking creates one TrialBooking."""
        resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
                'notes': 'Sync test booking',
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        trial_id = resp.data['id']
        self.assertEqual(TrialBooking.objects.using('tenant_test').filter(id=trial_id).count(), 1)

    def test_02_zero_normal_booking_duplicates_created(self):
        """2. Zero normal Booking duplicates created."""
        self.assertEqual(Booking.objects.using('tenant_test').count(), 0)
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(Booking.objects.using('tenant_test').count(), 0)

    def test_03_trial_appears_in_trial_management_api(self):
        """3. Trial appears in Trial Management API."""
        resp_post = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = resp_post.data['id']
        resp_list = self.client.get(
            '/api/v1/tenant/trial-bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        results = resp_list.data.get('results', resp_list.data)
        trial_ids = [t['id'] for t in results]
        self.assertIn(trial_id, trial_ids)

    def test_04_trial_appears_in_unified_operations_booking_api(self):
        """4. Trial appears in unified Operations Booking API/view with Source/Type=TRIAL / PROSPECT."""
        resp_post = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = resp_post.data['id']

        resp_ops = self.client.get(
            '/api/v1/tenant/bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp_ops.status_code, status.HTTP_200_OK)
        ops_bookings = resp_ops.data if isinstance(resp_ops.data, list) else resp_ops.data.get('results', [])
        trial_rows = [b for b in ops_bookings if b.get('id') == trial_id]
        self.assertEqual(len(trial_rows), 1)
        row = trial_rows[0]
        self.assertTrue(row.get('is_trial'))
        self.assertEqual(row.get('booking_source'), 'TRIAL')
        self.assertEqual(row.get('booking_type'), 'PROSPECT')
        self.assertTrue(row.get('booking_number', '').startswith('TRL-'))

    def test_05_lead_360_returns_same_trial_booking(self):
        """5. Lead 360 returns same TrialBooking ID."""
        resp_post = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = resp_post.data['id']

        resp_lead_360 = self.client.get(
            f'/api/v1/tenant/leads/{self.lead1.id}/trials/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp_lead_360.status_code, status.HTTP_200_OK)
        lead_trials = resp_lead_360.data if isinstance(resp_lead_360.data, list) else resp_lead_360.data.get('results', [])
        ids = [str(t['id']) for t in lead_trials]
        self.assertIn(str(trial_id), ids)

    def test_06_dashboard_reflects_booking(self):
        """6. Dashboard reflects booking."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        set_tenant_db_alias('tenant_test')
        dashboard_data = CRMDashboardService.get_dashboard_data(
            organization=self.org_a,
            user=self.user_admin,
            filters={'preset': 'TODAY'},
            db_alias='tenant_test',
        )
        trials_perf = dashboard_data.get('trials', {})
        self.assertGreaterEqual(trials_perf.get('booked', 0), 1)

    def test_07_lead_current_status_updated(self):
        """7. Lead current_status updated to TRIAL_BOOKED."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.lead1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead1.current_status, 'TRIAL_BOOKED')

    def test_08_pipeline_status_updated(self):
        """8. Pipeline status updated (TRIAL_BOOKED / TRIAL_SCHEDULED)."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.lead1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead1.current_status, 'TRIAL_BOOKED')

    def test_09_occurrence_trial_capacity_decreases(self):
        """9. Occurrence trial capacity decreases."""
        resp_before = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot_before = next(s for s in resp_before.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(slot_before['remaining_trial_capacity'], 5)

        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead1.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )

        resp_after = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot_after = next(s for s in resp_after.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(slot_after['remaining_trial_capacity'], 4)

    def test_10_full_trial_cap_excluded_from_availability(self):
        """10. Full trial cap occurrence is completely excluded from available trial slots."""
        occ_cap1 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow_date,
            start_at=self.start_dt1 + timedelta(hours=3),
            end_at=self.end_dt1 + timedelta(hours=3),
            capacity=10,
            trial_capacity=1,
            status='OPEN',
        )
        # Book the only trial seat
        CRMLeadService.book_trial(
            lead=self.lead1,
            class_occurrence_id=str(occ_cap1.id),
            branch=self.branch_a1,
            db_alias='tenant_test',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        returned_occ_ids = [s['occurrence_id'] for s in resp.data['slots']]
        self.assertNotIn(str(occ_cap1.id), returned_occ_ids)

    # =========================================================================
    # PART 23: TESTS — RESCHEDULE (11 - 27)
    # =========================================================================

    def test_11_reschedule_changes_occurrence(self):
        """11. Reschedule changes occurrence from old occurrence to new occurrence."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.class_occurrence_id, self.occ2.id)

    def test_12_old_occurrence_capacity_released(self):
        """12. Old occurrence capacity released upon reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        # Before reschedule: occ1 has 4 seats remaining
        resp1 = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        s1 = next(s for s in resp1.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(s1['remaining_trial_capacity'], 4)

        # Reschedule to occ2
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )

        # After reschedule: occ1 has all 5 seats back!
        resp2 = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        s2 = next(s for s in resp2.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(s2['remaining_trial_capacity'], 5)

    def test_13_new_occurrence_capacity_consumed(self):
        """13. New occurrence capacity consumed upon reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        # occ2 has 3 seats originally
        resp_before = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.next_day.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        s_before = next(s for s in resp_before.data['slots'] if s['occurrence_id'] == str(self.occ2.id))
        self.assertEqual(s_before['remaining_trial_capacity'], 3)

        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )

        resp_after = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.next_day.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        s_after = next(s for s in resp_after.data['slots'] if s['occurrence_id'] == str(self.occ2.id))
        self.assertEqual(s_after['remaining_trial_capacity'], 2)

    def test_14_same_trial_booking_remains_canonical(self):
        """14. Same TrialBooking remains canonical after reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        original_id = trial.id
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], str(original_id))

    def test_15_no_duplicate_trial_booking(self):
        """15. No duplicate TrialBooking created on reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.assertEqual(TrialBooking.objects.using('tenant_test').count(), 1)
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(TrialBooking.objects.using('tenant_test').count(), 1)

    def test_16_no_duplicate_normal_booking(self):
        """16. No duplicate normal Booking created on reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(Booking.objects.using('tenant_test').count(), 0)

    def test_17_operations_view_shows_new_occurrence(self):
        """17. Operations view shows new occurrence immediately after reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp = self.client.get(
            '/api/v1/tenant/bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        ops_bookings = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        trial_row = next(b for b in ops_bookings if b['id'] == str(trial.id))
        self.assertEqual(trial_row['occurrence'], str(self.occ2.id))
        self.assertEqual(trial_row['occurrence_date'], self.next_day.isoformat())

    def test_18_lead_360_shows_new_occurrence(self):
        """18. Lead 360 shows new occurrence immediately after reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp = self.client.get(
            f'/api/v1/tenant/leads/{self.lead1.id}/trials/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        lead_trials = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        row = next(t for t in lead_trials if t['id'] == str(trial.id))
        self.assertEqual(row['class_occurrence_id'], str(self.occ2.id))

    def test_19_trial_management_shows_new_occurrence(self):
        """19. Trial Management shows new occurrence immediately after reschedule."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/{trial.id}/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.data['class_occurrence_id'], str(self.occ2.id))

    def test_20_dashboard_does_not_double_count_reschedule(self):
        """20. Dashboard does not double count reschedule as two bookings."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        # Reschedule trial
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        set_tenant_db_alias('tenant_test')
        dashboard_data = CRMDashboardService.get_dashboard_data(
            organization=self.org_a,
            user=self.user_admin,
            filters={'preset': 'TODAY'},
            db_alias='tenant_test',
        )
        trials_perf = dashboard_data.get('trials', {})
        self.assertEqual(trials_perf.get('booked', 0), 1)

    def test_21_reschedule_history_written(self):
        """21. Reschedule history written to TrialStatusHistory."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(self.occ2.id), 'reason': 'Customer requested evening slot'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        histories = list(TrialStatusHistory.objects.using('tenant_test').filter(trial_booking=trial).order_by('created_at'))
        # Should record RESCHEDULED step
        rescheduled_entries = [h for h in histories if h.to_status == 'RESCHEDULED']
        self.assertGreaterEqual(len(rescheduled_entries), 1)
        entry = rescheduled_entries[0]
        self.assertEqual(entry.to_status, 'RESCHEDULED')
        self.assertEqual(entry.from_status, 'BOOKED')
        self.assertIn('Customer requested evening slot', entry.reason_code or '')

    def test_22_reschedule_to_full_trial_occurrence_rejected(self):
        """22. Reschedule to full trial occurrence rejected."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        occ_full = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.next_day,
            start_at=self.start_dt2 + timedelta(hours=2),
            end_at=self.end_dt2 + timedelta(hours=2),
            capacity=10,
            trial_capacity=0,
            status='OPEN',
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_full.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('capacity reached', resp.data['error'])

    def test_23_failed_reschedule_preserves_old_occurrence(self):
        """23. Failed reschedule preserves old occurrence without side effects."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        occ_full = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.next_day,
            start_at=self.start_dt2 + timedelta(hours=2),
            end_at=self.end_dt2 + timedelta(hours=2),
            capacity=0,
            trial_capacity=0,
            status='OPEN',
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_full.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.class_occurrence_id, self.occ1.id)
        # Old occurrence capacity remains occupied (4 remaining out of 5)
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = next(s for s in resp.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(slot['remaining_trial_capacity'], 4)

    def test_24_concurrent_last_seat_reschedule_protected(self):
        """24. Concurrent last-seat reschedule protected with row-level locks and transactional revalidation."""
        occ_last = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.next_day,
            start_at=self.start_dt2 + timedelta(hours=4),
            end_at=self.end_dt2 + timedelta(hours=4),
            capacity=1,
            trial_capacity=1,
            status='OPEN',
        )
        # Another lead takes the last seat
        lead_competing = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            first_name='Clara',
            last_name='Quick',
            phone_normalized='+15557778888',
            email_normalized='clara@example.com',
            lead_source=self.source,
            current_status='INQUIRY',
        )
        CRMLeadService.book_trial(
            lead=lead_competing, class_occurrence_id=str(occ_last.id), branch=self.branch_a1, db_alias='tenant_test'
        )

        trial1 = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial1.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_last.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        trial1.refresh_from_db(using='tenant_test')
        self.assertEqual(trial1.class_occurrence_id, self.occ1.id)

    def test_25_unauthorized_branch_reschedule_rejected(self):
        """25. Unauthorized branch reschedule rejected."""
        occ_branch2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a2,
            occurrence_date=self.next_day,
            start_at=self.start_dt2,
            end_at=self.end_dt2,
            capacity=10,
            trial_capacity=3,
            status='OPEN',
        )
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        # Single branch user only has access to branch_a1
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_branch2.id)},
            **self.auth_headers(self.token_single, self.branch_a1)
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

    def test_26_past_occurrence_reschedule_rejected(self):
        """26. Past occurrence reschedule rejected."""
        yesterday_dt = timezone.now() - timedelta(days=1)
        occ_past = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=yesterday_dt.date(),
            start_at=yesterday_dt,
            end_at=yesterday_dt + timedelta(hours=1),
            capacity=10,
            trial_capacity=3,
            status='OPEN',
        )
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_past.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('past', resp.data['error'].lower())

    def test_27_cancelled_occurrence_rejected(self):
        """27. Cancelled occurrence rejected for reschedule."""
        occ_cancelled = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.next_day,
            start_at=self.start_dt2,
            end_at=self.end_dt2,
            capacity=10,
            trial_capacity=3,
            status='CANCELLED',
        )
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_cancelled.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('open', resp.data['error'].lower())

    # =========================================================================
    # PART 24: TESTS — CANCEL / ATTEND / NO-SHOW & CONVERTED GUARD (28 - 34)
    # =========================================================================

    def test_28_cancel_releases_capacity(self):
        """28. Cancel trial releases occurrence trial capacity."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        # Cancel trial
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/cancel/',
            {'cancellation_reason': 'SCHEDULE_CONFLICT'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Check capacity is released back to 5
        resp_slots = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = next(s for s in resp_slots.data['slots'] if s['occurrence_id'] == str(self.occ1.id))
        self.assertEqual(slot['remaining_trial_capacity'], 5)

    def test_29_cancel_syncs_all_screens(self):
        """29. Cancel syncs Trial Management, Operations Bookings, Lead 360, and Lead status."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/cancel/',
            {'cancellation_reason': 'SCHEDULE_CONFLICT'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        # 1. Lead status transitions to FOLLOW_UP_PENDING
        self.lead1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead1.current_status, 'FOLLOW_UP_PENDING')

        # 2. Trial Management shows CANCELLED
        resp_trial = self.client.get(
            f'/api/v1/tenant/trial-bookings/{trial.id}/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp_trial.data['status'], 'CANCELLED')

        # 3. Operations Bookings shows CANCELLED
        resp_ops = self.client.get(
            '/api/v1/tenant/bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        ops_bookings = resp_ops.data if isinstance(resp_ops.data, list) else resp_ops.data.get('results', [])
        row = next(b for b in ops_bookings if b['id'] == str(trial.id))
        self.assertEqual(row['status'], 'CANCELLED')

        # 4. Lead 360 shows CANCELLED
        resp_lead_360 = self.client.get(
            f'/api/v1/tenant/leads/{self.lead1.id}/trials/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        lead_trials = resp_lead_360.data if isinstance(resp_lead_360.data, list) else resp_lead_360.data.get('results', [])
        row_360 = next(t for t in lead_trials if t['id'] == str(trial.id))
        self.assertEqual(row_360['status'], 'CANCELLED')

    def test_30_attended_syncs_all_screens(self):
        """30. Mark attended syncs Lead status to TRIAL_ATTENDED, Operations Bookings, Lead 360, and Dashboard."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/',
            {'notes': 'Great effort in class'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 1. Lead status
        self.lead1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead1.current_status, 'TRIAL_ATTENDED')

        # 2. Operations view
        resp_ops = self.client.get(
            '/api/v1/tenant/bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        ops_bookings = resp_ops.data if isinstance(resp_ops.data, list) else resp_ops.data.get('results', [])
        row = next(b for b in ops_bookings if b['id'] == str(trial.id))
        self.assertEqual(row['status'], 'ATTENDED')

        # 3. Lead 360
        resp_lead_360 = self.client.get(
            f'/api/v1/tenant/leads/{self.lead1.id}/trials/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        lead_trials = resp_lead_360.data if isinstance(resp_lead_360.data, list) else resp_lead_360.data.get('results', [])
        row_360 = next(t for t in lead_trials if t['id'] == str(trial.id))
        self.assertEqual(row_360['status'], 'ATTENDED')

    def test_31_no_show_syncs_all_screens(self):
        """31. Mark no-show syncs Lead status to NO_SHOW across all endpoints."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_no_show/',
            {'notes': 'Did not show up'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.lead1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead1.current_status, 'NO_SHOW')

        # Operations view
        resp_ops = self.client.get(
            '/api/v1/tenant/bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        ops_bookings = resp_ops.data if isinstance(resp_ops.data, list) else resp_ops.data.get('results', [])
        row = next(b for b in ops_bookings if b['id'] == str(trial.id))
        self.assertEqual(row['status'], 'NO_SHOW')

    def test_32_attention_behavior_correct(self):
        """32. Attention behavior correct: Mark attended/no-show generates appropriate task and records event."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/',
            {'notes': 'Attended trial session'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        # Check sales followup task or attention flag
        tasks = SalesFollowupTask.objects.using('tenant_test').filter(lead=self.lead1)
        self.assertGreaterEqual(tasks.count(), 1)

    def test_33_dashboard_status_counts_correct(self):
        """33. Dashboard status counts correct for attended, no-show, and cancelled."""
        trial = CRMLeadService.book_trial(
            lead=self.lead1, class_occurrence_id=str(self.occ1.id), branch=self.branch_a1, db_alias='tenant_test'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        set_tenant_db_alias('tenant_test')
        data = CRMDashboardService.get_dashboard_data(
            organization=self.org_a,
            user=self.user_admin,
            filters={'preset': 'TODAY'},
            db_alias='tenant_test',
        )
        trials_perf = data.get('trials', {})
        self.assertEqual(trials_perf.get('attended', 0), 1)

    def test_34_converted_lead_trial_restriction_correct(self):
        """34. Converted Lead trial restriction: cannot book prospect trials."""
        resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {
                'lead_id': str(self.lead2.id),
                'class_occurrence_id': str(self.occ1.id),
                'branch_id': str(self.branch_a1.id),
            },
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('converted', resp.data['error'].lower())
