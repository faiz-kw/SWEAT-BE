"""
Regression Test Suite for SWEAT Members Module Audit & Hardening.
Tests:
1. Manager Approval Materialization: Extension, Freeze (with booking cancellation), Upgrade, and Freeze Rejection.
2. Booking Eligibility: Past membership end_date and within active/scheduled freeze windows.
3. Member Identity Lookup: TenantUser.id fallback lookup in MemberViewSet.
4. CRM Lead Conversion: Bidirectional linking (lead.converted_user_profile & LeadConversion).
"""
import uuid
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from django.core.exceptions import ValidationError

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageEntitlementDefinition,
)
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_memberships import (
    Membership,
    MembershipEntitlement,
    MembershipFreeze,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipStatusHistory,
    MembershipPackageHistory,
)
from apps.tenant_core.models_approvals import ApprovalRequest, ApprovalAction
from apps.tenant_core.models_crm import Lead, LeadConversion
from apps.tenant_core.services_approvals import AdminApprovalService
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService


class MembersModuleAuditRegressionTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master Tenant Setup
        self.tenant, _ = Tenant.objects.using('default').get_or_create(
            name="Audit Test Gym",
            slug="audit-gym",
            status="ACTIVE",
            defaults={"code": "AUDIT-GYM"}
        )
        self.ds, _ = TenantDataSource.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={"db_name": "test_fitness_tenant", "database_name": "test_fitness_tenant", "status": "ACTIVE"}
        )
        if self.ds.db_name != 'test_fitness_tenant' or self.ds.database_name != 'test_fitness_tenant':
            self.ds.db_name = 'test_fitness_tenant'
            self.ds.database_name = 'test_fitness_tenant'
            self.ds.status = 'ACTIVE'
            self.ds.save(using='default')

        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='AUDIT-PLAN',
            defaults={'name': 'Audit Plan', 'tier': 'ENTERPRISE', 'status': 'ACTIVE'}
        )
        self.sub, _ = TenantSubscription.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={'plan': self.plan, 'status': 'ACTIVE'}
        )
        if self.sub.status != 'ACTIVE':
            self.sub.status = 'ACTIVE'
            self.sub.save(using='default')

        for code, name in [
            ('core', 'Core Platform'),
            ('members', 'Member Operations'),
            ('memberships', 'Memberships & Changes'),
            ('commerce', 'Commerce & Billing'),
            ('finance', 'Finance & Payments'),
            ('crm', 'CRM & Pipeline'),
            ('cs', 'Customer Success & Health'),
            ('classes', 'Classes & Bookings'),
        ]:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=code, defaults={'name': name, 'status': 'ACTIVE', 'is_core': True}
            )
            tm, _ = TenantModule.objects.using('default').get_or_create(
                tenant=self.tenant, module=pm, defaults={'status': 'ENABLED', 'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'}
            )
            if not tm.is_enabled or tm.status != 'ENABLED':
                tm.status = 'ENABLED'
                tm.is_enabled = True
                tm.save(using='default')
            ModuleCatalog.objects.using('tenant_test').get_or_create(
                module_code=code, defaults={'name': name, 'source_module_id': uuid.uuid4(), 'is_enabled': True, 'status': 'ACTIVE'}
            )

        # 2. Org & Branch
        self.org, _ = Organization.objects.using('tenant_test').get_or_create(
            name="Audit Org",
            defaults={"code": "AUD_ORG", "status": "ACTIVE"}
        )
        self.loc, _ = Location.objects.using('tenant_test').get_or_create(
            organization=self.org,
            name="Audit Campus",
            defaults={"code": "AUD_CAMP"}
        )
        self.branch, _ = Branch.objects.using('tenant_test').get_or_create(
            organization=self.org,
            location=self.loc,
            name="Audit Branch",
            defaults={"code": "AUD_BR", "status": "ACTIVE"}
        )

        # 3. Staff Admin User
        self.admin_user, _ = TenantUser.objects.using('tenant_test').get_or_create(
            email="admin@audit.test",
            defaults={"username": "audit_admin", "status": "ACTIVE", "organization": self.org}
        )

        # Roles & Permissions for Admin
        core_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='core')
        members_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='members')
        crm_mod = ModuleCatalog.objects.using('tenant_test').get(module_code='crm')

        sub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=core_mod, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True}
        )
        sub_360, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=members_mod, submodule_code='client-360', defaults={'name': 'Member 360', 'is_enabled': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=crm_mod, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )

        perm_core_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.view",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.view", "action": "view", "label": "View Users", "is_active": True}
        )
        perm_core_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.edit",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.edit", "action": "edit", "label": "Edit Users", "is_active": True}
        )
        perm_core_create, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="core.users.create",
            defaults={"module": core_mod, "submodule": sub_users, "source_permission_id": uuid.uuid4(), "code": "core.users.create", "action": "create", "label": "Create Users", "is_active": True}
        )
        perm_360_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="members.client-360.view",
            defaults={"module": members_mod, "submodule": sub_360, "source_permission_id": uuid.uuid4(), "code": "members.client-360.view", "action": "view", "label": "View 360", "is_active": True}
        )
        perm_leads_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code="crm.leads.view",
            defaults={"module": crm_mod, "submodule": sub_leads, "source_permission_id": uuid.uuid4(), "code": "crm.leads.view", "action": "view", "label": "View Leads", "is_active": True}
        )

        self.org_admin_role, _ = Role.objects.using('tenant_test').get_or_create(
            organization=self.org,
            code="AUDIT_ADMIN",
            defaults={"name": "Audit Admin", "scope": "ORG", "is_system_role": True, "is_active": True}
        )
        admin_ps, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.org_admin_role, name="Audit Admin PSet", defaults={"is_active": True}
        )
        for m in [core_mod, members_mod, crm_mod]:
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=self.org_admin_role, module=m, defaults={"permission_set": admin_ps, "can_access": True, "is_visible": True}
            )
        for s in [sub_users, sub_360, sub_leads]:
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=self.org_admin_role, submodule=s, defaults={"permission_set": admin_ps, "can_access": True, "is_visible": True}
            )
        for p in [perm_core_view, perm_core_edit, perm_core_create, perm_360_view, perm_leads_view]:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=admin_ps, permission=p, defaults={"granted": True, "is_allowed": True}
            )
        RoleAssignment.objects.using('tenant_test').get_or_create(
            user=self.admin_user, role=self.org_admin_role, defaults={"organization": self.org, "is_active": True, "status": "ACTIVE"}
        )

        admin_refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test'
        )
        self.admin_token = str(admin_refresh.access_token)

        # 4. Member Profile
        self.member_user, _ = TenantUser.objects.using('tenant_test').get_or_create(
            email="member@audit.test",
            defaults={"username": "audit_member", "first_name": "Audit", "last_name": "Member", "status": "ACTIVE", "organization": self.org}
        )
        self.member_profile, _ = UserProfile.objects.using('tenant_test').get_or_create(
            user=self.member_user,
            defaults={"member_number": "MEM-AUDIT-001", "preferred_branch": self.branch, "member_status": "ACTIVE", "member_type": "MEMBER"}
        )

        # 5. Packages
        self.category, _ = ProgramCategory.objects.using('tenant_test').get_or_create(
            organization=self.org, name="Fitness", defaults={"code": "FIT"}
        )
        self.prog, _ = Program.objects.using('tenant_test').get_or_create(
            organization=self.org, category=self.category, name="CrossFit", defaults={"code": "CF", "status": "ACTIVE"}
        )
        self.pkg_standard, _ = Package.objects.using('tenant_test').get_or_create(
            organization=self.org, program=self.prog, name="Standard Package", defaults={"code": "STD", "status": "ACTIVE"}
        )
        self.pkg_std_v1, _ = PackageVersion.objects.using('tenant_test').get_or_create(
            package=self.pkg_standard, version_number=1, defaults={"duration_value": 30, "duration_unit": "DAY", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )
        self.pkg_std_price, _ = PackagePrice.objects.using('tenant_test').get_or_create(
            package_version=self.pkg_std_v1, defaults={"base_price": Decimal("3000.00"), "currency": "INR", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )

        self.pkg_vip, _ = Package.objects.using('tenant_test').get_or_create(
            organization=self.org, program=self.prog, name="VIP Package", defaults={"code": "VIP", "status": "ACTIVE"}
        )
        self.pkg_vip_v1, _ = PackageVersion.objects.using('tenant_test').get_or_create(
            package=self.pkg_vip, version_number=1, defaults={"duration_value": 60, "duration_unit": "DAY", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )
        self.pkg_vip_price, _ = PackagePrice.objects.using('tenant_test').get_or_create(
            package_version=self.pkg_vip_v1, defaults={"base_price": Decimal("6000.00"), "currency": "INR", "status": "ACTIVE", "effective_from": timezone.now(), "created_by_user": self.admin_user}
        )

        # Class Template & Occurrence
        self.class_cat, _ = ClassCategory.objects.using('tenant_test').get_or_create(
            organization=self.org, name="HIIT", defaults={"code": "HIIT"}
        )
        self.template, _ = ClassTemplate.objects.using('tenant_test').get_or_create(
            name="Morning Blast", category=self.class_cat, organization=self.org, program=self.prog, defaults={"default_capacity": 10, "status": "ACTIVE"}
        )

    def _create_membership(self, end_date=None, status="ACTIVE", parq_status="COMPLETED"):
        today = timezone.now().date()
        end = end_date or (today + timedelta(days=30))
        m = Membership.objects.using('tenant_test').create(
            user_profile=self.member_profile,
            package=self.pkg_standard,
            package_version=self.pkg_std_v1,
            program=self.prog,
            home_branch=self.branch,
            purchase_branch=self.branch,
            membership_number=f"MS-{uuid.uuid4().hex[:6].upper()}",
            start_date=today,
            end_date=end,
            status=status,
            parq_status=parq_status,
        )
        MembershipEntitlement.objects.using('tenant_test').create(
            membership=m,
            entitlement_type="HOME_BRANCH_SESSION",
            allocated_units=Decimal("20.00"),
            consumed_units=Decimal("0.00"),
            status="ACTIVE",
            valid_from=today,
            valid_until=end,
            is_unlimited=False,
        )
        return m

    def test_upgrade_approval_materializes_package(self):
        """Approval of MEMBERSHIP_UPGRADE applies package change and policy rule."""
        m = self._create_membership()
        self.assertEqual(m.package, self.pkg_standard)

        req = ApprovalRequest.objects.using('tenant_test').create(
            organization=self.org,
            request_type='MEMBERSHIP_UPGRADE',
            status='PENDING',
            entity_type='Membership',
            entity_id=m.id,
            requested_by_user=self.member_user,
            requested_payload={
                'current_membership_id': str(m.id),
                'target_package_id': str(self.pkg_vip.id),
                'target_version_id': str(self.pkg_vip_v1.id),
                'reason': 'Customer requested VIP tier',
            },
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.admin_user,
            action='APPROVED',
            comment='Upgrade approved',
            db_alias='tenant_test',
        )

        req.refresh_from_db(using='tenant_test')
        self.assertEqual(req.status, 'APPROVED')

        m.refresh_from_db(using='tenant_test')
        self.assertEqual(m.package, self.pkg_vip)
        self.assertEqual(m.package_version, self.pkg_vip_v1)

    def test_extension_approval_materializes_end_date(self):
        """Approval of MEMBERSHIP_EXTENSION by manager extends membership end_date."""
        m = self._create_membership()
        original_end = m.end_date

        req = ApprovalRequest.objects.using('tenant_test').create(
            organization=self.org,
            request_type='MEMBERSHIP_EXTENSION',
            status='PENDING',
            entity_type='Membership',
            entity_id=m.id,
            requested_by_user=self.member_user,
            requested_payload={'days': 14, 'reason': 'Medical recovery extension'},
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.admin_user,
            action='APPROVED',
            comment='Verified medical documentation',
            db_alias='tenant_test',
        )

        req.refresh_from_db(using='tenant_test')
        self.assertEqual(req.status, 'APPROVED')

        m.refresh_from_db(using='tenant_test')
        self.assertEqual(m.end_date, original_end + timedelta(days=14))

    def test_freeze_approval_materializes_freeze_and_cancels_overlapping_booking(self):
        """Approval of MEMBERSHIP_FREEZE activates/schedules freeze and cancels overlapping bookings."""
        today = timezone.now().date()
        m = self._create_membership()
        original_end = m.end_date

        freeze_from = today + timedelta(days=2)
        freeze_until = today + timedelta(days=12)
        freeze_days = (freeze_until - freeze_from).days

        freeze = MembershipFreeze.objects.using('tenant_test').create(
            membership=m,
            freeze_from=freeze_from,
            freeze_until=freeze_until,
            reason_text="Work travel",
            status='SCHEDULED',
            extend_membership_days=freeze_days,
        )

        # Create an overlapping class occurrence and booking
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            start_at=timezone.now() + timedelta(days=5),
            end_at=timezone.now() + timedelta(days=5, hours=1),
            capacity=10,
            status='SCHEDULED',
        )
        booking = Booking.objects.using('tenant_test').create(
            branch=self.branch,
            user_profile=self.member_profile,
            occurrence=occ,
            booking_number=f"BK-{uuid.uuid4().hex[:6].upper()}",
            booking_type='MEMBER',
            membership=m,
            status='CONFIRMED',
        )

        req = ApprovalRequest.objects.using('tenant_test').create(
            organization=self.org,
            request_type='MEMBERSHIP_FREEZE',
            status='PENDING',
            entity_type='Membership',
            entity_id=m.id,
            requested_by_user=self.member_user,
            requested_payload={'freeze_id': str(freeze.id), 'from_date': str(freeze_from), 'to_date': str(freeze_until)},
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.admin_user,
            action='APPROVED',
            comment='Approved freeze for travel',
            db_alias='tenant_test',
        )

        freeze.refresh_from_db(using='tenant_test')
        self.assertEqual(freeze.status, 'SCHEDULED')
        self.assertEqual(freeze.approved_by_user, self.admin_user)

        m.refresh_from_db(using='tenant_test')
        self.assertEqual(m.end_date, original_end + timedelta(days=freeze_days))

        booking.refresh_from_db(using='tenant_test')
        self.assertEqual(booking.status, 'CANCELLED')

    def test_freeze_rejection_marks_freeze_cancelled(self):
        """Rejection of MEMBERSHIP_FREEZE cancels the MembershipFreeze record so future requests aren't blocked."""
        today = timezone.now().date()
        m = self._create_membership()

        freeze = MembershipFreeze.objects.using('tenant_test').create(
            membership=m,
            freeze_from=today + timedelta(days=5),
            freeze_until=today + timedelta(days=15),
            reason_text="Vacation",
            status='SCHEDULED',
        )

        req = ApprovalRequest.objects.using('tenant_test').create(
            organization=self.org,
            request_type='MEMBERSHIP_FREEZE',
            status='PENDING',
            entity_type='Membership',
            entity_id=m.id,
            requested_by_user=self.member_user,
            requested_payload={'freeze_id': str(freeze.id)},
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.admin_user,
            action='REJECTED',
            comment='Freeze request denied',
            db_alias='tenant_test',
        )

        req.refresh_from_db(using='tenant_test')
        self.assertEqual(req.status, 'REJECTED')

        freeze.refresh_from_db(using='tenant_test')
        self.assertEqual(freeze.status, 'CANCELLED')

    def test_booking_rejected_when_occurrence_date_is_past_membership_end_date(self):
        """Booking is rejected if class occurrence occurs after the membership end_date."""
        today = timezone.now().date()
        m = self._create_membership(end_date=today + timedelta(days=2))

        # Class occurrence scheduled in 7 days (membership will be expired on that date)
        future_occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            start_at=timezone.now() + timedelta(days=7),
            end_at=timezone.now() + timedelta(days=7, hours=1),
            capacity=10,
            status='SCHEDULED',
        )

        # 1. evaluate_occurrence_eligibility
        eligibility = BookingWaitlistAttendanceService.evaluate_occurrence_eligibility(
            occurrence=future_occ,
            membership=m,
            user_profile=self.member_profile,
            db_alias='tenant_test',
        )
        self.assertFalse(eligibility['eligible'])
        self.assertEqual(eligibility['reason_code'], 'MEMBERSHIP_EXPIRED_ON_DATE')

        # 2. create_booking raises ValidationError
        with self.assertRaises(ValidationError) as ctx:
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=future_occ,
                membership=m,
                db_alias='tenant_test',
            )
        self.assertIn("Membership expires", str(ctx.exception))

    def test_booking_rejected_during_active_or_scheduled_freeze_window(self):
        """Booking is rejected if class occurrence falls within a scheduled freeze period."""
        today = timezone.now().date()
        m = self._create_membership(end_date=today + timedelta(days=60))

        freeze_start = today + timedelta(days=10)
        freeze_end = today + timedelta(days=20)
        MembershipFreeze.objects.using('tenant_test').create(
            membership=m,
            freeze_from=freeze_start,
            freeze_until=freeze_end,
            status='SCHEDULED',
        )

        frozen_occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            start_at=timezone.now() + timedelta(days=15),
            end_at=timezone.now() + timedelta(days=15, hours=1),
            capacity=10,
            status='SCHEDULED',
        )

        # 1. evaluate_occurrence_eligibility
        eligibility = BookingWaitlistAttendanceService.evaluate_occurrence_eligibility(
            occurrence=frozen_occ,
            membership=m,
            user_profile=self.member_profile,
            db_alias='tenant_test',
        )
        self.assertFalse(eligibility['eligible'])
        self.assertEqual(eligibility['reason_code'], 'MEMBERSHIP_FROZEN_ON_DATE')

        # 2. create_booking raises ValidationError
        with self.assertRaises(ValidationError) as ctx:
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=frozen_occ,
                membership=m,
                db_alias='tenant_test',
            )
        self.assertIn("freeze window", str(ctx.exception))

    def test_member_lookup_by_tenant_user_id_fallback(self):
        """MemberViewSet resolves member profile even when TenantUser UUID is provided as pk."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        response = self.client.get(f"/api/v1/tenant/members/{self.member_user.id}/360/")
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['member']['member_number'], self.member_profile.member_number)

    def test_member_creation_from_lead_links_bidirectionally(self):
        """POST /api/v1/tenant/members/ with from_lead_id sets converted status, links profile, and creates LeadConversion."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            first_name="Jane",
            last_name="Convert",
            email_normalized="jane.convert@test.com",
            phone_normalized="+919876543210",
            current_status="ENGAGED",
        )

        response = self.client.post(
            "/api/v1/tenant/members/",
            {
                "name": "Jane Convert",
                "email": "jane.convert@test.com",
                "phone": "+919876543210",
                "gender": "F",
                "location": str(self.branch.id),
                "from_lead_id": str(lead.id),
            },
            format='json',
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.current_status, 'CONVERTED')
        self.assertIsNotNone(lead.converted_user_profile)
        self.assertEqual(lead.converted_user_profile.user.email, "jane.convert@test.com")

        conv = LeadConversion.objects.using('tenant_test').filter(lead=lead).first()
        self.assertIsNotNone(conv)
        self.assertEqual(conv.user_profile, lead.converted_user_profile)

    def test_mobile_schedule_returns_occurrences_for_target_date(self):
        """GET /api/v1/mobile/schedule/?date=YYYY-MM-DD returns 200 with slots for target date."""
        today = timezone.now().date()
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=today,
            start_at=timezone.now().replace(hour=9, minute=0, second=0),
            end_at=timezone.now().replace(hour=10, minute=0, second=0),
            capacity=12,
            status='OPEN',
        )

        response = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('slots', data)
        self.assertIn('categories', data)
        self.assertGreaterEqual(data['classes_count'], 1)
        slot_ids = [s['id'] for s in data['slots']]
        self.assertIn(str(occ.id), slot_ids)

        # Verify envelope shape
        target_slot = next(s for s in data['slots'] if s['id'] == str(occ.id))
        self.assertEqual(target_slot['class_name'], self.template.name)
        self.assertEqual(target_slot['category'], self.class_cat.name)
        self.assertEqual(target_slot['branch']['id'], str(self.branch.id))
        self.assertEqual(target_slot['capacity']['total_capacity'], 12)
        self.assertEqual(target_slot['capacity']['available_seats'], 12)

    def test_mobile_schedule_category_filtering_exact_partial_code(self):
        """GET /api/v1/mobile/schedule/ filters by category UUID, exact name, partial icontains name, and code."""
        today = timezone.now().date()
        cat_reformer = ClassCategory.objects.using('tenant_test').create(
            organization=self.org, name="Reformer Pilates", code="CC-REFORM"
        )
        cat_hiit = ClassCategory.objects.using('tenant_test').create(
            organization=self.org, name="Bootcamp HIIT & Conditioning", code="CC-HIIT"
        )
        tmpl_reformer = ClassTemplate.objects.using('tenant_test').create(
            code="CLS-REF-REG", name="Sweat Reformer", category=cat_reformer, organization=self.org, program=self.prog, default_capacity=8, status='ACTIVE'
        )
        tmpl_hiit = ClassTemplate.objects.using('tenant_test').create(
            code="CLS-HIIT-REG", name="Sweat Bootcamp", category=cat_hiit, organization=self.org, program=self.prog, default_capacity=16, status='ACTIVE'
        )
        occ_ref = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tmpl_reformer, branch=self.branch, occurrence_date=today,
            start_at=timezone.now().replace(hour=7, minute=0, second=0),
            end_at=timezone.now().replace(hour=8, minute=0, second=0),
            capacity=8, status='OPEN'
        )
        occ_hiit = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tmpl_hiit, branch=self.branch, occurrence_date=today,
            start_at=timezone.now().replace(hour=8, minute=0, second=0),
            end_at=timezone.now().replace(hour=9, minute=0, second=0),
            capacity=16, status='OPEN'
        )

        # 1. Filter by category UUID
        res_uuid = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}&category={cat_reformer.id}",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_uuid.status_code, status.HTTP_200_OK)
        ids_uuid = [s['id'] for s in res_uuid.json()['slots']]
        self.assertIn(str(occ_ref.id), ids_uuid)
        self.assertNotIn(str(occ_hiit.id), ids_uuid)

        # 2. Filter by partial category name ('Bootcamp HIIT' matching 'Bootcamp HIIT & Conditioning')
        res_partial = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}&category=Bootcamp+HIIT",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_partial.status_code, status.HTTP_200_OK)
        ids_partial = [s['id'] for s in res_partial.json()['slots']]
        self.assertIn(str(occ_hiit.id), ids_partial)
        self.assertNotIn(str(occ_ref.id), ids_partial)

        # 3. Filter by category code ('CC-REFORM')
        res_code = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}&category=CC-REFORM",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_code.status_code, status.HTTP_200_OK)
        ids_code = [s['id'] for s in res_code.json()['slots']]
        self.assertIn(str(occ_ref.id), ids_code)
        self.assertNotIn(str(occ_hiit.id), ids_code)

        # 4. Filter by non-existent category
        res_none = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}&category=NONEXISTENT_FORMAT",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_none.status_code, status.HTTP_200_OK)
        self.assertEqual(res_none.json()['classes_count'], 0)

    def test_mobile_schedule_branch_filter_and_empty_date(self):
        """GET /api/v1/mobile/schedule/ filters by branch and handles genuine empty days with 200 status."""
        today = timezone.now().date()
        branch_empty = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc, name="Empty Branch", code="EMP_BR", status="ACTIVE"
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template, branch=self.branch, occurrence_date=today,
            start_at=timezone.now().replace(hour=11, minute=0, second=0),
            end_at=timezone.now().replace(hour=12, minute=0, second=0),
            capacity=10, status='OPEN'
        )

        # Query empty branch
        res_emp_br = self.client.get(
            f"/api/v1/mobile/schedule/?date={today.isoformat()}&branch_id={branch_empty.id}",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_emp_br.status_code, status.HTTP_200_OK)
        self.assertEqual(res_emp_br.json()['classes_count'], 0)

        # Query future date with zero occurrences
        far_future = today + timedelta(days=90)
        res_future = self.client.get(
            f"/api/v1/mobile/schedule/?date={far_future.isoformat()}",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(res_future.status_code, status.HTTP_200_OK)
        self.assertEqual(res_future.json()['classes_count'], 0)
        self.assertEqual(len(res_future.json()['slots']), 0)
