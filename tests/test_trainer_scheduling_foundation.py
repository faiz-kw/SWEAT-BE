import uuid
from datetime import date, time, datetime, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    Organization,
    Location,
    Branch,
    TenantUser,
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    EmployeeWorkSchedule,
    EmployeeScheduleException,
    ApprovalRequest,
    ApprovalAction,
)
from apps.tenant_core.services_approvals import AdminApprovalService
from apps.tenant_core.services_workforce import TrainerAvailabilityService
from apps.tenant_core.views_workforce import EmployeeWorkScheduleViewSet


class TrainerSchedulingFoundationTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="Sweat Performance Org",
            code=f"SWT-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Downtown Campus",
            city="Metropolis",
            status="ACTIVE"
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name="Downtown Branch",
            code=f"BRN-{uuid.uuid4().hex[:4]}",
            timezone="UTC",
            status="ACTIVE"
        )

        # Trainer User
        self.trainer_user = TenantUser.objects.create(
            organization=self.org,
            email=f"trainer-{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Max",
            last_name="Power",
            status="ACTIVE"
        )
        self.user_profile = UserProfile.objects.create(
            user=self.trainer_user,
            first_name_snapshot="Max",
            last_name_snapshot="Power"
        )
        self.employee_profile = EmployeeProfile.objects.create(
            user_profile=self.user_profile,
            organization=self.org,
            employee_code=f"EMP-{uuid.uuid4().hex[:4].upper()}",
            designation="Senior Fitness Trainer",
            employment_status="ACTIVE",
        )
        self.trainer_profile = TrainerProfile.objects.create(
            employee_profile=self.employee_profile,
            trainer_code=f"TR-{uuid.uuid4().hex[:4].upper()}",
            trainer_status="ACTIVE",
            can_teach_all_specialties=True,
        )

        # Admin Approver
        self.admin_user = TenantUser.objects.create(
            organization=self.org,
            email=f"admin-{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Diana",
            last_name="Director",
            status="ACTIVE"
        )

    def test_bulk_sync_roster_and_weekoff_allowlist(self):
        """
        Verify that bulk_sync atomically updates the employee's work schedule.
        Any weekday omitted from the payload is deleted, acting as a scheduled Week-Off.
        """
        factory = APIRequestFactory()
        view = EmployeeWorkScheduleViewSet.as_view({'post': 'bulk_sync'}, permission_classes=[])

        # 1. Sync Mon (1) and Tue (2) shifts
        payload = {
            'employee_profile_id': str(self.employee_profile.id),
            'branch_id': str(self.branch.id),
            'schedules': [
                {'day_of_week': 1, 'start_time': '09:00:00', 'end_time': '17:00:00'},
                {'day_of_week': 2, 'start_time': '09:00:00', 'end_time': '17:00:00'},
            ]
        }
        request = factory.post('/api/v1/tenant/work-schedules/bulk-sync/', payload, format='json')
        request.user = self.admin_user
        request.organization = self.org
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmployeeWorkSchedule.objects.filter(employee_profile=self.employee_profile).count(), 2)

        # 2. Sync payload with only Wednesday (3). Monday and Tuesday must be cleared.
        payload2 = {
            'employee_profile_id': str(self.employee_profile.id),
            'branch_id': str(self.branch.id),
            'schedules': [
                {'day_of_week': 3, 'start_time': '10:00:00', 'end_time': '18:00:00'},
            ]
        }
        request2 = factory.post('/api/v1/tenant/work-schedules/bulk-sync/', payload2, format='json')
        request2.user = self.admin_user
        request2.organization = self.org
        response2 = view(request2)

        self.assertEqual(response2.status_code, 200)
        schedules = EmployeeWorkSchedule.objects.filter(employee_profile=self.employee_profile)
        self.assertEqual(schedules.count(), 1)
        self.assertEqual(schedules.first().day_of_week, 3)

    def test_bulk_sync_week_specific_roster(self):
        """
        Verify that bulk_sync supports creating week-specific rosters (SPECIFIC_WEEK)
        and recurring rosters (RECURRING_FROM_WEEK), with calendar week filtering.
        """
        factory = APIRequestFactory()
        view_sync = EmployeeWorkScheduleViewSet.as_view({'post': 'bulk_sync'}, permission_classes=[])
        view_list = EmployeeWorkScheduleViewSet.as_view({'get': 'list'}, permission_classes=[])

        # 1. Sync ongoing roster from 2026-09-01
        payload_base = {
            'employee_profile_id': str(self.employee_profile.id),
            'branch_id': str(self.branch.id),
            'week_start_date': '2026-09-01',
            'apply_mode': 'RECURRING_FROM_WEEK',
            'schedules': [
                {'day_of_week': 1, 'start_time': '08:00:00', 'end_time': '16:00:00'},
                {'day_of_week': 2, 'start_time': '08:00:00', 'end_time': '16:00:00'},
            ]
        }
        req_base = factory.post('/api/v1/tenant/work-schedules/bulk-sync/', payload_base, format='json')
        req_base.user = self.admin_user
        req_base.organization = self.org
        resp_base = view_sync(req_base)
        self.assertEqual(resp_base.status_code, 200)

        # 2. Sync a week-specific override for week 2026-09-21 to 2026-09-27
        payload_week = {
            'employee_profile_id': str(self.employee_profile.id),
            'branch_id': str(self.branch.id),
            'week_start_date': '2026-09-21',
            'week_end_date': '2026-09-27',
            'apply_mode': 'SPECIFIC_WEEK',
            'schedules': [
                {'day_of_week': 3, 'start_time': '10:00:00', 'end_time': '18:00:00'},
                {'day_of_week': 4, 'start_time': '10:00:00', 'end_time': '18:00:00'},
            ]
        }
        req_week = factory.post('/api/v1/tenant/work-schedules/bulk-sync/', payload_week, format='json')
        req_week.user = self.admin_user
        req_week.organization = self.org
        resp_week = view_sync(req_week)
        self.assertEqual(resp_week.status_code, 200)

        # Query schedules for week 2026-09-21 -> 2026-09-27
        req_q = factory.get(
            f'/api/v1/tenant/work-schedules/?employee_profile_id={self.employee_profile.id}&week_start=2026-09-21&week_end=2026-09-27'
        )
        req_q.user = self.admin_user
        req_q.organization = self.org
        resp_q = view_list(req_q)
        self.assertEqual(resp_q.status_code, 200)
        results = resp_q.data.get('results', resp_q.data) if isinstance(resp_q.data, dict) else resp_q.data
        days_found = [item['day_of_week'] for item in results]
        self.assertIn(3, days_found)
        self.assertIn(4, days_found)

    def test_trainer_leave_approval_and_auto_materialization(self):
        """
        Verify that when a trainer applies for leave (TRAINER_LEAVE_REQUEST),
        Admin approval automatically materializes an active EmployeeScheduleException.
        """
        target_date = date(2026, 10, 15)

        # 1. Trainer submits leave request
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='TRAINER_LEAVE_REQUEST',
            entity_type='EmployeeProfile',
            entity_id=self.employee_profile.id,
            requested_by_user=self.trainer_user,
            requested_payload={
                'employee_profile_id': str(self.employee_profile.id),
                'branch_id': str(self.branch.id),
                'exception_date': target_date.isoformat(),
                'exception_type': 'LEAVE',
                'is_available': False,
                'reason': 'Family wedding out of town',
            },
            required_approvals=1
        )

        self.assertEqual(req.status, 'PENDING')
        self.assertFalse(
            EmployeeScheduleException.objects.filter(
                employee_profile=self.employee_profile,
                exception_date=target_date
            ).exists()
        )

        # 2. Admin approves the leave request
        action = AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.admin_user,
            action='APPROVED',
            comment='Have a wonderful time!'
        )

        req.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')

        # 3. Schedule exception must now be materialized
        exc = EmployeeScheduleException.objects.filter(
            employee_profile=self.employee_profile,
            exception_date=target_date
        ).first()

        self.assertIsNotNone(exc)
        self.assertEqual(exc.exception_type, 'LEAVE')
        self.assertFalse(exc.is_available)
        self.assertEqual(exc.status, 'ACTIVE')
        self.assertEqual(exc.reason, 'Family wedding out of town')

    def test_availability_engine_with_roster_weekoff_and_overrides(self):
        """
        Verify TrainerAvailabilityService respects:
        - Regular roster shifts
        - Missing schedule rows (Week-off = unavailable)
        - Materialized Leave (unavailable)
        - Materialized Weekly-Off Override (available on off-day)
        """
        # Configure trainer shift on Monday only (iso_day 1)
        # Choose a fixed Monday: 2026-10-12
        monday_date = date(2026, 10, 12)
        self.assertEqual(monday_date.isoweekday(), 1)

        tuesday_date = date(2026, 10, 13)
        self.assertEqual(tuesday_date.isoweekday(), 2)

        EmployeeWorkSchedule.objects.create(
            employee_profile=self.employee_profile,
            branch=self.branch,
            day_of_week=1,
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            schedule_type='REGULAR',
            status='ACTIVE',
        )

        # 1. Check Monday 10:00-11:00 -> Available
        dt_mon = datetime.combine(monday_date, time(10, 0))
        avail, reason, _ = TrainerAvailabilityService.is_trainer_available(
            trainer=self.trainer_profile,
            branch=self.branch,
            start_datetime=dt_mon,
            duration_minutes=60,
        )
        self.assertTrue(avail, f"Expected available on Monday shift: {reason}")

        # 2. Check Tuesday 10:00-11:00 -> Unavailable (No schedule row = scheduled Week-Off)
        dt_tue = datetime.combine(tuesday_date, time(10, 0))
        avail, reason, _ = TrainerAvailabilityService.is_trainer_available(
            trainer=self.trainer_profile,
            branch=self.branch,
            start_datetime=dt_tue,
            duration_minutes=60,
        )
        self.assertFalse(avail, "Expected unavailable on Tuesday (Week-Off)")
        self.assertIn("does not have an active work shift", reason)

        # 3. Add WEEKLY_OFF_OVERRIDE for Tuesday
        EmployeeScheduleException.objects.create(
            employee_profile=self.employee_profile,
            branch=self.branch,
            exception_date=tuesday_date,
            exception_type='WEEKLY_OFF_OVERRIDE',
            is_available=True,
            start_time=time(9, 0),
            end_time=time(14, 0),
            reason='Covering special morning workshop',
            status='ACTIVE',
        )

        # Now Tuesday 10:00-11:00 is covered by the override!
        avail, reason, _ = TrainerAvailabilityService.is_trainer_available(
            trainer=self.trainer_profile,
            branch=self.branch,
            start_datetime=dt_tue,
            duration_minutes=60,
        )
        self.assertTrue(avail, f"Expected available on Tuesday due to override: {reason}")
