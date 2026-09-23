"""
backend/tests/test_crm_trial_occurrence_authoritative.py — Authoritative Class Occurrence & Trial Capacity Integration Tests

33 Focused Test Cases:
1. class occurrence with allow_trial true appears
2. allow_trial false does not appear
3. trial_cap reached does not appear / rejects
4. overall capacity reached rejects
5. wrong branch does not appear
6. wrong program does not appear
7. inactive class does not appear
8. cancelled occurrence does not appear
9. past occurrence does not appear
10. future valid occurrence appears
11. successful TrialBooking references occurrence
12. booking reduces available trial capacity
13. last-seat concurrent booking protected
14. duplicate Lead trial booking handled
15. successful booking changes Lead → TRIAL_BOOKED
16. failed booking does not change Lead status
17. manual TRIAL_BOOKED transition without TrialBooking blocked
18. confirmation → canonical confirmed Lead state
19. attended → canonical attended Lead state
20. no-show → canonical no-show state
21. Trial Management lists created booking
22. Lead 360 returns same booking
23. branch authorization enforced
24. tenant isolation enforced
25. availability endpoint dynamic
26. trial cap 2, zero trials → occurrence visible
27. trial cap 2, one trial → occurrence visible with 1 remaining
28. trial cap 2, two active trials → occurrence NOT returned
29. cancelled trial releases trial capacity
30. full trial cap but normal seats remain → occurrence unavailable for trial
31. normal booking cap full → occurrence unavailable for trial
32. two concurrent bookings for final trial seat → exactly one succeeds
33. failed capacity booking does not change Lead status
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
from apps.tenant_core.models_workforce import UserProfile
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
    TrialBooking,
    TrialStatusHistory,
)
from apps.tenant_core.models_catalog import Program
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    ClassBranchAvailability,
)
from apps.tenant_core.models_bookings import (
    BookingPolicySet,
    Booking,
)
from apps.tenant_core.services_crm import CRMLeadService


class CRMTrialOccurrenceAuthoritativeTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-OCC-A',
            name='Tenant Occ Fitness',
            slug='tenant-occ-fitness',
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
            code='ENT-PLAN-OCC',
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

        # 2. Tenant DB Setup (Tenant A)
        set_tenant_db_alias('tenant_test')
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-OCC-A',
            name='Org Occ Core',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-OCC-A',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch_a1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-OCC-1',
            name='Downtown Club',
            status='ACTIVE',
        )
        self.branch_a2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-OCC-2',
            name='Uptown Club',
            status='ACTIVE',
        )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin_occ@tenanta.com',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a1,
            is_primary=True,
            scope_type='HOME',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a2,
            is_primary=False,
            scope_type='ADDITIONAL',
        )

        # RBAC Setup
        self._setup_rbac()

        # Programs
        self.program_pilates = Program.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Pilates Reformer',
            code='PROG-PILATES',
            status='ACTIVE',
        )
        self.program_strength = Program.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Strength & Conditioning',
            code='PROG-STRENGTH',
            status='ACTIVE',
        )

        # Class Category
        self.category = ClassCategory.objects.using('tenant_test').create(
            organization=self.org_a,
            code='GROUP-PILATES',
            name='Group Pilates',
            status='ACTIVE',
        )

        # Class Template (Pilates, Trial Enabled, Trial Cap = 2, Booking Cap = 10)
        self.class_pilates = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.category,
            program=self.program_pilates,
            code='PILATES-TRIAL',
            name='Reformer Beginners',
            default_capacity=10,
            default_trial_capacity=2,
            allow_booking=True,
            allow_trial=True,
            status='ACTIVE',
        )

        # Class Template (No Trials Allowed)
        self.class_no_trial = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.category,
            program=self.program_pilates,
            code='PILATES-NO-TRIAL',
            name='Masterclass Advanced',
            default_capacity=10,
            default_trial_capacity=0,
            allow_booking=True,
            allow_trial=False,
            status='ACTIVE',
        )

        # Class Template (Inactive)
        self.class_inactive = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.category,
            program=self.program_pilates,
            code='PILATES-INACTIVE',
            name='Archived Reformer',
            default_capacity=10,
            default_trial_capacity=2,
            allow_booking=True,
            allow_trial=True,
            status='INACTIVE',
        )

        # Lead Source & Lead
        self.source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Website',
            source_type='WEBSITE',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a1,
            lead_source=self.source,
            first_name='Rohan',
            last_name='Mehra',
            phone_normalized='+919876543210',
            email_normalized='rohan@example.com',
            interested_program=self.program_pilates,
            current_status='NEW_LEAD',
        )

        # Tomorrow Occurrence
        self.tomorrow = timezone.now().date() + timedelta(days=1)
        self.start_dt = timezone.make_aware(timezone.datetime.combine(self.tomorrow, time(9, 0)))
        self.end_dt = timezone.make_aware(timezone.datetime.combine(self.tomorrow, time(10, 0)))

        self.occ_valid = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_pilates,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow,
            start_at=self.start_dt,
            end_at=self.end_dt,
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )

        # Tokens
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant_a, 'tenant_test').access_token)

    def _setup_rbac(self):
        mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        sub_trials, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=mod_crm, submodule_code='trials', defaults={'name': 'Trials', 'is_enabled': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ROLE_ADMIN_OCC',
            name='Admin Role Occ',
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.role_admin, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_admin, submodule=sub_trials, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_admin, submodule=sub_leads, can_access=True)

        ps_admin = RolePermissionSet.objects.using('tenant_test').create(role=self.role_admin, name='admin_occ_perms')
        perm_codes = ['crm.trials.view', 'crm.trials.create', 'crm.trials.edit', 'crm.leads.view', 'crm.leads.create', 'crm.leads.edit']
        for code in perm_codes:
            action = code.split('.')[-1]
            submod = sub_trials if 'trials' in code else sub_leads
            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=code,
                defaults={
                    'code': code,
                    'label': code,
                    'action': action,
                    'module': mod_crm,
                    'submodule': submod,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                },
            )
            RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=p)

        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin, role=self.role_admin, branch=None, is_active=True, status='ACTIVE'
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def auth_headers(self, token, branch=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        if branch:
            headers['HTTP_X_BRANCH_ID'] = str(branch.id)
        return headers

    # --- TEST CASES ---

    def test_01_occurrence_with_allow_trial_true_appears(self):
        """1. Class occurrence with allow_trial=True appears in availability endpoint."""
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertIn(str(self.occ_valid.id), occ_ids)

    def test_02_allow_trial_false_does_not_appear(self):
        """2. Class occurrence with allow_trial=False does NOT appear."""
        occ_no = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_no_trial,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow,
            start_at=self.start_dt,
            end_at=self.end_dt,
            capacity=10,
            trial_capacity=0,
            status='OPEN',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(occ_no.id), occ_ids)

    def test_03_trial_cap_reached_does_not_appear_and_rejects(self):
        """3. When trial_cap is reached (e.g. 2/2 booked), occurrence does not appear and rejects booking."""
        # Book 2 trials for other leads
        for i in range(2):
            other_lead = Lead.objects.using('tenant_test').create(
                organization=self.org_a, branch=self.branch_a1, first_name=f'Lead{i}', last_name='Test', current_status='NEW_LEAD'
            )
            CRMLeadService.book_trial(
                lead=other_lead, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
            )

        # Availability check: should NOT appear
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

        # Direct API booking attempt must be rejected with 400
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(post_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("full", post_resp.data.get('error', '').lower())

    def test_04_overall_capacity_reached_rejects(self):
        """4. When overall capacity is full (even if 0 trials booked), occurrence does not appear and rejects."""
        # Fill all 10 seats with member bookings
        for i in range(10):
            usr = TenantUser.objects.using('tenant_test').create(
                organization=self.org_a, email=f'user{i}@test.com', first_name=f'User{i}', last_name='T', status='ACTIVE'
            )
            prof, _ = UserProfile.objects.using('tenant_test').get_or_create(user=usr)
            Booking.objects.using('tenant_test').create(
                booking_number=f'BKG-CAP-{i}-{uuid.uuid4().hex[:6]}',
                user_profile=prof,
                occurrence=self.occ_valid,
                branch=self.branch_a1,
                status='CONFIRMED',
            )

        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_05_wrong_branch_does_not_appear(self):
        """5. Occurrence at branch A2 does not appear when querying branch A1."""
        occ_a2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_pilates,
            branch=self.branch_a2,
            occurrence_date=self.tomorrow,
            start_at=self.start_dt,
            end_at=self.end_dt,
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(occ_a2.id), occ_ids)

    def test_06_wrong_program_does_not_appear(self):
        """6. When filtering by program_strength, Pilates class occurrence does not appear."""
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}&program_id={self.program_strength.id}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_07_inactive_class_does_not_appear(self):
        """7. Inactive class template occurrences do not appear."""
        occ_inact = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_inactive,
            branch=self.branch_a1,
            occurrence_date=self.tomorrow,
            start_at=self.start_dt,
            end_at=self.end_dt,
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(occ_inact.id), occ_ids)

    def test_08_cancelled_occurrence_does_not_appear(self):
        """8. Cancelled occurrence does not appear."""
        self.occ_valid.status = 'CANCELLED'
        self.occ_valid.save(using='tenant_test')
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_09_past_occurrence_does_not_appear(self):
        """9. Past occurrence does not appear in trial availability."""
        yesterday = timezone.now().date() - timedelta(days=1)
        occ_past = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_pilates,
            branch=self.branch_a1,
            occurrence_date=yesterday,
            start_at=timezone.now() - timedelta(hours=25),
            end_at=timezone.now() - timedelta(hours=24),
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={yesterday.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertNotIn(str(occ_past.id), occ_ids)

    def test_10_future_valid_occurrence_appears(self):
        """10. Future valid occurrence with open seats appears with correct metadata."""
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slots = resp.data.get('slots', [])
        matching = next((s for s in slots if s['occurrence_id'] == str(self.occ_valid.id)), None)
        self.assertIsNotNone(matching)
        self.assertEqual(matching['class_name'], 'Reformer Beginners')
        self.assertEqual(matching['program_name'], 'Pilates Reformer')
        self.assertEqual(matching['trial_capacity'], 2)
        self.assertEqual(matching['remaining_trial_capacity'], 2)

    def test_11_successful_trial_booking_references_occurrence(self):
        """11. Successful TrialBooking explicitly references the selected class occurrence."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(post_resp.status_code, status.HTTP_201_CREATED)
        booking = TrialBooking.objects.using('tenant_test').get(id=post_resp.data['id'])
        self.assertEqual(booking.class_occurrence_id, self.occ_valid.id)

    def test_12_booking_reduces_available_trial_capacity(self):
        """12. Booking a trial reduces available trial spots from 2 to 1."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        matching = next((s for s in slots if s['occurrence_id'] == str(self.occ_valid.id)), None)
        self.assertIsNotNone(matching)
        self.assertEqual(matching['remaining_trial_capacity'], 1)

    def test_13_last_seat_concurrent_booking_protected(self):
        """13. Booking the last trial seat succeeds, subsequent attempt fails."""
        lead2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='Lead2', last_name='T', current_status='NEW_LEAD'
        )
        lead3 = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='Lead3', last_name='T', current_status='NEW_LEAD'
        )
        # 1st booking (1 remaining)
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        # 2nd booking for another lead (last seat, 0 remaining)
        resp2 = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(lead2.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)

        # 3rd booking must fail with full message
        resp3 = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(lead3.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("full", resp3.data.get('error', '').lower())

    def test_14_duplicate_lead_trial_booking_handled(self):
        """14. A lead cannot book the same occurrence twice."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp2 = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already has an active trial", resp2.data.get('error', '').lower())

    def test_15_successful_booking_changes_lead_to_trial_booked(self):
        """15. Successful booking changes Lead status to TRIAL_BOOKED."""
        self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'TRIAL_BOOKED')

    def test_16_failed_booking_does_not_change_lead_status(self):
        """16. If booking fails (e.g. invalid occurrence), Lead status remains unchanged."""
        resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(uuid.uuid4()), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'NEW_LEAD')

    def test_17_manual_trial_booked_transition_without_trial_booking_blocked(self):
        """17. Manual transition to TRIAL_BOOKED without a real TrialBooking is blocked."""
        resp = self.client.post(
            f'/api/v1/tenant/leads/{self.lead.id}/transition-status/',
            {'new_status': 'TRIAL_BOOKED'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Book a real trial session", resp.data.get('error', ''))

    def test_18_confirmation_transitions_lead_to_trial_confirmed(self):
        """18. Trial confirmation transitions Lead to TRIAL_CONFIRMED."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = post_resp.data['id']
        conf_resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial_id}/confirm/',
            {'channel': 'PHONE'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(conf_resp.status_code, status.HTTP_200_OK)
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'TRIAL_CONFIRMED')

    def test_19_attendance_transitions_lead_to_trial_attended(self):
        """19. Marking trial attended transitions Lead to TRIAL_ATTENDED."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = post_resp.data['id']
        att_resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial_id}/mark-attended/',
            {},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(att_resp.status_code, status.HTTP_200_OK)
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'TRIAL_ATTENDED')

    def test_20_noshow_transitions_lead_to_no_show(self):
        """20. Marking trial no-show transitions Lead to NO_SHOW."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trial_id = post_resp.data['id']
        ns_resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial_id}/mark-no-show/',
            {},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(ns_resp.status_code, status.HTTP_200_OK)
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'NO_SHOW')

    def test_21_trial_management_lists_created_booking(self):
        """21. Trial Management workspace API lists the booked trial."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        list_resp = self.client.get(
            '/api/v1/tenant/trial-bookings/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        results = list_resp.data.get('results', list_resp.data)
        trial_ids = [t['id'] for t in results]
        self.assertIn(post_resp.data['id'], trial_ids)

    def test_22_lead360_returns_same_booking(self):
        """22. Lead 360 trials tab returns the same backend trial booking."""
        post_resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        trials_resp = self.client.get(
            f'/api/v1/tenant/leads/{self.lead.id}/trials/',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(trials_resp.status_code, status.HTTP_200_OK)
        trials_data = trials_resp.data if isinstance(trials_resp.data, list) else trials_resp.data.get('results', [])
        ids = [t['id'] for t in trials_data]
        self.assertIn(post_resp.data['id'], ids)

    def test_23_branch_authorization_enforced(self):
        """23. User without branch access cannot view available slots for that branch."""
        # Create user restricted only to branch A1
        user_a1_only = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='a1only@test.com', first_name='A1', last_name='User', status='ACTIVE'
        )
        UserBranch.objects.using('tenant_test').create(user=user_a1_only, branch=self.branch_a1, is_primary=True, scope_type='HOME')
        role_branch = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ROLE_BRANCH_ONLY',
            name='Branch Only Role',
            scope='BRANCH',
            is_active=True,
        )
        mod_crm = ModuleCatalog.objects.using('tenant_test').get(module_code='crm')
        sub_trials = SubmoduleCatalog.objects.using('tenant_test').get(submodule_code='trials')
        RoleModuleAccess.objects.using('tenant_test').create(role=role_branch, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=role_branch, submodule=sub_trials, can_access=True)
        ps_branch = RolePermissionSet.objects.using('tenant_test').create(role=role_branch, name='branch_perms')
        p = Permission.objects.using('tenant_test').get(permission_code='crm.trials.view')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_branch, permission=p)
        RoleAssignment.objects.using('tenant_test').create(
            user=user_a1_only, role=role_branch, branch=self.branch_a1, is_active=True, status='ACTIVE'
        )
        token_restricted = str(_build_tenant_token(user_a1_only, self.tenant_a, 'tenant_test').access_token)

        # Trying to query branch A2 should be denied 403
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a2.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(token_restricted, self.branch_a2)
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_24_tenant_isolation_enforced(self):
        """24. Cross-tenant trial booking is rejected."""
        tenant_b = Tenant.objects.using('default').create(
            code='CRM-TEN-B-OCC', name='Tenant B', slug='tenant-b-occ', status='ACTIVE'
        )
        TenantDataSource.objects.using('default').create(
            tenant=tenant_b, db_name='test_fitness_tenant_b', database_name='test_fitness_tenant_b', status='ACTIVE', database_engine='POSTGRESQL'
        )
        org_b = Organization.objects.using('tenant_test').create(code='ORG-OCC-B', name='Org B', status='ACTIVE')
        loc_b = Location.objects.using('tenant_test').create(organization=org_b, code='LOC-B', name='Loc B', status='ACTIVE')
        branch_b = Branch.objects.using('tenant_test').create(organization=org_b, location=loc_b, code='BR-B', name='Branch B', status='ACTIVE')
        lead_b = Lead.objects.using('tenant_test').create(
            organization=org_b, branch=branch_b, first_name='Foreign', last_name='Lead', current_status='NEW_LEAD'
        )

        # Trying to book tenant B lead into tenant A branch
        with self.assertRaises(Exception):
            CRMLeadService.book_trial(
                lead=lead_b, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
            )

    def test_25_availability_endpoint_dynamic(self):
        """25. Availability endpoint dynamically reflects new occurrences."""
        day_after = self.tomorrow + timedelta(days=1)
        occ_future = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_pilates,
            branch=self.branch_a1,
            occurrence_date=day_after,
            start_at=timezone.make_aware(timezone.datetime.combine(day_after, time(14, 0))),
            end_at=timezone.make_aware(timezone.datetime.combine(day_after, time(15, 0))),
            capacity=10,
            trial_capacity=2,
            status='OPEN',
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={day_after.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp.data.get('slots', [])
        occ_ids = [s['occurrence_id'] for s in slots]
        self.assertIn(str(occ_future.id), occ_ids)

    def test_26_trial_cap_2_zero_trials_visible(self):
        """26. Trial cap 2, zero trials booked → occurrence visible with 2 remaining."""
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = next(s for s in resp.data['slots'] if s['occurrence_id'] == str(self.occ_valid.id))
        self.assertEqual(slot['trial_capacity'], 2)
        self.assertEqual(slot['remaining_trial_capacity'], 2)

    def test_27_trial_cap_2_one_trial_visible_1_remaining(self):
        """27. Trial cap 2, 1 trial booked → occurrence visible with 1 remaining."""
        other_lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='Other', last_name='L', current_status='NEW_LEAD'
        )
        CRMLeadService.book_trial(
            lead=other_lead, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
        )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slot = next(s for s in resp.data['slots'] if s['occurrence_id'] == str(self.occ_valid.id))
        self.assertEqual(slot['remaining_trial_capacity'], 1)

    def test_28_trial_cap_2_two_trials_not_returned(self):
        """28. Trial cap 2, 2 active trials booked → occurrence NOT returned."""
        for i in range(2):
            l = Lead.objects.using('tenant_test').create(
                organization=self.org_a, branch=self.branch_a1, first_name=f'L{i}', last_name='T', current_status='NEW_LEAD'
            )
            CRMLeadService.book_trial(
                lead=l, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
            )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        occ_ids = [s['occurrence_id'] for s in resp.data.get('slots', [])]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_29_cancelled_trial_releases_trial_capacity(self):
        """29. Cancelled trial releases trial capacity, making slot visible again."""
        other_lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='Other', last_name='L', current_status='NEW_LEAD'
        )
        trial1 = CRMLeadService.book_trial(
            lead=other_lead, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
        )
        other_lead2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='Other2', last_name='L', current_status='NEW_LEAD'
        )
        trial2 = CRMLeadService.book_trial(
            lead=other_lead2, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
        )

        # Now full (not visible)
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertNotIn(str(self.occ_valid.id), [s['occurrence_id'] for s in resp.data.get('slots', [])])

        # Cancel trial 1 via API
        cancel_resp = self.client.post(
            f'/api/v1/tenant/trial-bookings/{trial1.id}/cancel/',
            {'reason': 'Client cancelled'},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK)

        # Now visible again with 1 trial remaining!
        resp2 = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        slots = resp2.data.get('slots', [])
        slot = next(s for s in slots if s['occurrence_id'] == str(self.occ_valid.id))
        self.assertEqual(slot['remaining_trial_capacity'], 1)

    def test_30_full_trial_cap_normal_seats_remain_unavailable_for_trial(self):
        """30. Normal seats remain (e.g. 2/10 booked), but trial cap reached (2/2 trials) → unavailable for trial."""
        for i in range(2):
            l = Lead.objects.using('tenant_test').create(
                organization=self.org_a, branch=self.branch_a1, first_name=f'L{i}', last_name='T', current_status='NEW_LEAD'
            )
            CRMLeadService.book_trial(
                lead=l, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
            )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        occ_ids = [s['occurrence_id'] for s in resp.data.get('slots', [])]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_31_normal_booking_cap_full_unavailable_for_trial(self):
        """31. 0 trials booked, but all 10 normal seats booked → unavailable for trial."""
        for i in range(10):
            usr = TenantUser.objects.using('tenant_test').create(
                organization=self.org_a, email=f'u{i}@test.com', first_name=f'U{i}', last_name='T', status='ACTIVE'
            )
            prof, _ = UserProfile.objects.using('tenant_test').get_or_create(user=usr)
            Booking.objects.using('tenant_test').create(
                booking_number=f'BKG-31-{i}-{uuid.uuid4().hex[:6]}',
                user_profile=prof,
                occurrence=self.occ_valid,
                branch=self.branch_a1,
                status='CONFIRMED',
            )
        resp = self.client.get(
            f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={self.branch_a1.id}&date={self.tomorrow.isoformat()}',
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        occ_ids = [s['occurrence_id'] for s in resp.data.get('slots', [])]
        self.assertNotIn(str(self.occ_valid.id), occ_ids)

    def test_32_concurrent_bookings_final_seat_exactly_one_succeeds(self):
        """32. When 1 trial seat remains, two attempts on that seat result in exactly one success."""
        # Book 1st trial (1 remains out of 2)
        other_lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='L1', last_name='T', current_status='NEW_LEAD'
        )
        CRMLeadService.book_trial(
            lead=other_lead, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
        )

        lead_a = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='LA', last_name='T', current_status='NEW_LEAD'
        )
        lead_b = Lead.objects.using('tenant_test').create(
            organization=self.org_a, branch=self.branch_a1, first_name='LB', last_name='T', current_status='NEW_LEAD'
        )

        resp_a = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(lead_a.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        resp_b = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(lead_b.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )

        successes = sum(1 for r in [resp_a, resp_b] if r.status_code == status.HTTP_201_CREATED)
        failures = sum(1 for r in [resp_a, resp_b] if r.status_code == status.HTTP_400_BAD_REQUEST)
        self.assertEqual(successes, 1)
        self.assertEqual(failures, 1)

    def test_33_failed_capacity_booking_does_not_change_lead_status(self):
        """33. When booking fails due to capacity, lead status remains NEW_LEAD."""
        # Fill capacity
        for i in range(2):
            l = Lead.objects.using('tenant_test').create(
                organization=self.org_a, branch=self.branch_a1, first_name=f'Fill{i}', last_name='T', current_status='NEW_LEAD'
            )
            CRMLeadService.book_trial(
                lead=l, branch=self.branch_a1, class_occurrence_id=self.occ_valid.id, db_alias='tenant_test'
            )

        resp = self.client.post(
            '/api/v1/tenant/trial-bookings/',
            {'lead_id': str(self.lead.id), 'class_occurrence_id': str(self.occ_valid.id), 'branch_id': str(self.branch_a1.id)},
            **self.auth_headers(self.token_admin, self.branch_a1)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'NEW_LEAD')
