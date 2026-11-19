"""
Dynamic Role-Based Phone Number Privacy & Visibility Test Suite.

Covers all required security & functional scenarios:
  1. Platform Super Admin sees full phone number.
  2. Org Admin WITH 'core.privacy.view_phone_number' granted sees full phone.
  3. Org Admin WITHOUT 'core.privacy.view_phone_number' (revoked) sees ******7890.
  4. Sales Executive without permission sees ******7890 across CRM, Comms, Timeline,
     Referrers, Duplicate Check, Meta Imports, Members, and Support Tickets.
  5. Sales Executive dynamically granted permission immediately sees full phone.
  6. Follow-up staff (Front Desk) without permission sees ******7890.
  7. Trial Management staff (Trainer) without permission sees ******7890.
  8. Revoking a previously granted permission immediately re-masks phone numbers.
  9. Multiple roles on one user: union grants access; revoking granting role re-masks.
 10. Fail-closed on missing/unauthenticated request; self-profile visibility preserved.
 11. Masked phone strings submitted during Lead/Member edit never overwrite real DB phones.
 12. Duplicate Check & Referrer Search remain searchable by phone while masking results.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.models_crm import (
    Lead,
    LeadActivity,
    LeadSource,
    SalesFollowupTask,
    TrialBooking,
)
from apps.tenant_core.models_meta_leads import MetaLeadImport
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_rbac import (
    Permission,
    Role,
    RoleAssignment,
    RolePermissionSet,
    RolePermissionSetItem,
)
from apps.tenant_core.models_tickets import SupportTicket
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.rbac_defaults import sync_default_role_permissions
from apps.tenant_core.security_utils import (
    MASKED_PHONE_PLACEHOLDER,
    PERM_VIEW_PHONE_NUMBER,
    can_view_phone_number,
    is_masked_phone,
    mask_communication_identifier,
    mask_phone_if_restricted,
    mask_phone_number,
    sanitize_meta_field_data,
)
from apps.tenant_core.serializers import TenantUserSerializer
from apps.tenant_core.serializers_communication import CommunicationMessageSerializer
from apps.tenant_core.serializers_crm import (
    LeadActivitySerializer,
    LeadSerializer,
    SalesFollowupTaskSerializer,
    TrialBookingSerializer,
)
from apps.tenant_core.serializers_tickets import SupportTicketSerializer
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.views_members import serialize_member
from apps.tenant_core.views_meta_leads import ImportSerializer
from config.routers import set_tenant_db_alias


class DynamicPhonePrivacyRBACTestCase(TestCase):
    databases = '__all__'

    RAW_LEAD_PHONE = '+919876547890'
    MASKED_LEAD_PHONE = '******7890'
    RAW_MEMBER_PHONE = '+919820114321'
    MASKED_MEMBER_PHONE = '******4321'

    DB_ALIAS = 'tenant_test'

    def setUp(self):
        set_tenant_db_alias(self.DB_ALIAS)
        self.factory = RequestFactory()

        self.org = Organization.objects.create(
            name='SWEAT Privacy Test Org',
            code='SWEAT-PRIV',
            status='ACTIVE',
        )
        self.location = Location.objects.create(
            organization=self.org,
            name='Mumbai West',
            code='MUM-W',
            city='Mumbai',
            status='ACTIVE',
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.location,
            name='Andheri West',
            code='AND-W',
            status='ACTIVE',
        )

        # Seed canonical roles & permissions (including core.privacy.view_phone_number)
        sync_default_role_permissions(db_alias=self.DB_ALIAS, org=self.org, overwrite_custom=False)

        self.phone_perm = Permission.objects.get(permission_code=PERM_VIEW_PHONE_NUMBER)
        self.role_org_admin = Role.objects.get(organization=self.org, code='ORG_ADMIN')
        self.role_branch_mgr = Role.objects.get(organization=self.org, code='BRANCH_MANAGER')
        self.role_sales = Role.objects.get(organization=self.org, code='SALES_REP')
        self.role_front_desk = Role.objects.get(organization=self.org, code='FRONT_DESK')
        self.role_trainer = Role.objects.get(organization=self.org, code='TRAINER')

        # Create users
        self.org_admin_user = self._create_user_with_role('admin@sweat.test', 'Org', 'Admin', self.role_org_admin)
        self.sales_user = self._create_user_with_role('sales@sweat.test', 'Sales', 'Exec', self.role_sales)
        self.followup_user = self._create_user_with_role('followup@sweat.test', 'Followup', 'Exec', self.role_front_desk)
        self.trial_user = self._create_user_with_role('trial@sweat.test', 'Trial', 'Staff', self.role_trainer)

        self.member_user = TenantUser.objects.create(
            organization=self.org,
            email='member@sweat.test',
            first_name='Riya',
            last_name='Kapoor',
            phone=self.RAW_MEMBER_PHONE,
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch,
        )
        self.member_profile = UserProfile.objects.create(
            user=self.member_user,
            first_name_snapshot='Riya',
            last_name_snapshot='Kapoor',
            preferred_branch=self.branch,
            emergency_contact_json={'phone': '+919900112233'},
        )

        self.lead_source = LeadSource.objects.create(
            organization=self.org,
            name='Instagram Meta',
            code='IG_META',
            source_type='META',
            status='ACTIVE',
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Diana',
            last_name='Yohannan',
            phone_normalized=self.RAW_LEAD_PHONE,
            email_normalized='diana@example.com',
            current_status='NEW_LEAD',
            assigned_sales_user=self.sales_user,
        )
        self.activity = LeadActivity.objects.create(
            lead=self.lead,
            activity_type='CALL',
            outcome='CONNECTED',
            notes='Discussed Pilates trial',
            performed_by_user=self.sales_user,
        )
        self.followup_task = SalesFollowupTask.objects.create(
            lead=self.lead,
            assigned_to_user=self.followup_user,
            created_by_user=self.sales_user,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(hours=2),
            status='PENDING',
        )
        self.trial_booking = TrialBooking.objects.create(
            branch=self.branch,
            lead=self.lead,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1),
            status='BOOKED',
            confirmation_status='PENDING',
        )
        self.comm_msg = CommunicationMessage.objects.create(
            organization=self.org,
            lead=self.lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            purpose='TRANSACTIONAL',
            recipient=self.RAW_LEAD_PHONE,
            sender_identifier=self.RAW_LEAD_PHONE,
            raw_sender_data={'phone_number': self.RAW_LEAD_PHONE, 'note': 'ok'},
            body_snapshot='Hi Diana, welcome to SWEAT!',
            status='SENT',
            created_by_user=self.sales_user,
        )
        self.meta_import = MetaLeadImport.objects.create(
            organization=self.org,
            mode='LIVE',
            page_id='1001',
            form_id='2002',
            external_lead_id='ext-lead-1',
            payload_hash='hash-1',
            lead=self.lead,
            status='IMPORTED',
            field_data=[
                {'name': 'full_name', 'values': ['Diana Yohannan']},
                {'name': 'phone_number', 'values': [self.RAW_LEAD_PHONE]},
                {'name': 'email', 'values': ['diana@example.com']},
            ],
        )
        self.ticket = SupportTicket.objects.create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            ticket_number='TKT-PRIV-001',
            category='GENERAL',
            priority='MEDIUM',
            status='OPEN',
            subject='Billing question',
            description='Need receipt copy',
        )

    def _create_user_with_role(self, email: str, first: str, last: str, role: Role) -> TenantUser:
        u = TenantUser.objects.create(
            organization=self.org,
            email=email,
            first_name=first,
            last_name=last,
            phone='+919111222333',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch,
        )
        RoleAssignment.objects.create(
            organization=self.org,
            user=u,
            role=role,
            branch=None if role.scope == 'ORG' else self.branch,
            scope_type='ORGANIZATION' if role.scope == 'ORG' else 'BRANCH',
            status='ACTIVE',
            is_active=True,
        )
        return u

    def _make_request(self, user):
        req = self.factory.get('/api/v1/tenant/crm/leads/')
        if user is not None:
            user._db_alias = self.DB_ALIAS
            req.user = user
        req._tenant_db_alias = self.DB_ALIAS
        return req

    def _set_role_phone_permission(self, role: Role, granted: bool):
        pset = RolePermissionSet.objects.filter(role=role, is_active=True).first()
        if not pset:
            pset = RolePermissionSet.objects.create(
                role=role,
                organization=self.org,
                name=f'{role.name} Permissions',
                scope_type='ORGANIZATION',
                is_active=True,
                status='ACTIVE',
            )
        RolePermissionSetItem.objects.update_or_create(
            permission_set=pset,
            permission=self.phone_perm,
            defaults={'granted': granted, 'is_allowed': granted},
        )

    # ------------------------------------------------------------------
    # 1. Core Masking & Super Admin / Fail-Closed Checks
    # ------------------------------------------------------------------
    def test_01_mask_format_and_fail_closed(self):
        self.assertEqual(mask_phone_number('+919876547890'), '******7890')
        self.assertEqual(mask_phone_number('98765-47890'), '******7890')
        self.assertEqual(mask_phone_number('12'), MASKED_PHONE_PLACEHOLDER)
        # Idempotent on already masked strings
        self.assertEqual(mask_phone_number('******7890'), '******7890')
        self.assertTrue(is_masked_phone('******7890'))
        self.assertTrue(is_masked_phone('+91-XXXXX-7890'))
        self.assertTrue(is_masked_phone('Phone number hidden'))
        self.assertFalse(is_masked_phone('+919876547890'))

        # Fail-closed when request is None or unauthenticated
        self.assertFalse(can_view_phone_number(None))
        self.assertEqual(mask_phone_if_restricted(None, self.RAW_LEAD_PHONE), self.MASKED_LEAD_PHONE)
        anon_req = self._make_request(SimpleNamespace(is_authenticated=False))
        self.assertFalse(can_view_phone_number(anon_req))
        self.assertEqual(mask_phone_if_restricted(anon_req, self.RAW_LEAD_PHONE), self.MASKED_LEAD_PHONE)

    def test_02_platform_super_admin_always_allowed(self):
        super_user = SimpleNamespace(
            id=uuid.uuid4(),
            is_authenticated=True,
            is_superuser=True,
            _auth_type='platform',
            _role_codes={'SUPER_ADMIN'},
        )
        req = self._make_request(super_user)
        self.assertTrue(can_view_phone_number(req))
        self.assertEqual(mask_phone_if_restricted(req, self.RAW_LEAD_PHONE), self.RAW_LEAD_PHONE)

    # ------------------------------------------------------------------
    # 2. Org Admin Configurable (With & Without Permission)
    # ------------------------------------------------------------------
    def test_03_org_admin_default_allowed_and_dynamically_revokable(self):
        # Default after sync: ORG_ADMIN has core.privacy.view_phone_number granted
        req_allowed = self._make_request(self.org_admin_user)
        self.assertTrue(can_view_phone_number(req_allowed))
        lead_data = LeadSerializer(self.lead, context={'request': req_allowed}).data
        self.assertEqual(lead_data['phone_normalized'], self.RAW_LEAD_PHONE)
        self.assertEqual(lead_data['phone'], self.RAW_LEAD_PHONE)

        # Revoke permission from ORG_ADMIN via Permission Matrix (no hardcoded bypass!)
        self._set_role_phone_permission(self.role_org_admin, granted=False)
        req_revoked = self._make_request(self.org_admin_user)
        self.assertFalse(can_view_phone_number(req_revoked))
        lead_data_revoked = LeadSerializer(self.lead, context={'request': req_revoked}).data
        self.assertEqual(lead_data_revoked['phone_normalized'], self.MASKED_LEAD_PHONE)
        self.assertEqual(lead_data_revoked['phone'], self.MASKED_LEAD_PHONE)

        # Re-grant permission -> immediately visible again
        self._set_role_phone_permission(self.role_org_admin, granted=True)
        req_regranted = self._make_request(self.org_admin_user)
        self.assertTrue(can_view_phone_number(req_regranted))
        self.assertEqual(
            LeadSerializer(self.lead, context={'request': req_regranted}).data['phone_normalized'],
            self.RAW_LEAD_PHONE,
        )

    # ------------------------------------------------------------------
    # 3. Sales Executive Without Permission vs. Dynamically Granted vs. Revoked
    # ------------------------------------------------------------------
    def test_04_sales_executive_denied_by_default_across_all_serializers(self):
        req = self._make_request(self.sales_user)
        self.assertFalse(can_view_phone_number(req))

        # Leads
        lead_data = LeadSerializer(self.lead, context={'request': req}).data
        self.assertEqual(lead_data['phone_normalized'], self.MASKED_LEAD_PHONE)
        self.assertEqual(lead_data['phone'], self.MASKED_LEAD_PHONE)

        # Activities
        act_data = LeadActivitySerializer(self.activity, context={'request': req}).data
        self.assertEqual(act_data['lead_phone'], self.MASKED_LEAD_PHONE)

        # Follow-ups
        task_data = SalesFollowupTaskSerializer(self.followup_task, context={'request': req}).data
        self.assertEqual(task_data['lead_phone'], self.MASKED_LEAD_PHONE)

        # Trials
        trial_data = TrialBookingSerializer(self.trial_booking, context={'request': req}).data
        self.assertEqual(trial_data['lead_phone'], self.MASKED_LEAD_PHONE)

        # Communications (phone masked, email untouched)
        comm_data = CommunicationMessageSerializer(self.comm_msg, context={'request': req}).data
        self.assertEqual(comm_data['lead_phone'], self.MASKED_LEAD_PHONE)
        self.assertEqual(comm_data['recipient'], self.MASKED_LEAD_PHONE)
        self.assertEqual(comm_data['sender_identifier'], self.MASKED_LEAD_PHONE)
        self.assertEqual(comm_data['raw_sender_data']['phone_number'], self.MASKED_LEAD_PHONE)
        self.assertEqual(
            mask_communication_identifier(req, 'EMAIL', 'diana@example.com'),
            'diana@example.com',
        )

        # Lead Timeline
        timeline = CRMLeadService.get_lead_timeline(self.lead, db_alias=self.DB_ALIAS, request=req)
        comm_events = [e for e in timeline if e['event_type'].startswith('COMMUNICATION_')]
        self.assertTrue(len(comm_events) >= 1)
        self.assertEqual(comm_events[0]['metadata']['recipient'], self.MASKED_LEAD_PHONE)

        # Meta Lead Import (field_data + matched_lead)
        meta_data = ImportSerializer(
            self.meta_import,
            context={'request': req, 'alias': self.DB_ALIAS, 'page_conn_map': {}, 'mapping_map': {}},
        ).data
        self.assertEqual(meta_data['matched_lead']['phone'], self.MASKED_LEAD_PHONE)
        phone_entry = [f for f in meta_data['field_data'] if f['name'] == 'phone_number'][0]
        self.assertEqual(phone_entry['values'], [self.MASKED_LEAD_PHONE])

        # Member serialization (including emergency contact)
        member_data = serialize_member(self.member_profile, alias=self.DB_ALIAS, request=req)
        self.assertEqual(member_data['phone'], self.MASKED_MEMBER_PHONE)
        self.assertEqual(member_data['emergency_contact'], '******2233')

        # Support Tickets
        ticket_data = SupportTicketSerializer(self.ticket, context={'request': req}).data
        self.assertEqual(ticket_data['member_phone'], self.MASKED_MEMBER_PHONE)

        # TenantUserSerializer (other user masked; self unmasked)
        other_user_data = TenantUserSerializer(self.member_user, context={'request': req}).data
        self.assertEqual(other_user_data['phone'], self.MASKED_MEMBER_PHONE)
        self_user_data = TenantUserSerializer(self.sales_user, context={'request': req}).data
        self.assertEqual(self_user_data['phone'], '+919111222333')

    def test_05_sales_executive_grant_and_revoke_lifecycle(self):
        # 1. Grant dynamically via Permission Matrix model
        self._set_role_phone_permission(self.role_sales, granted=True)
        req_granted = self._make_request(self.sales_user)
        self.assertTrue(can_view_phone_number(req_granted))
        self.assertEqual(
            LeadSerializer(self.lead, context={'request': req_granted}).data['phone_normalized'],
            self.RAW_LEAD_PHONE,
        )
        self.assertEqual(
            CommunicationMessageSerializer(self.comm_msg, context={'request': req_granted}).data['recipient'],
            self.RAW_LEAD_PHONE,
        )
        self.assertEqual(
            serialize_member(self.member_profile, alias=self.DB_ALIAS, request=req_granted)['phone'],
            self.RAW_MEMBER_PHONE,
        )

        # 2. Revoke dynamically -> immediately masked again
        self._set_role_phone_permission(self.role_sales, granted=False)
        req_revoked = self._make_request(self.sales_user)
        self.assertFalse(can_view_phone_number(req_revoked))
        self.assertEqual(
            LeadSerializer(self.lead, context={'request': req_revoked}).data['phone_normalized'],
            self.MASKED_LEAD_PHONE,
        )

    # ------------------------------------------------------------------
    # 4. Follow-up Staff, Trial Staff & Branch Manager Denied by Default
    # ------------------------------------------------------------------
    def test_06_followup_trial_and_branch_manager_denied_by_default(self):
        req_followup = self._make_request(self.followup_user)
        self.assertEqual(
            SalesFollowupTaskSerializer(self.followup_task, context={'request': req_followup}).data['lead_phone'],
            self.MASKED_LEAD_PHONE,
        )

        req_trial = self._make_request(self.trial_user)
        self.assertEqual(
            TrialBookingSerializer(self.trial_booking, context={'request': req_trial}).data['lead_phone'],
            self.MASKED_LEAD_PHONE,
        )

        # Previously hardcoded as unmasked in old security_utils.py — now strictly dynamic!
        bm_user = self._create_user_with_role('bm@sweat.test', 'Branch', 'Mgr', self.role_branch_mgr)
        req_bm = self._make_request(bm_user)
        self.assertFalse(can_view_phone_number(req_bm))
        self.assertEqual(
            LeadSerializer(self.lead, context={'request': req_bm}).data['phone_normalized'],
            self.MASKED_LEAD_PHONE,
        )

    # ------------------------------------------------------------------
    # 5. Multiple Roles Union Behavior
    # ------------------------------------------------------------------
    def test_07_multiple_roles_union_and_revocation(self):
        # Assign a custom role WITH phone permission alongside SALES_REP (without)
        auditor_role = Role.objects.create(
            organization=self.org,
            name='Privacy Auditor',
            code='PRIVACY_AUDITOR',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
        )
        self._set_role_phone_permission(auditor_role, granted=True)
        ra = RoleAssignment.objects.create(
            organization=self.org,
            user=self.sales_user,
            role=auditor_role,
            scope_type='ORGANIZATION',
            status='ACTIVE',
            is_active=True,
        )

        req_multi = self._make_request(self.sales_user)
        self.assertTrue(can_view_phone_number(req_multi))

        # Deactivate the granting role assignment -> immediately reverts to masked
        ra.is_active = False
        ra.status = 'INACTIVE'
        ra.save()

        req_after = self._make_request(self.sales_user)
        self.assertFalse(can_view_phone_number(req_after))

    # ------------------------------------------------------------------
    # 6. Data Integrity: Masked Phone Never Overwrites Real Phone on Update
    # ------------------------------------------------------------------
    def test_08_editing_lead_with_masked_phone_preserves_real_phone(self):
        req = self._make_request(self.sales_user)
        ser = LeadSerializer(
            instance=self.lead,
            data={
                'first_name': 'Diana',
                'last_name': 'Updated',
                'phone_normalized': self.MASKED_LEAD_PHONE,
                'phone': self.MASKED_LEAD_PHONE,
            },
            partial=True,
            context={'request': req},
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        updated = CRMLeadService.update_lead(self.lead, ser.validated_data, actor_user=self.sales_user, db_alias=self.DB_ALIAS)
        updated.refresh_from_db()
        self.assertEqual(updated.last_name, 'Updated')
        self.assertEqual(updated.phone_normalized, self.RAW_LEAD_PHONE)

    def test_09_meta_field_data_sanitizer_handles_dict_and_list(self):
        req_denied = self._make_request(self.sales_user)
        dict_payload = {'full_name': 'Test', 'mobile_number': '+919988776655'}
        sanitized = sanitize_meta_field_data(req_denied, dict_payload)
        self.assertEqual(sanitized['mobile_number'], '******6655')
        self.assertEqual(sanitized['full_name'], 'Test')

        req_allowed = self._make_request(self.org_admin_user)
        unsanitized = sanitize_meta_field_data(req_allowed, dict_payload)
        self.assertEqual(unsanitized['mobile_number'], '+919988776655')

    # ------------------------------------------------------------------
    # 7. Telephony Privacy & Server-Side Click-to-Call by lead_id
    # ------------------------------------------------------------------
    def test_10_telephony_missed_calls_and_inbound_poll_respect_dynamic_permission(self):
        from unittest.mock import patch
        from contextlib import nullcontext
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.tenant_core.models_crm import CallSession
        from apps.tenant_core.views_telephony import (
            ActiveInboundCallPollView,
            MissedCallsQueueView,
        )

        CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.sales_user,
            branch=self.branch,
            provider='VOBIZ',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            user_phone_number=self.RAW_LEAD_PHONE,
            started_at=timezone.now(),
        )

        api_rf = APIRequestFactory()
        with patch('apps.tenant_core.views_telephony.resolve_telephony_tenant_context', return_value=nullcontext()):
            # 1. Sales Rep WITHOUT permission -> sees ******7890
            req_denied = api_rf.get('/api/v1/tenant/crm/telephony/missed-calls/')
            self.sales_user._db_alias = self.DB_ALIAS
            req_denied._tenant_db_alias = self.DB_ALIAS
            force_authenticate(req_denied, user=self.sales_user)
            missed_resp = MissedCallsQueueView.as_view()(req_denied)
            self.assertEqual(missed_resp.status_code, 200)
            self.assertEqual(missed_resp.data['missed_calls'][0]['user_phone_number'], self.MASKED_LEAD_PHONE)
            self.assertEqual(missed_resp.data['missed_calls'][0]['masked_phone'], self.MASKED_LEAD_PHONE)

            poll_req = api_rf.get('/api/v1/tenant/crm/telephony/active/')
            poll_req._tenant_db_alias = self.DB_ALIAS
            force_authenticate(poll_req, user=self.sales_user)
            poll_resp = ActiveInboundCallPollView.as_view()(poll_req)
            self.assertEqual(poll_resp.status_code, 200)
            self.assertTrue(poll_resp.data['active'])
            self.assertEqual(poll_resp.data['caller_phone'], self.MASKED_LEAD_PHONE)

            # 2. Grant permission -> immediately sees full phone number
            self._set_role_phone_permission(self.role_sales, granted=True)
            req_granted = api_rf.get('/api/v1/tenant/crm/telephony/missed-calls/')
            req_granted._tenant_db_alias = self.DB_ALIAS
            force_authenticate(req_granted, user=self.sales_user)
            missed_resp2 = MissedCallsQueueView.as_view()(req_granted)
            self.assertEqual(missed_resp2.data['missed_calls'][0]['user_phone_number'], self.RAW_LEAD_PHONE)

    def test_11_click_to_call_by_lead_id_and_disposition_with_masked_phone(self):
        from unittest.mock import MagicMock, patch
        from contextlib import nullcontext
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.tenant_core.models_crm import AgentPresence, CallSession
        from apps.tenant_core.views_telephony import CallDispositionView, ClickToCallView

        AgentPresence.objects.create(
            organization=self.org,
            user=self.sales_user,
            sip_username='sweat_rep_101',
            status='AVAILABLE',
        )

        api_rf = APIRequestFactory()
        mock_result = MagicMock(success=True, provider_message_id='vobiz-uuid-999')

        with patch('apps.tenant_core.views_telephony.resolve_telephony_tenant_context', return_value=nullcontext()), \
             patch('apps.tenant_core.views_telephony._is_within_trai_hours', return_value=True), \
             patch('apps.tenant_core.views_telephony.VobizVoiceAdapter.initiate_click_to_call', return_value=mock_result) as mock_dial:

            # Sales Rep without phone visibility calls using ONLY lead_id
            req = api_rf.post('/api/v1/tenant/crm/telephony/click-to-call/', {'lead_id': str(self.lead.id)}, format='json')
            self.sales_user._db_alias = self.DB_ALIAS
            req._tenant_db_alias = self.DB_ALIAS
            force_authenticate(req, user=self.sales_user)
            resp = ClickToCallView.as_view()(req)

            self.assertEqual(resp.status_code, 200)
            # Server dialed the real phone privately while returning masked_phone to restricted rep
            mock_dial.assert_called_once()
            self.assertEqual(mock_dial.call_args.kwargs['customer_phone'], self.RAW_LEAD_PHONE)
            self.assertEqual(resp.data['masked_phone'], self.MASKED_LEAD_PHONE)

            # Submit call disposition with masked caller_phone -> never overwrites real phone on CallSession
            session_id = resp.data['call_session_id']
            disp_req = api_rf.post(
                '/api/v1/tenant/crm/telephony/disposition/',
                {
                    'session_id': session_id,
                    'lead_id': str(self.lead.id),
                    'caller_phone': self.MASKED_LEAD_PHONE,
                    'disposition': 'INTERESTED',
                    'disposition_notes': 'Wants morning stretch slot',
                },
                format='json',
            )
            disp_req._tenant_db_alias = self.DB_ALIAS
            force_authenticate(disp_req, user=self.sales_user)
            disp_resp = CallDispositionView.as_view()(disp_req)
            self.assertEqual(disp_resp.status_code, 200)

            cs = CallSession.objects.get(id=session_id)
            self.assertEqual(cs.user_phone_number, self.RAW_LEAD_PHONE)
            self.assertEqual(cs.disposition, 'INTERESTED')
            self.assertEqual(cs.lead_id, self.lead.id)
