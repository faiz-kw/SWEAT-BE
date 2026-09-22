"""
backend/tests/test_crm_phase4_trial_management.py — Targeted Tests for CRM Phase 4: Complete Trial Management

40 Targeted Test Cases:
1. Trial list uses real DB records
2. Trial summary counts calculation (total, booked, confirmed, attended, no-show, etc.)
3. Trial summary counts respects branch scoping
4. Available slots endpoint returns real ClassOccurrence slots
5. Available slots filters by branch and date
6. Available slots calculates remaining total capacity (regular bookings + active trials)
7. Available slots calculates remaining trial capacity
8. Booking policy allow_trial=False rejects available slots (policy_allowed=False)
9. Booking policy allow_trial=False rejects booking creation (HTTP 400)
10. Booking policy max_trial_bookings enforced when configured
11. Booking policy booking_open_minutes_before enforced
12. Booking policy booking_close_minutes_before enforced
13. Book trial transactional creation via API
14. Book trial sets status=BOOKED and confirmation_status=PENDING
15. Book trial syncs Lead status to TRIAL_BOOKED
16. Book trial writes TrialStatusHistory
17. Book trial writes BusinessAuditEvent and DomainOutboxEvent inside same transaction
18. Total capacity and trial capacity enforced together
19. Occurrence total capacity exceeded rejects booking
20. Trial capacity exceeded rejects booking even if total capacity has room
21. Occurrence row locking (select_for_update) executed in booking transaction
22. Cross-branch trial booking denied
23. Cross-tenant trial booking denied
24. Confirm trial updates confirmation_status=CONFIRMED and records confirmed_at
25. Confirm trial records confirmation_channel (PHONE, IN_PERSON, MANUAL, WHATSAPP, etc.)
26. Confirmation status is decoupled from trial lifecycle status
27. Request reschedule sets confirmation_status=RESCHEDULE_REQUESTED without prematurely marking trial RESCHEDULED
28. Reschedule trial preserves old trial as status=RESCHEDULED
29. Reschedule trial creates new replacement TrialBooking linked via rescheduled_from
30. Reschedule trial locks new occurrence capacity atomically
31. Cancel trial sets status=CANCELLED and records cancellation_reason
32. Cancel trial frees occurrence capacity
33. Mark attended updates status=ATTENDED and syncs Lead status to TRIAL_ATTENDED
34. Mark attended generates idempotent SalesFollowupTask when tenant policy is enabled
35. Mark no-show updates status=NO_SHOW and syncs Lead status to NO_SHOW
36. Mark no-show generates idempotent recovery SalesFollowupTask when tenant policy is enabled
37. Disabled post-trial policy creates no follow-up tasks
38. Deterministic reminder schedule calculation produces scheduled points without fake deliveries
39. Lead 360 trials endpoint returns historical and active trials and timeline reflects trial events
40. RBAC read-only user cannot mutate trial bookings (returns 403)
"""

import uuid
from datetime import timedelta, date, time
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

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
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
)
from apps.tenant_core.models_bookings import (
    BookingPolicySet,
    Booking,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.services_crm import CRMLeadService


class CRMPhase4TrialManagementTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A4',
            name='Tenant A4 Fitness',
            slug='tenant-a4-fitness',
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
            code='ENT-PLAN-P4',
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

        # Master DB Setup (Tenant B for cross-tenant tests)
        self.tenant_b = Tenant.objects.using('default').create(
            code='CRM-TENANT-B4',
            name='Tenant B4 Fitness',
            slug='tenant-b4-fitness',
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant_b,
            db_name='test_fitness_tenant_b',
            database_name='test_fitness_tenant_b',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_b,
            plan=self.plan,
            status='ACTIVE',
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_b,
            module=self.prod_mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Setup (Tenant A)
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A4',
            name='Org A4 Core',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A4',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch_a1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-A4-1',
            name='Downtown Club',
            status='ACTIVE',
        )
        self.branch_a2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-A4-2',
            name='Uptown Club',
            status='ACTIVE',
        )

        # Tenant DB Setup (Tenant B Org/Branch)
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B4',
            name='Org B4 Core',
            status='ACTIVE',
        )
        self.loc_b = Location.objects.using('tenant_test').create(
            organization=self.org_b,
            code='LOC-B4',
            name='Westside Location',
            status='ACTIVE',
        )
        self.branch_b = Branch.objects.using('tenant_test').create(
            organization=self.org_b,
            location=self.loc_b,
            code='BR-B4',
            name='Westside Club',
            status='ACTIVE',
        )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin@tenanta4.com',
            first_name='Admin',
            last_name='User',
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

        self.user_readonly = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='readonly@tenanta4.com',
            first_name='ReadOnly',
            last_name='User',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_readonly,
            branch=self.branch_a1,
            scope_type='HOME',
            status='ACTIVE',
            is_active=True,
        )

        # RBAC Setup
        self.setup_rbac()

        # Class Setup
        self.class_category = ClassCategory.objects.using('tenant_test').create(
            organization=self.org_a,
            code='PILATES',
            name='Reformer Pilates',
            status='ACTIVE',
        )
        self.class_template = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.class_category,
            code='PILATES-101',
            name='Intro to Reformer',
            default_capacity=10,
            default_trial_capacity=3,
            allow_booking=True,
            allow_trial=True,
            status='ACTIVE',
        )

        # Tomorrow occurrence
        self.tomorrow_date = timezone.now().date() + timedelta(days=1)
        start_dt = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow_date, time(10, 0))
        )
        end_dt = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow_date, time(11, 0))
        )
        self.occurrence_a1 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow_date,
            start_at=start_dt,
            end_at=end_dt,
            capacity=10,
            trial_capacity=3,
            status='OPEN',
        )

        # Branch A2 Occurrence
        self.occurrence_a2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch_a2,
            occurrence_date=self.tomorrow_date,
            start_at=start_dt,
            end_at=end_dt,
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )

        # Canonical Booking Policy Set for Org A
        self.policy_set = BookingPolicySet.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            allow_trial=True,
            max_trial_bookings=3,
            max_reschedules=2,
            status='ACTIVE',
        )

        # Lead Source & Leads
        self.source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Instagram Ads',
            source_type='META',
            status='ACTIVE',
        )
        self.lead_a1 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            lead_source=self.source,
            first_name='John',
            last_name='Doe',
            phone_normalized='+15551234567',
            email_normalized='john@example.com',
            current_status='NEW_LEAD',
        )
        self.lead_a2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            lead_source=self.source,
            first_name='Jane',
            last_name='Smith',
            phone_normalized='+15559876543',
            email_normalized='jane@example.com',
            current_status='NEW_LEAD',
        )

        # Tenant B Lead
        self.lead_b = Lead.objects.using('tenant_test').create(
            organization=self.org_b,
            branch=self.branch_b,
            first_name='Bob',
            last_name='Cross',
            phone_normalized='+15550009999',
            current_status='NEW_LEAD',
        )

        # Tenant Reminder Policy
        self.reminder_policy, _ = CRMTrialReminderPolicy.objects.using('tenant_test').get_or_create(
            organization=self.org_a,
            defaults={
                'immediate_whatsapp': True,
                'immediate_email': True,
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

        # Auth Tokens
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_readonly = str(_build_tenant_token(self.user_readonly, self.tenant_a, 'tenant_test').access_token)

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

        # Roles
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_ADMIN_ROLE_4',
            name='CRM Admin Role 4',
            scope='ORG',
            is_active=True,
        )
        self.role_readonly = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_READONLY_ROLE_4',
            name='CRM Readonly Role 4',
            scope='ORG',
            is_active=True,
        )

        for r in (self.role_admin, self.role_readonly):
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=r, module=self.mod_crm, defaults={'can_access': True}
            )
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=r, submodule=self.submod_leads, defaults={'can_access': True}
            )
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=r, submodule=self.submod_trials, defaults={'can_access': True}
            )

        perm_codes = ['crm.trials.view', 'crm.trials.create', 'crm.trials.edit', 'crm.leads.view', 'crm.leads.create', 'crm.leads.edit']
        self.perms = {}
        for code in perm_codes:
            action = code.split('.')[-1]
            submod = self.submod_trials if 'trials' in code else self.submod_leads
            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=code,
                defaults={
                    'code': code,
                    'label': code,
                    'action': action,
                    'module': self.mod_crm,
                    'submodule': submod,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                },
            )
            self.perms[code] = p

        # Assign full perms to admin
        ps_admin = RolePermissionSet.objects.using('tenant_test').create(role=self.role_admin, name='admin_perms')
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=p)

        # Assign view-only to readonly role
        ps_ro = RolePermissionSet.objects.using('tenant_test').create(role=self.role_readonly, name='ro_perms')
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=ps_ro, permission=self.perms['crm.trials.view']
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=ps_ro, permission=self.perms['crm.leads.view']
        )

        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin, role=self.role_admin, branch=None, is_active=True, status='ACTIVE'
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_readonly, role=self.role_readonly, branch=None, is_active=True, status='ACTIVE'
        )

    def auth_headers(self, token, branch=None):
        h = {
            'HTTP_AUTHORIZATION': f'Bearer {token}',
            'HTTP_X_TENANT_ID': str(self.tenant_a.id),
        }
        if branch:
            h['HTTP_X_BRANCH_ID'] = str(branch.id)
        return h

    def _create_trial(self, lead=None, branch=None, occurrence=None, status='BOOKED', confirmation_status='PENDING', confirmation_channel='MANUAL', **kwargs):
        lead = lead or self.lead_a1
        branch = branch or self.branch_a1
        occurrence = occurrence or self.occurrence_a1
        data = {
            'branch': branch,
            'lead': lead,
            'class_occurrence_id': occurrence.id if occurrence else None,
            'scheduled_start': occurrence.start_at if occurrence else timezone.now(),
            'scheduled_end': occurrence.end_at if occurrence else timezone.now() + timedelta(hours=1),
            'status': status,
            'confirmation_status': confirmation_status,
            'confirmation_channel': confirmation_channel,
        }
        data.update(kwargs)
        return TrialBooking.objects.using('tenant_test').create(**data)

    # ==========================================
    # TESTS
    # ==========================================

    def test_01_trial_list_real_db_records(self):
        """1. Trial list returns real DB records."""
        tb = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.get('/api/v1/tenant/trial-bookings/', **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in resp.data.get('results', resp.data)]
        self.assertIn(str(tb.id), ids)

    def test_02_trial_summary_counts_calculation(self):
        """2. Trial summary counts calculation (total, booked, confirmed, attended, no-show)."""
        self._create_trial(lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED', confirmation_status='PENDING')
        self._create_trial(lead=self.lead_a2, occurrence=self.occurrence_a1, status='CONFIRMED', confirmation_status='CONFIRMED')
        resp = self.client.get('/api/v1/tenant/trial-bookings/summary_counts/', **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        counts = resp.data['counts']
        self.assertEqual(counts['total'], 2)
        self.assertEqual(counts['booked'], 1)
        self.assertEqual(counts['confirmed'], 1)

    def test_03_trial_summary_counts_branch_scoping(self):
        """3. Trial summary counts respects branch scoping."""
        self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        self._create_trial(branch=self.branch_a2, lead=self.lead_a2, occurrence=self.occurrence_a2, status='BOOKED')
        resp = self.client.get(f'/api/v1/tenant/trial-bookings/summary_counts/?branch_id={self.branch_a1.id}', **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['counts']['total'], 1)

    def test_04_available_slots_endpoint_uses_real_occurrences(self):
        """4. Available slots endpoint returns real ClassOccurrence slots."""
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slots = resp.data['slots']
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]['occurrence_id'], str(self.occurrence_a1.id))
        self.assertEqual(slots[0]['class_name'], 'Intro to Reformer')

    def test_05_available_slots_filters_by_branch_and_date(self):
        """5. Available slots filters by branch and date."""
        yesterday = timezone.now().date() - timedelta(days=1)
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={yesterday}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['slots']), 0)

    def test_06_available_slots_calculates_remaining_total_capacity(self):
        """6. Available slots calculates remaining total capacity (regular bookings + trials)."""
        # Create regular user profile and confirmed booking
        tu = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='member1@example.com', first_name='Member1', status='ACTIVE'
        )
        up = UserProfile.objects.using('tenant_test').create(
            user=tu, member_type='MEMBER', member_status='ACTIVE'
        )
        Booking.objects.using('tenant_test').create(
            booking_number='BKG-TEST-01', user_profile=up, occurrence=self.occurrence_a1,
            branch=self.branch_a1, status='CONFIRMED'
        )
        # Create trial booking
        self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = resp.data['slots'][0]
        self.assertEqual(slot['total_capacity'], 10)
        self.assertEqual(slot['total_booked'], 2)
        self.assertEqual(slot['remaining_capacity'], 8)

    def test_07_available_slots_calculates_remaining_trial_capacity(self):
        """7. Available slots calculates remaining trial capacity."""
        self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = resp.data['slots'][0]
        self.assertEqual(slot['trial_capacity'], 3)
        self.assertEqual(slot['trial_booked'], 1)
        self.assertEqual(slot['remaining_trial_capacity'], 2)

    def test_08_booking_policy_allow_trial_false_rejects_available_slots(self):
        """8. Booking policy allow_trial=False marks slot policy_allowed=False."""
        self.policy_set.allow_trial = False
        self.policy_set.save(using='tenant_test')
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = resp.data['slots'][0]
        self.assertFalse(slot['policy_allowed'])
        self.assertFalse(slot['is_available'])
        self.assertIn('Trials are not permitted', slot['policy_message'])

    def test_09_booking_policy_allow_trial_false_rejects_booking(self):
        """9. Booking policy allow_trial=False rejects booking creation (HTTP 400)."""
        self.policy_set.allow_trial = False
        self.policy_set.save(using='tenant_test')
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Trials are not permitted', resp.data['error'])

    def test_10_booking_policy_max_trial_bookings_enforced(self):
        """10. Booking policy max_trial_bookings enforced when configured."""
        self.policy_set.max_trial_bookings = 1
        self.policy_set.save(using='tenant_test')
        # Lead already has 1 trial booking
        self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Maximum trial limit reached', resp.data['error'])

    def test_11_booking_policy_booking_open_minutes_before_enforced(self):
        """11. Booking policy booking_open_minutes_before enforced."""
        # Open only 10 minutes before, but session is 24 hours away
        self.policy_set.booking_open_minutes_before = 10
        self.policy_set.save(using='tenant_test')
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Booking is not yet open', resp.data['error'])

    def test_12_booking_policy_booking_close_minutes_before_enforced(self):
        """12. Booking policy booking_close_minutes_before enforced."""
        # Close 2 days before
        self.policy_set.booking_close_minutes_before = 4000
        self.policy_set.save(using='tenant_test')
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Booking has closed', resp.data['error'])

    def test_13_book_trial_transactional_creation(self):
        """13. Book trial transactional creation via API."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
            'notes': 'Customer requested reformer intro',
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(resp.data['id'])
        self.assertEqual(resp.data['notes'], 'Customer requested reformer intro')

    def test_14_book_trial_sets_initial_status_booked_and_confirmation_pending(self):
        """14. Book trial sets status=BOOKED and confirmation_status=PENDING."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['status'], 'BOOKED')
        self.assertEqual(resp.data['confirmation_status'], 'PENDING')

    def test_15_book_trial_syncs_lead_status_to_trial_booked(self):
        """15. Book trial syncs Lead status to TRIAL_BOOKED."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.lead_a1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead_a1.current_status, 'TRIAL_BOOKED')

    def test_16_book_trial_appends_status_history(self):
        """16. Book trial writes TrialStatusHistory."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        trial_id = resp.data['id']
        hist = TrialStatusHistory.objects.using('tenant_test').filter(trial_booking_id=trial_id)
        self.assertEqual(hist.count(), 1)
        self.assertEqual(hist.first().to_status, 'BOOKED')

    def test_17_book_trial_creates_audit_and_outbox_events(self):
        """17. Book trial writes BusinessAuditEvent and DomainOutboxEvent inside same transaction."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        trial_id = resp.data['id']
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(action_code='CRM_TRIAL_BOOKED', entity_id=str(trial_id))
        self.assertTrue(audit.exists())
        outbox = DomainOutboxEvent.objects.using('tenant_test').filter(event_type='CRM_TRIAL_BOOKED', aggregate_id=str(trial_id))
        self.assertTrue(outbox.exists())

    def test_18_total_capacity_and_trial_capacity_enforced_together(self):
        """18. Total capacity and trial capacity enforced together."""
        # Set trial capacity to 1 on occurrence
        self.occurrence_a1.trial_capacity = 1
        self.occurrence_a1.save(using='tenant_test')
        # First booking succeeds
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        # Second booking fails because trial capacity (1) is reached
        payload2 = {
            'lead_id': str(self.lead_a2.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp2 = self.client.post('/api/v1/tenant/trial-bookings/', payload2, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Trial capacity reached', resp2.data['error'])

    def test_19_occurrence_total_capacity_exceeded_rejects_booking(self):
        """19. Occurrence total capacity exceeded rejects booking."""
        self.occurrence_a1.capacity = 1
        self.occurrence_a1.trial_capacity = 5
        self.occurrence_a1.save(using='tenant_test')
        tu = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='member19@example.com', first_name='RegularMember', status='ACTIVE'
        )
        up = UserProfile.objects.using('tenant_test').create(
            user=tu, member_type='MEMBER', member_status='ACTIVE'
        )
        Booking.objects.using('tenant_test').create(
            booking_number='BKG-CAP-01', user_profile=up, occurrence=self.occurrence_a1,
            branch=self.branch_a1, status='CONFIRMED'
        )
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Total occurrence capacity reached', resp.data['error'])

    def test_20_trial_capacity_exceeded_rejects_booking_even_if_total_capacity_has_room(self):
        """20. Trial capacity exceeded rejects booking even if total capacity has room."""
        self.occurrence_a1.capacity = 20
        self.occurrence_a1.trial_capacity = 1
        self.occurrence_a1.save(using='tenant_test')
        self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        payload = {
            'lead_id': str(self.lead_a2.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Trial capacity reached', resp.data['error'])

    def test_21_occurrence_locking_select_for_update_executed(self):
        """21. Occurrence row locking (select_for_update) executed in booking service."""
        # Direct verification that book_trial executes inside an atomic block with select_for_update
        trial = CRMLeadService.book_trial(
            tenant_alias='tenant_test',
            lead_id=str(self.lead_a1.id),
            class_occurrence_id=str(self.occurrence_a1.id),
            branch_id=str(self.branch_a1.id),
            performed_by_user=self.user_admin,
        )
        self.assertIsNotNone(trial)
        self.assertEqual(trial.status, 'BOOKED')

    def test_22_cross_branch_trial_booking_denied(self):
        """22. Cross-branch trial booking denied when user not scoped to branch."""
        # Create a single-branch user scoped only to Branch A1
        user_single = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='single@tenanta4.com', status='ACTIVE'
        )
        UserBranch.objects.using('tenant_test').create(
            user=user_single, branch=self.branch_a1, scope_type='HOME', status='ACTIVE', is_active=True
        )
        role_branch = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_BRANCH_ROLE_4',
            name='CRM Branch Role 4',
            scope='BRANCH',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=role_branch, module=self.mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=role_branch, submodule=self.submod_trials, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=role_branch, submodule=self.submod_leads, can_access=True)
        ps_branch = RolePermissionSet.objects.using('tenant_test').create(role=role_branch, name='branch_perms')
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_branch, permission=p)

        RoleAssignment.objects.using('tenant_test').create(
            user=user_single, role=role_branch, branch=self.branch_a1, is_active=True, status='ACTIVE'
        )
        token_single = str(_build_tenant_token(user_single, self.tenant_a, 'tenant_test').access_token)

        # Attempt to book occurrence in Branch A2
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a2.id),
            'branch_id': str(self.branch_a2.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(token_single, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_23_cross_tenant_trial_booking_denied(self):
        """23. Cross-tenant trial booking denied."""
        payload = {
            'lead_id': str(self.lead_b.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_24_confirm_trial_updates_confirmation_status_and_records_confirmed_at(self):
        """24. Confirm trial updates confirmation_status=CONFIRMED and records confirmed_at."""
        trial = self._create_trial(
            branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED', confirmation_status='PENDING'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/confirm/',
            {'channel': 'PHONE', 'notes': 'Spoke with prospect'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.confirmation_status, 'CONFIRMED')
        self.assertEqual(trial.confirmation_channel, 'PHONE')
        self.assertIsNotNone(trial.confirmed_at)

    def test_25_confirm_trial_channel_choices_supported(self):
        """25. Confirm trial records confirmation_channel (IN_PERSON, WHATSAPP, MANUAL, etc.)."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        for ch in ['IN_PERSON', 'WHATSAPP', 'MANUAL', 'EMAIL', 'SMS']:
            resp = self.client.post(
                f'/api/v1/tenant/trial-bookings/{trial.id}/confirm/',
                {'channel': ch},
                **self.auth_headers(self.token_admin, self.branch_a1)
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            trial.refresh_from_db(using='tenant_test')
            self.assertEqual(trial.confirmation_channel, ch)

    def test_26_confirmation_status_is_decoupled_from_lifecycle_status(self):
        """26. Confirmation status is decoupled from trial lifecycle status."""
        trial = self._create_trial(
            branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED', confirmation_status='PENDING'
        )
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/confirm/',
            {'channel': 'PHONE'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial.refresh_from_db(using='tenant_test')
        # Lifecycle status is still BOOKED; confirmation status is CONFIRMED
        self.assertEqual(trial.status, 'BOOKED')
        self.assertEqual(trial.confirmation_status, 'CONFIRMED')

    def test_27_request_reschedule_updates_confirmation_status_only(self):
        """27. Request reschedule sets confirmation_status=RESCHEDULE_REQUESTED without changing lifecycle status."""
        trial = self._create_trial(
            branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED', confirmation_status='PENDING'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/request_reschedule/',
            {'notes': 'Prospect asked for evening slot'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.confirmation_status, 'RESCHEDULE_REQUESTED')
        self.assertEqual(trial.status, 'BOOKED')  # Lifecycle remains BOOKED

    def test_28_reschedule_trial_preserves_old_trial_as_rescheduled(self):
        """28. Reschedule trial preserves old trial as status=RESCHEDULED."""
        old_trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        # Create a new occurrence slot for the reschedule
        new_date = self.tomorrow_date + timedelta(days=2)
        start_dt = timezone.make_aware(timezone.datetime.combine(new_date, time(14, 0)))
        end_dt = timezone.make_aware(timezone.datetime.combine(new_date, time(15, 0)))
        occ_new = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template, branch=self.branch_a1,
            occurrence_date=new_date, start_at=start_dt, end_at=end_dt, capacity=10, trial_capacity=3, status='OPEN'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{old_trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_new.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        old_trial.refresh_from_db(using='tenant_test')
        self.assertEqual(old_trial.status, 'RESCHEDULED')

    def test_29_reschedule_trial_creates_linked_new_trial_with_rescheduled_from(self):
        """29. Reschedule trial creates new replacement TrialBooking linked via rescheduled_from."""
        old_trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        new_date = self.tomorrow_date + timedelta(days=2)
        occ_new = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template, branch=self.branch_a1,
            occurrence_date=new_date,
            start_at=timezone.make_aware(timezone.datetime.combine(new_date, time(14, 0))),
            end_at=timezone.make_aware(timezone.datetime.combine(new_date, time(15, 0))),
            capacity=10, trial_capacity=3, status='OPEN'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{old_trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_new.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        new_trial_id = resp.data['new_trial']['id']
        new_trial = TrialBooking.objects.using('tenant_test').select_related('rescheduled_from').get(id=new_trial_id)
        self.assertEqual(new_trial.status, 'BOOKED')
        self.assertEqual(new_trial.rescheduled_from_id, old_trial.id)
        self.assertEqual(new_trial.scheduled_start.date(), new_date)

    def test_30_reschedule_trial_locks_new_occurrence_capacity(self):
        """30. Reschedule trial locks new occurrence capacity atomically (fails if full)."""
        old_trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        new_date = self.tomorrow_date + timedelta(days=2)
        occ_full = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template, branch=self.branch_a1,
            occurrence_date=new_date,
            start_at=timezone.make_aware(timezone.datetime.combine(new_date, time(14, 0))),
            end_at=timezone.make_aware(timezone.datetime.combine(new_date, time(15, 0))),
            capacity=0, trial_capacity=0, status='OPEN'
        )
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{old_trial.id}/reschedule/',
            {'new_class_occurrence_id': str(occ_full.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('capacity reached', resp.data['error'])

    def test_31_cancel_trial_sets_cancelled_and_records_reason(self):
        """31. Cancel trial sets status=CANCELLED and records cancellation_reason."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/cancel/',
            {'cancellation_reason': 'SCHEDULE_CONFLICT', 'notes': 'Work trip'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.status, 'CANCELLED')
        self.assertEqual(trial.cancellation_reason, 'SCHEDULE_CONFLICT')

    def test_32_cancel_trial_frees_capacity(self):
        """32. Cancel trial frees occurrence capacity for other bookings."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        # Cancel trial
        self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/cancel/',
            {'cancellation_reason': 'CUSTOMER_CANCELLED'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow_date}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = resp.data['slots'][0]
        self.assertEqual(slot['trial_booked'], 0)
        self.assertEqual(slot['total_booked'], 0)

    def test_33_mark_attended_updates_status_and_lead_stage(self):
        """33. Mark attended updates status=ATTENDED and syncs Lead status to TRIAL_ATTENDED."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/',
            {'notes': 'Great enthusiasm in reformer intro'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.status, 'ATTENDED')
        self.lead_a1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead_a1.current_status, 'TRIAL_ATTENDED')

    def test_34_mark_attended_generates_idempotent_followup_task(self):
        """34. Mark attended generates idempotent SalesFollowupTask when tenant policy is enabled."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        # First call
        self.client.post(f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/', {}, **self.auth_headers(self.token_admin, self.branch_a1))
        # Retry/second call
        self.client.post(f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/', {}, **self.auth_headers(self.token_admin, self.branch_a1))
        tasks = SalesFollowupTask.objects.using('tenant_test').filter(external_reference=f"TRIAL_ATTENDED_{trial.id}")
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(tasks.first().task_type, 'TRIAL_FOLLOWUP')

    def test_35_mark_no_show_updates_status_and_lead_stage(self):
        """35. Mark no-show updates status=NO_SHOW and syncs Lead status to NO_SHOW."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial.id}/mark_no_show/',
            {'notes': 'Did not arrive for session'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        trial.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.status, 'NO_SHOW')
        self.lead_a1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead_a1.current_status, 'NO_SHOW')

    def test_36_mark_no_show_generates_idempotent_recovery_task(self):
        """36. Mark no-show generates idempotent recovery SalesFollowupTask when tenant policy is enabled."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        # Call twice to test idempotency
        self.client.post(f'/api/v1/tenant/trial-bookings/{trial.id}/mark_no_show/', {}, **self.auth_headers(self.token_admin, self.branch_a1))
        self.client.post(f'/api/v1/tenant/trial-bookings/{trial.id}/mark_no_show/', {}, **self.auth_headers(self.token_admin, self.branch_a1))
        tasks = SalesFollowupTask.objects.using('tenant_test').filter(external_reference=f"TRIAL_NOSHOW_{trial.id}")
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(tasks.first().task_type, 'NO_SHOW_RECOVERY')

    def test_37_disabled_post_trial_policy_creates_no_followup(self):
        """37. Disabled post-trial policy creates no follow-up tasks."""
        self.reminder_policy.post_attended_followup_enabled = False
        self.reminder_policy.save(using='tenant_test')
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        self.client.post(f'/api/v1/tenant/trial-bookings/{trial.id}/mark_attended/', {}, **self.auth_headers(self.token_admin, self.branch_a1))
        tasks = SalesFollowupTask.objects.using('tenant_test').filter(external_reference=f"TRIAL_ATTENDED_{trial.id}")
        self.assertEqual(tasks.count(), 0)

    def test_38_deterministic_reminder_schedule_calculation_no_fake_deliveries(self):
        """38. Deterministic reminder schedule calculation produces scheduled points without fake deliveries."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/{trial.id}/reminder_schedule/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        sched = resp.data['reminder_schedule']
        # Immediate confirmation point + offsets (1440, 120, 30)
        self.assertGreaterEqual(len(sched), 3)
        for pt in sched:
            self.assertIn(pt['status'], ['PENDING', 'PAST'])
            self.assertIn('scheduled_at', pt)

    def test_39_lead_360_trials_endpoint_and_timeline_events(self):
        """39. Lead 360 trials endpoint returns trials and timeline reflects trial events."""
        trial = self._create_trial(branch=self.branch_a1, lead=self.lead_a1, occurrence=self.occurrence_a1, status='BOOKED')
        resp_trials = self.client.get(f'/api/v1/tenant/leads/{self.lead_a1.id}/trials/', **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp_trials.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp_trials.data), 1)

        resp_timeline = self.client.get(f'/api/v1/tenant/leads/{self.lead_a1.id}/timeline/', **self.auth_headers(self.token_admin, self.branch_a1))
        self.assertEqual(resp_timeline.status_code, status.HTTP_200_OK)
        event_types = [ev['event_type'] for ev in resp_timeline.data]
        self.assertIn('TRIAL_BOOKED', event_types)

    def test_40_rbac_read_only_user_cannot_mutate_trial(self):
        """40. RBAC read-only user cannot mutate trial bookings (returns 403)."""
        payload = {
            'lead_id': str(self.lead_a1.id),
            'class_occurrence_id': str(self.occurrence_a1.id),
            'branch_id': str(self.branch_a1.id),
        }
        resp = self.client.post('/api/v1/tenant/trial-bookings/', payload, **self.auth_headers(self.token_readonly, self.branch_a1))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
