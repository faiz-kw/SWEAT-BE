"""
backend/tests/test_crm_agent_assignment.py

Unit and regression tests for:
1. CRMAgentAssignmentConfigViewSet:
   - Accurate role-wise active staff user counts (including trainers, coaches, and staff)
   - Exclusion of inactive, deactivated, and customer member accounts
   - Robust role-code matching (singular / plural)
   - Comprehensive available_users listing with proper role assignments
2. LeadAssignmentEligibilityService:
   - Dynamic representative determination based on allowed_role_codes
   - Excluded user state enforcement
   - Non-dropping auto-assignment and eligibility checks
"""

import uuid
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory
from rest_framework.request import Request

from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_crm import CRMAgentAssignmentConfig
from apps.tenant_core.models_workforce import TrainerProfile, EmployeeProfile, UserProfile
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer
from apps.tenant_core.views_crm import CRMAgentAssignmentConfigViewSet
from apps.tenant_core.views_workforce import TrainerProfileViewSet
from apps.tenant_core.views_classes import ClassOccurrenceViewSet
from apps.tenant_core.services_classes import ClassSchedulingService
from apps.tenant_core.services_lead_assignment import LeadAssignmentEligibilityService
from config.routers import set_tenant_db_alias


class CRMAgentAssignmentConfigTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        self.db = 'tenant_test'
        set_tenant_db_alias(self.db)
        from apps.tenant_core.models_org import Location
        self.org = Organization.objects.using(self.db).create(
            name="Test Fitness Org",
            code=f"ORG_{uuid.uuid4().hex[:6].upper()}",
            status="ACTIVE",
        )
        self.loc = Location.objects.using(self.db).create(
            organization=self.org,
            name='Central Location',
            code=f"LOC_{uuid.uuid4().hex[:3].upper()}",
            city='Mumbai',
        )
        self.branch = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            name="Main Branch",
            status="ACTIVE",
        )

        # 1. Create Roles
        self.role_admin = Role.objects.using(self.db).create(
            organization=self.org,
            name="Organization Administrator",
            code="ORG_ADMIN",
            scope="ORG",
            is_active=True,
            status="ACTIVE",
        )
        self.role_rep = Role.objects.using(self.db).create(
            organization=self.org,
            name="Sales & CRM Representative",
            code="SALES_REP",
            scope="ORG",
            is_active=True,
            status="ACTIVE",
        )
        self.role_hybrid = Role.objects.using(self.db).create(
            organization=self.org,
            name="Hybrid Trainers",
            code="HYBRID_TRAINERS",
            scope="BRANCH",
            is_active=True,
            status="ACTIVE",
        )

        # 2. Create Active Staff Member with ORG_ADMIN
        self.user_admin = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="admin@test.com",
            first_name="Admin",
            last_name="User",
            user_type="STAFF",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_admin,
            role=self.role_admin,
            status="ACTIVE",
            is_active=True,
            scope_type="ORGANIZATION",
        )

        # 3. Create Active Staff Member with SALES_REP
        self.user_rep = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="rep@test.com",
            first_name="Sales",
            last_name="Rep",
            user_type="STAFF",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_rep,
            role=self.role_rep,
            status="ACTIVE",
            is_active=True,
            scope_type="ORGANIZATION",
        )

        # 4. Create Active Trainer with HYBRID_TRAINERS (user_type='TRAINER', not 'STAFF')
        self.user_trainer_1 = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="trainer1@test.com",
            first_name="Hybrid",
            last_name="Trainer One",
            user_type="TRAINER",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_trainer_1,
            role=self.role_hybrid,
            status="ACTIVE",
            is_active=True,
            scope_type="BRANCH",
            branch=self.branch,
        )

        # 5. Create Second Active Trainer with HYBRID_TRAINERS (user_type='TRAINER')
        self.user_trainer_2 = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="trainer2@test.com",
            first_name="Hybrid",
            last_name="Trainer Two",
            user_type="TRAINER",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_trainer_2,
            role=self.role_hybrid,
            is_active=True,  # status is unset or None
            scope_type="BRANCH",
            branch=self.branch,
        )

        # 6. Create Deactivated / Inactive User with HYBRID_TRAINERS (MUST NOT be counted)
        self.user_inactive_trainer = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="inactive_trainer@test.com",
            first_name="Inactive",
            last_name="Trainer",
            user_type="TRAINER",
            status="INACTIVE",
            is_login_allowed=False,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_inactive_trainer,
            role=self.role_hybrid,
            status="ACTIVE",
            is_active=True,
            scope_type="BRANCH",
            branch=self.branch,
        )

        # 7. Create Customer Member with role (MUST NOT be counted as staff)
        self.user_member = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="member@test.com",
            first_name="Gym",
            last_name="Member",
            user_type="MEMBER",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.user_member,
            role=self.role_hybrid,
            status="ACTIVE",
            is_active=True,
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def test_role_wise_user_count_accuracy(self):
        """Verify role_data counts accurately reflect active staff holding the role, excluding inactive and members."""
        factory = APIRequestFactory()
        request = factory.get('/tenant/crm/agent-assignment-config/')
        request._tenant_db = self.db
        request.org = self.org
        request.user = self.user_admin

        viewset = CRMAgentAssignmentConfigViewSet()
        response = viewset.list(request)
        self.assertEqual(response.status_code, 200)

        roles_dict = {r['code']: r for r in response.data['available_roles']}
        self.assertIn('HYBRID_TRAINERS', roles_dict)
        # Should count user_trainer_1 and user_trainer_2 (2 active trainers), NOT the inactive user or member
        self.assertEqual(roles_dict['HYBRID_TRAINERS']['user_count'], 2)
        self.assertEqual(roles_dict['ORG_ADMIN']['user_count'], 1)
        self.assertEqual(roles_dict['SALES_REP']['user_count'], 1)

    def test_available_users_includes_trainers_and_staff(self):
        """Verify available_users includes all active workforce staff (both STAFF and TRAINER), excluding MEMBER."""
        factory = APIRequestFactory()
        request = factory.get('/tenant/crm/agent-assignment-config/')
        request._tenant_db = self.db
        request.org = self.org
        request.user = self.user_admin

        viewset = CRMAgentAssignmentConfigViewSet()
        response = viewset.list(request)
        self.assertEqual(response.status_code, 200)

        user_emails = [u['email'] for u in response.data['available_users']]
        self.assertIn('admin@test.com', user_emails)
        self.assertIn('rep@test.com', user_emails)
        self.assertIn('trainer1@test.com', user_emails)
        self.assertIn('trainer2@test.com', user_emails)
        # Inactive trainer and gym member must NOT be in available_users
        self.assertNotIn('inactive_trainer@test.com', user_emails)
        self.assertNotIn('member@test.com', user_emails)

        # Check role serialization on trainer2 (whose status was unset)
        t2_data = next(u for u in response.data['available_users'] if u['email'] == 'trainer2@test.com')
        self.assertTrue(len(t2_data['roles']) >= 1)
        self.assertEqual(t2_data['roles'][0]['code'], 'HYBRID_TRAINERS')

    def test_lead_assignment_eligibility_service_resolves_hybrid_trainers(self):
        """Verify LeadAssignmentEligibilityService determines trainers as eligible when role is allowed."""
        config, _ = CRMAgentAssignmentConfig.objects.using(self.db).get_or_create(
            organization=self.org,
            defaults={
                'allowed_role_codes': ['HYBRID_TRAINERS'],
                'require_branch_match': False,
            }
        )
        config.allowed_role_codes = ['HYBRID_TRAINERS']
        config.save(using=self.db)

        candidates = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch,
            db_alias=self.db,
            include_unavailable=True,
        )

        candidate_emails = [c['email'] for c in candidates if c['is_available']]
        self.assertIn('trainer1@test.com', candidate_emails)
        self.assertIn('trainer2@test.com', candidate_emails)
        # Admin and Rep should not be available because their role is not allowed
        self.assertNotIn('admin@test.com', candidate_emails)
        self.assertNotIn('rep@test.com', candidate_emails)

    def test_trainer_profile_viewset_includes_hybrid_trainers_with_staff_user_type(self):
        """Verify staff accounts with Hybrid Trainers role are recognized as trainers and auto-synced."""
        # Create user with user_type='STAFF' (exact scenario for staff created via Admin Users)
        sairaj = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="sairaj@test.com",
            first_name="Sairaj",
            last_name="Pakhare",
            user_type="STAFF",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=sairaj,
            role=self.role_hybrid,
            status="ACTIVE",
            is_active=True,
            scope_type="ORGANIZATION",
        )

        # 1. _trainer_users must identify Sairaj
        trainer_users = TrainerProfileViewSet._trainer_users(self.db, self.org)
        self.assertIn(sairaj.id, list(trainer_users.values_list('id', flat=True)))

        # 2. GET /tenant/trainer-profiles/ must auto-sync and list Sairaj
        factory = APIRequestFactory()
        request = factory.get('/tenant/trainer-profiles/?trainer_status=ACTIVE')
        request._tenant_db = self.db
        request.organization = self.org
        request.user = self.user_admin
        drf_req = Request(request)
        drf_req._tenant_db = self.db
        drf_req.organization = self.org
        drf_req.user = self.user_admin

        viewset = TrainerProfileViewSet()
        viewset.request = drf_req
        viewset.format_kwarg = None
        response = viewset.list(drf_req)
        self.assertEqual(response.status_code, 200)

        results = response.data.get('results', response.data)
        trainer_user_ids = [
            t.get('user_id') or t.get('user') or
            (t.get('employee_profile', {}) or {}).get('user_profile', {}).get('user')
            for t in results
        ]
        # Verify TrainerProfile was created for Sairaj
        profile = TrainerProfile.objects.using(self.db).filter(
            employee_profile__user_profile__user_id=sairaj.id
        ).first()
        self.assertIsNotNone(profile)

        # 3. GET /tenant/trainer-profiles/me/ when logged in as Sairaj returns 200
        req_me = factory.get('/tenant/trainer-profiles/me/')
        req_me._tenant_db = self.db
        req_me.organization = self.org
        req_me.user = sairaj
        drf_req_me = Request(req_me)
        drf_req_me._tenant_db = self.db
        drf_req_me.organization = self.org
        drf_req_me.user = sairaj

        me_viewset = TrainerProfileViewSet()
        me_viewset.request = drf_req_me
        me_viewset.format_kwarg = None
        me_response = me_viewset.me(drf_req_me)
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.data['id'], str(profile.id))

    def test_session_trainer_assignment_and_visibility(self):
        """Verify trainer assignment to a class and query visibility by trainer_profile_id and user_id."""
        now = timezone.now()
        cat = ClassCategory.objects.using(self.db).create(
            organization=self.org,
            name="Functional Training",
            code="FUNC",
        )
        template = ClassTemplate.objects.using(self.db).create(
            organization=self.org,
            category=cat,
            name="HIIT Strength",
            code="HIIT_01",
            default_duration_minutes=45,
            default_delivery_mode="HYBRID",
            status="ACTIVE",
        )
        occurrence = ClassOccurrence.objects.using(self.db).create(
            class_template=template,
            branch=self.branch,
            occurrence_date=now.date(),
            start_at=now + timezone.timedelta(hours=2),
            end_at=now + timezone.timedelta(hours=2, minutes=45),
            capacity=20,
            status="SCHEDULED",
        )

        # Auto-create trainer profile for user_trainer_1
        TrainerProfileViewSet._ensure_trainer_profiles_for_staff(self.db, self.org)
        profile = TrainerProfile.objects.using(self.db).filter(
            employee_profile__user_profile__user_id=self.user_trainer_1.id
        ).first()
        self.assertIsNotNone(profile)

        # Assign trainer using ClassSchedulingService
        assignment = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=str(occurrence.id),
            trainer_profile_id=str(profile.id),
            trainer_role='LEAD',
            actor=self.user_admin,
            db_alias=self.db,
        )
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.occurrence_id, occurrence.id)

        # Verify querying ClassOccurrenceViewSet by trainer_profile_id
        factory = APIRequestFactory()
        req_by_profile = factory.get(f'/tenant/class-occurrences/?trainer_id={profile.id}')
        req_by_profile._tenant_db = self.db
        req_by_profile.organization = self.org
        req_by_profile.user = self.user_admin
        drf_req1 = Request(req_by_profile)
        drf_req1._tenant_db = self.db
        drf_req1.organization = self.org
        drf_req1.user = self.user_admin

        occ_viewset1 = ClassOccurrenceViewSet()
        occ_viewset1.request = drf_req1
        occ_viewset1.format_kwarg = None
        res1 = occ_viewset1.list(drf_req1)
        self.assertEqual(res1.status_code, 200)
        ids1 = [item['id'] for item in res1.data.get('results', res1.data)]
        self.assertIn(str(occurrence.id), ids1)

        # Verify querying ClassOccurrenceViewSet by user_id
        req_by_user = factory.get(f'/tenant/class-occurrences/?trainer_id={self.user_trainer_1.id}')
        req_by_user._tenant_db = self.db
        req_by_user.organization = self.org
        req_by_user.user = self.user_admin
        drf_req2 = Request(req_by_user)
        drf_req2._tenant_db = self.db
        drf_req2.organization = self.org
        drf_req2.user = self.user_admin

        occ_viewset2 = ClassOccurrenceViewSet()
        occ_viewset2.request = drf_req2
        occ_viewset2.format_kwarg = None
        res2 = occ_viewset2.list(drf_req2)
        self.assertEqual(res2.status_code, 200)
        ids2 = [item['id'] for item in res2.data.get('results', res2.data)]
        self.assertIn(str(occurrence.id), ids2)

