import uuid
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    Organization,
    Location,
    Branch,
    TenantUser,
    ApprovalRequest,
    ApprovalAction,
)
from apps.tenant_core.services_approvals import AdminApprovalService


class Layer2Phase11ApprovalsTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="Sweat Governance Org",
            code=f"GOV-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="HQ Campus",
            city="Metro",
            status="ACTIVE"
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name="HQ Branch",
            code=f"BRN-{uuid.uuid4().hex[:4]}",
            timezone="UTC"
        )

        # Staff User 1 (Requester)
        self.requester = TenantUser.objects.create(
            organization=self.org,
            email=f"requester-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Alice",
            last_name="Staff",
            status="ACTIVE"
        )

        # Staff User 2 (Approver 1)
        self.approver1 = TenantUser.objects.create(
            organization=self.org,
            email=f"manager-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Bob",
            last_name="Manager",
            status="ACTIVE"
        )

        # Staff User 3 (Approver 2)
        self.approver2 = TenantUser.objects.create(
            organization=self.org,
            email=f"director-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Carol",
            last_name="Director",
            status="ACTIVE"
        )

    def test_create_approval_request(self):
        """Creates an approval request with pending status and audit trail."""
        target_id = uuid.uuid4()
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='REFUND_OVERRIDE',
            entity_type='Order',
            entity_id=target_id,
            requested_by_user=self.requester,
            requested_payload={'amount': '150.00', 'reason': 'Customer dissatisfaction'},
            required_approvals=1
        )

        self.assertIsNotNone(req.id)
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(req.required_approvals, 1)
        self.assertIsNone(req.resolved_at)

    def test_segregation_of_duties_prevents_self_approval(self):
        """Requesters cannot approve their own requests unless explicitly allowed."""
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='DISCOUNT_EXCEPTION',
            entity_type='DiscountCampaign',
            entity_id=uuid.uuid4(),
            requested_by_user=self.requester,
            required_approvals=1
        )

        with self.assertRaises(ValidationError) as ctx:
            AdminApprovalService.process_action(
                approval_request=req,
                approver_user=self.requester,
                action='APPROVED',
                comment='Self-approving my own exception'
            )
        self.assertIn("Segregation of duties violation", str(ctx.exception))

    def test_single_approval_resolves_request(self):
        """Approving a 1-approval request sets status to APPROVED and records action."""
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='FREEZE_EXTENSION',
            entity_type='MembershipFreeze',
            entity_id=uuid.uuid4(),
            requested_by_user=self.requester,
            required_approvals=1
        )

        action_record = AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.approver1,
            action='APPROVED',
            comment='Verified medical documentation'
        )

        self.assertEqual(action_record.action, 'APPROVED')
        req.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')
        self.assertIsNotNone(req.resolved_at)

    def test_rejection_immediately_rejects_request(self):
        """Rejecting an approval request immediately sets status to REJECTED."""
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='CUSTOM_CREDIT',
            entity_type='RewardAccount',
            entity_id=uuid.uuid4(),
            requested_by_user=self.requester,
            required_approvals=2
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.approver1,
            action='REJECTED',
            comment='Policy does not allow discretionary wallet credits'
        )

        req.refresh_from_db()
        self.assertEqual(req.status, 'REJECTED')
        self.assertIsNotNone(req.resolved_at)

    def test_multi_approver_quorum(self):
        """Requests requiring multiple approvals remain PENDING until all required approvals are submitted."""
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='CONTRACT_TERMINATION',
            entity_type='Membership',
            entity_id=uuid.uuid4(),
            requested_by_user=self.requester,
            required_approvals=2
        )

        # 1st approval: Should remain PENDING
        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.approver1,
            action='APPROVED',
            comment='Manager sign-off'
        )
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertIsNone(req.resolved_at)

        # 2nd approval by same user should be prohibited
        with self.assertRaises(ValidationError):
            AdminApprovalService.process_action(
                approval_request=req,
                approver_user=self.approver1,
                action='APPROVED',
                comment='Duplicate sign-off attempt'
            )

        # 2nd approval by distinct director user resolves to APPROVED
        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.approver2,
            action='APPROVED',
            comment='Director sign-off'
        )
        req.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')
        self.assertIsNotNone(req.resolved_at)

    def test_cannot_act_on_resolved_request(self):
        """Submitting actions on already resolved requests is prohibited."""
        req = AdminApprovalService.create_approval_request(
            organization=self.org,
            request_type='WAIVER',
            entity_type='User',
            entity_id=uuid.uuid4(),
            requested_by_user=self.requester,
            required_approvals=1
        )

        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=self.approver1,
            action='APPROVED'
        )

        with self.assertRaises(ValidationError) as ctx:
            AdminApprovalService.process_action(
                approval_request=req,
                approver_user=self.approver2,
                action='REJECTED'
            )
        self.assertIn("Cannot act on approval request", str(ctx.exception))
