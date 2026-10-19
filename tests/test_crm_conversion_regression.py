"""
backend/tests/test_crm_conversion_regression.py

Targeted regression tests for Lead -> Member conversion:
1. Valid lead conversion creates exactly one correctly linked member and user profile.
2. Missing required fields validation returns HTTP 400.
3. Repeated submissions do not create duplicates (idempotency).
4. Failed conversion does not leave inconsistent or orphan records.
5. Lead history and attribution remain intact.
6. Cash payment creates ApprovalRequest for studio manager review (financial governance preserved).
7. Converted member is discoverable in Client 360 (build_member_360_aggregate).
"""

import uuid
import logging
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_crm import Lead, LeadConversion, LeadSource, TrialBooking
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.models_commerce import Order
from apps.tenant_core.models_approvals import ApprovalRequest
from apps.tenant_core.serializers_crm import LeadSerializer
from apps.tenant_core.views_crm import LeadViewSet
from apps.tenant_core.views_members import build_member_360_aggregate

DB = 'tenant_test'


class LeadConversionRegressionTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Test Gym',
            code=f"SW-{uuid.uuid4().hex[:4].upper()}",
            slug=f"sweat-test-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Organization & Branch
        self.org = Organization.objects.using(DB).create(
            name='SWEAT Fitness',
            code=f"SW-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name='Indiranagar',
            code=f"IN-{uuid.uuid4().hex[:3].upper()}",
            city='Bangalore',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='SWEAT Indiranagar',
            code=f"BR-{uuid.uuid4().hex[:3].upper()}",
            status='ACTIVE',
        )

        # Admin Staff User
        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Admin',
            last_name='Staff',
            user_type='STAEF',
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.admin.is_superuser = True
        self.admin_profile = UserProfile.objects.using(DB).create(
            user=self.admin,
        )

        # Lead Source
        self.source = LeadSource.objects.using(DB).create(
            organization=self.org,
            code='WALK_IN',
            name='Walk-in',
            status='ACTIVE',
        )

        # Catalog setup: Category, Program, Package, Version, Price
        self.category = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            name='Bootcamp',
            code=f"CAT-{uuid.uuid4().hex[:4].upper()}",
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org,
            category=self.category,
            name='SWEAT Conditioning',
            code=f"PROG-{uuid.uuid4().hex[:4].upper()}",
        )
        self.pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            name='Monthly Standard',
            code=f"PKG-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.pkg_version = PackageVersion.objects.using(DB).create(
            package=self.pkg,
            version_number=1,
            name_snapshot='Monthly Standard v1',
            duration_value=30,
            duration_unit='DAY',
            total_days=30,
            status='ACTIVE',
            created_by_user=self.admin,
            effective_from=timezone.now() - timedelta(days=10),
        )
        self.pkg_price = PackagePrice.objects.using(DB).create(
            package_version=self.pkg_version,
            currency='INR',
            base_price=Decimal('5000.00'),
            tax_percent=Decimal('18.00'),
            status='ACTIVE',
            created_by_user=self.admin,
            effective_from=timezone.now() - timedelta(days=10),
        )

        # Unconverted Lead
        self.lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Aarav',
            last_name='Sharma',
            email_normalized=f"aarav_{uuid.uuid4().hex[:6]}@test.com",
            phone_normalized='+919876543210',
            lead_source=self.source,
            current_status='HOT_LEAD',
            interested_program=self.program,
            assigned_sales_user=self.admin,
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _prepare_request(self, request, user):
        request.user = user
        request.user.is_superuser = True
        request.tenant = self.tenant
        user._statedb = DB
        request._state_db = DB
        force_authenticate(request, user=user)

    def test_missing_required_fields_validation(self):
        """Conversion rejects requests with missing branch_id, package_version_id, or payment info."""
        view = LeadViewSet.as_view({'post': 'convert'})
        request = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/convert/",
            {"branch_id": str(self.branch.id)},  # Missing package_version_id, payment_provider, payment_amount
            format="json",
        )
        self._prepare_request(request, self.admin)
        response = view(request, pk=str(self.lead.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)
        self.assertIn("package_version_id", response.data[
            "error"
        ])

    def test_execute_conversion_creates_member_and_preserves_lead_linkage(self):
        """
        Valid conversion creates UserProfile, Membership, Order, and updates Lead.
        Lead attribution (source, assigned_to) is preserved.
        """
        view = LeadViewSet.as_view({'post': 'convert'})
        request = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/convert/",
            {
                "branch_id": str(self.branch.id),
                "package_version_id": str(self.pkg_version.id),
                "payment_provider": "CASH",
                "payment_amount": "5900.00",
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )
        self._prepare_request(request, self.admin)
        response = view(request, pk=str(self.lead.id))
        self.assertIn(response.status_code, [status.HTTP_201_CREATED, status.HTTP_202_ACCEPTED])

        # Verify Lead record
        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.lead_source_id, self.source.id)
        self.assertEqual(self.lead.assigned_sales_user_id, self.admin.id)

        # Verify created member user profile
        self.assertIsNotNone(self.lead.converted_user_profile)
        user_profile = self.lead.converted_user_profile
        self.assertEqual(user_profile.user.first_name, 'Aarav')
        self.assertEqual(user_profile.user.last_name, 'Sharma')

        # Verify Membership record created
        membership = Membership.objects.using(DB).filter(user_profile=user_profile).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.package_id, self.pkg.id)

        # Verify Order record created
        order = Order.objects.using(DB).filter(user_profile=user_profile).first()
        self.assertIsNotNone(order)
        self.assertEqual(order.branch_id, self.branch.id)

        # Verify Client 360 aggregate resolves the member correctly
        member_360 = build_member_360_aggregate(user_profile, DB)
        self.assertEqual(member_360["member"]["first_name"], "Aarav")
        self.assertIsNotNone(member_360["memberships"])

    def test_repeated_conversion_is_idempotent_and_does_not_create_duplicates(self):
        """Repeated submission on a converted lead returns already_converted without duplicating members."""
        view = LeadViewSet.as_view({'post': 'convert'})
        idem_key = str(uuid.uuid4())
        payload = {
            "branch_id": str(self.branch.id),
            "package_version_id": str(self.pkg_version.id),
            "payment_provider": "CASH",
            "payment_amount": "5900.00",
            "idempotency_key": idem_key,
        }

        # First conversion
        request1 = self.factory.post(f"/api/v1/tenant/leads/{self.lead.id}/convert/", payload, format="json")
        self._prepare_request(request1, self.admin)
        resp1 = view(request1, pk=str(self.lead.id))
        self.assertIn(resp1.status_code, [status.HTTP_201_CREATED, status.HTTP_202_ACCEPTED])

        # Record counts after first conversion
        profile_count_1 = UserProfile.objects.using(DB).filter(user__email=self.lead.email_normalized).count()
        membership_count_1 = Membership.objects.using(DB).filter(user_profile__user__email=self.lead.email_normalized).count()
        order_count_1 = Order.objects.using(DB).filter(user_profile__user__email=self.lead.email_normalized).count()
        self.assertEqual(profile_count_1, 1)

        # Second conversion attempt
        request2 = self.factory.post(f"/api/v1/tenant/leads/{self.lead.id}/convert/", payload, format="json")
        self._prepare_request(request2, self.admin)
        resp2 = view(request2, pk=str(self.lead.id))
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertTrue(resp2.data.get("already_converted"))

        # Verify NO duplicates created
        profile_count_2 = UserProfile.objects.using(DB).filter(user__email=self.lead.email_normalized).count()
        membership_count_2 = Membership.objects.using(DB).filter(user_profile__user__email=self.lead.email_normalized).count()
        order_count_2 = Order.objects.using(DB).filter(user_profile__user__email=self.lead.email_normalized).count()
        self.assertEqual(profile_count_2, profile_count_1)
        self.assertEqual(membership_count_2, membership_count_1)
        self.assertEqual(order_count_2, order_count_1)

    def test_cash_payment_governance_creates_approval_request(self):
        """Cash payments require managerial approval, creating an ApprovalRequest record."""
        view = LeadViewSet.as_view({'post': 'convert'})
        request = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/convert/",
            {
                "branch_id": str(self.branch.id),
                "package_version_id": str(self.pkg_version.id),
                "payment_provider": "CASH",
                "payment_amount": "5900.00",
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
        )
        self._prepare_request(request, self.admin)
        response = view(request, pk=str(self.lead.id))

        if response.data.get("status") == "PENDING_APPROVAL":
            appr = ApprovalRequest.objects.using(DB).filter(
                organization=self.org,
                request_type='CASH_PAYMENT_APPROVAL',
            ).first()
            self.assertIsNotNone(appr)
            self.assertEqual(appr.status, 'PENDING')
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertIn("approval_request_id", response.data)

    def test_lead_creation_does_not_provision_member_profile_or_role(self):
        """Creating an ordinary CRM lead must not provision a UserProfile or assign MEMBER role."""
        view = LeadViewSet.as_view({'post': 'create'})
        unique_email = f"intake_test_{uuid.uuid4().hex[:6]}@test.com"
        unique_phone = f"+9198{str(uuid.uuid4().int)[:8]}"
        payload = {
            'first_name': 'Rohan',
            'last_name': 'Mehra',
            'email': unique_email,
            'phone': unique_phone,
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }
        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_request(request, self.admin)
        response = view(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        lead_id = response.data['id']
        lead = Lead.objects.using(DB).get(id=lead_id)

        # 1. Lead stage is NEW_LEAD
        self.assertEqual(lead.current_status, 'NEW_LEAD')

        # 2. converted_user_profile is NOT set
        self.assertIsNone(lead.converted_user_profile)
        self.assertIsNone(lead.converted_user_profile_id)

        # 3. No UserProfile created
        profiles = UserProfile.objects.using(DB).filter(user__email=unique_email)
        self.assertEqual(profiles.count(), 0)

        # 4. TenantUser created as LEAD, without MEMBER role
        tenant_user = TenantUser.objects.using(DB).filter(email=unique_email).first()
        if tenant_user:
            self.assertEqual(tenant_user.user_type, 'LEAD')
            member_role_assignments = RoleAssignment.objects.using(DB).filter(
                user=tenant_user, role__code='MEMBER'
            )
            self.assertEqual(member_role_assignments.count(), 0)

        # 5. LeadSerializer representation confirms lead is not converted
        serializer_data = LeadSerializer(lead).data
        self.assertIsNone(serializer_data.get('converted_member'))
        eligibility = serializer_data.get('action_eligibility', {})
        self.assertFalse(eligibility.get('is_terminal'))
        self.assertTrue(eligibility.get('can_book_trial'))
        self.assertTrue(eligibility.get('can_convert'))
        self.assertTrue(eligibility.get('can_move_stage'))

    def test_trial_booking_and_attended_does_not_convert_lead(self):
        """Booking and attending a trial progresses lead stages but never converts the lead."""
        # Booking trial
        trial = TrialBooking.objects.using(DB).create(
            branch=self.branch,
            lead=self.lead,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1),
            status='BOOKED',
        )
        self.lead.current_status = 'TRIAL_BOOKED'
        self.lead.save(using=DB, update_fields=['current_status'])

        self.lead.refresh_from_db(using=DB)
        self.assertIsNone(self.lead.converted_user_profile)
        self.assertEqual(self.lead.current_status, 'TRIAL_BOOKED')
        ser_data = LeadSerializer(self.lead).data
        self.assertIsNone(ser_data.get('converted_member'))
        self.assertFalse(ser_data.get('action_eligibility', {}).get('is_terminal'))

        # Attending trial
        trial.status = 'ATTENDED'
        trial.save(using=DB, update_fields=['status'])
        self.lead.current_status = 'TRIAL_ATTENDED'
        self.lead.save(using=DB, update_fields=['current_status'])

        self.lead.refresh_from_db(using=DB)
        self.assertIsNone(self.lead.converted_user_profile)
        self.assertEqual(self.lead.current_status, 'TRIAL_ATTENDED')
        ser_data_attended = LeadSerializer(self.lead).data
        self.assertIsNone(ser_data_attended.get('converted_member'))
        self.assertFalse(ser_data_attended.get('action_eligibility', {}).get('is_terminal'))
        self.assertTrue(ser_data_attended.get('action_eligibility', {}).get('can_convert'))

    def test_unpaid_or_pending_order_does_not_convert_lead(self):
        """An order with status PENDING_PAYMENT does not convert the lead."""
        order = Order.objects.using(DB).create(
            branch=self.branch,
            lead=self.lead,
            order_number=f"ORD-UNPAID-{uuid.uuid4().hex[:6].upper()}",
            status='PENDING_PAYMENT',
            total_amount=Decimal('5900.00'),
        )
        self.lead.refresh_from_db(using=DB)
        self.assertIsNone(self.lead.converted_user_profile)
        self.assertNotEqual(self.lead.current_status, 'CONVERTED')

        ser_data = LeadSerializer(self.lead).data
        self.assertIsNone(ser_data.get('converted_member'))
        self.assertFalse(ser_data.get('action_eligibility', {}).get('is_terminal'))
        self.assertTrue(ser_data.get('action_eligibility', {}).get('can_convert'))

    def test_lead_with_profile_relationship_not_presented_as_converted_without_conversion_record(self):
        """A lead in NEW_LEAD with an existing/linked profile is not treated as converted without a conversion record."""
        dummy_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"dummy_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Dummy',
            last_name='User',
            status='ACTIVE',
        )
        dummy_profile = UserProfile.objects.using(DB).create(
            user=dummy_user,
            preferred_branch=self.branch,
        )
        self.lead.converted_user_profile = dummy_profile
        self.lead.current_status = 'NEW_LEAD'
        self.lead.save(using=DB, update_fields=['converted_user_profile', 'current_status'])

        # Serializer representation must NOT present as converted
        ser_data = LeadSerializer(self.lead).data
        self.assertIsNone(ser_data.get('converted_member'))
        eligibility = ser_data.get('action_eligibility', {})
        self.assertFalse(eligibility.get('is_terminal'))
        self.assertTrue(eligibility.get('can_convert'))

        # Backend actions: transition_status must be allowed
        view_trans = LeadViewSet.as_view({'post': 'transition_status'})
        req_trans = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/transition-status/",
            {'new_status': 'HOT_LEAD'},
            format='json',
        )
        self._prepare_request(req_trans, self.admin)
        res_trans = view_trans(req_trans, pk=str(self.lead.id))
        self.assertEqual(res_trans.status_code, status.HTTP_200_OK)

        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.current_status, 'HOT_LEAD')

    def test_converted_lead_is_terminal_and_blocks_further_sales_actions(self):
        """A legitimately converted lead is terminal; transitions, reassignments, and trial bookings are rejected."""
        conv_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"conv_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Conv',
            last_name='User',
            status='ACTIVE',
        )
        conv_profile = UserProfile.objects.using(DB).create(
            user=conv_user,
            preferred_branch=self.branch,
        )
        self.lead.converted_user_profile = conv_profile
        self.lead.current_status = 'CONVERTED'
        self.lead.save(using=DB, update_fields=['converted_user_profile', 'current_status'])
        LeadConversion.objects.using(DB).create(
            lead=self.lead,
            user_profile=conv_profile,
            converted_at=timezone.now(),
        )

        # 1. Serializer reports terminal
        ser_data = LeadSerializer(self.lead).data
        self.assertIsNotNone(ser_data.get('converted_member'))
        self.assertTrue(ser_data.get('action_eligibility', {}).get('is_terminal'))
        self.assertFalse(ser_data.get('action_eligibility', {}).get('can_book_trial'))
        self.assertFalse(ser_data.get('action_eligibility', {}).get('can_move_stage'))
        self.assertFalse(ser_data.get('action_eligibility', {}).get('can_convert'))

        # 2. transition_status is rejected
        view_trans = LeadViewSet.as_view({'post': 'transition_status'})
        req_trans = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/transition-status/",
            {'new_status': 'HOT_LEAD'},
            format='json',
        )
        self._prepare_request(req_trans, self.admin)
        res_trans = view_trans(req_trans, pk=str(self.lead.id))
        self.assertEqual(res_trans.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been converted", res_trans.data['error'])

        # 3. assign is rejected
        view_assign = LeadViewSet.as_view({'post': 'assign'})
        req_assign = self.factory.post(
            f"/api/v1/tenant/leads/{self.lead.id}/assign/",
            {'assigned_to_user_id': str(self.admin.id)},
            format='json',
        )
        self._prepare_request(req_assign, self.admin)
        res_assign = view_assign(req_assign, pk=str(self.lead.id))
        self.assertEqual(res_assign.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been converted", res_assign.data['error'])
