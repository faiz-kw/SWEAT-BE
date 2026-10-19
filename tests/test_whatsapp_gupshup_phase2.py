"""
backend/tests/test_whatsapp_gupshup_phase2.py — Gupshup WhatsApp Integration Phase 2 Test Suite.
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_crm import LeadSource, Lead, TrialBooking, SalesFollowupTask
from apps.tenant_core.models_govern import WhatsAppAutomationPolicy, InAppNotification
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.models_infra import Integration
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
from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
from config.routers import set_tenant_db_alias
from config.secrets import SecretResolver


class GupshupWhatsAppPhase2Tests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-GS-T2',
            name='SWEAT Elite Gym 2',
            slug='sweat-elite-gym-2',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            code='GS-PLAN-2',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='ORG-GS-2',
            name='SWEAT Elite Core',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-GS-2',
            name='Flagship Gym',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-GS-2',
            name='Main Floor',
            status='ACTIVE',
        )

        # Staff Admin User & RBAC
        self.staff_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sweat.fit',
            username='sweat_admin',
            first_name='Admin',
            last_name='User',
            phone='+919876500001',
            status='ACTIVE',
            is_login_allowed=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.staff_user,
            branch=self.branch,
            is_primary=True,
            is_active=True,
        )
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='ORG_ADMIN',
            name='Org Admin',
            is_system=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.staff_user,
            role=self.role_admin,
            scope_type='ORG',
        )
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            code='crm', defaults={'name': 'CRM'}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, code='leads', defaults={'name': 'Leads'}
        )
        self.perm_set_admin, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.role_admin, name='Admin Permissions'
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.mod_crm, defaults={'can_access': True, 'permission_set': self.perm_set_admin}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.submod_leads, defaults={'can_access': True, 'permission_set': self.perm_set_admin}
        )
        for p_code in [
            'crm.leads.view', 'crm.leads.edit', 'crm.leads.create',
            'crm.communications.view', 'crm.communications.send',
            'crm.settings.view', 'crm.settings.edit',
        ]:
            perm, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=p_code,
                defaults={
                    'module': self.mod_crm,
                    'submodule': self.submod_leads,
                    'code': p_code,
                    'label': p_code,
                    'action': p_code.split('.')[-1],
                }
            )
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=self.perm_set_admin, permission=perm
            )

        # Auth Token
        self.token_admin = str(_build_tenant_token(self.staff_user, self.tenant, 'tenant_test').access_token)
        self.auth_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {self.token_admin}',
            'HTTP_X_TENANT_ID': str(self.tenant.id),
        }

        # Lead Source & Lead
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            code='GS_SOURCE_2',
            name='Web Inbound',
            source_type='DIGITAL_ORGANIC',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Karan',
            last_name='Mehta',
            phone_normalized='+919876543210',
            email_normalized='karan.mehta@example.com',
            current_status='NEW_LEAD',
            consent_whatsapp=True,
            assigned_sales_user=self.staff_user,
        )

        # Class Structure
        self.cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            name='Reformer Pilates',
            code='PILATES',
        )
        self.tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            category=self.cat,
            name='Power Pilates Flow',
            code='PILATES-FLOW',
            default_duration_minutes=50,
            default_capacity=15,
            default_trial_capacity=3,
        )
        self.now = timezone.now()
        self.occurrence = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.tmpl,
            branch=self.branch,
            start_at=self.now + timedelta(hours=2),
            end_at=self.now + timedelta(hours=2, minutes=50),
            capacity=15,
            trial_capacity=3,
            status='SCHEDULED',
        )

        # Member User & Profile
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member.rohit@example.com',
            username='rohit_member',
            first_name='Rohit',
            last_name='Sharma',
            phone='+919876511223',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            first_name_snapshot='Rohit',
            last_name_snapshot='Sharma',
            member_status='ACTIVE',
        )

        # Secret Reference setup for Gupshup
        self.secret_ref = 'env://TEST_GUPSHUP_SECRET_2'
        SecretResolver.register_test_secret(self.secret_ref, json.dumps({
            'api_key': 'gs_live_test_api_key_secret_p2',
            'webhook_secret': 'gs_webhook_token_secure_p2',
        }))

        # Gupshup Integration record
        self.integration = Integration.objects.using('tenant_test').create(
            integration_type='WHATSAPP',
            provider='GUPSHUP',
            status='ACTIVE',
            secret_reference=self.secret_ref,
            configuration={
                'app_name': 'SweatEliteWhatsApp',
                'source_number': '919876500000',
                'webhook_secret': 'gs_webhook_token_secure_p2',
            },
        )

    def tearDown(self):
        SecretResolver.clear_test_secrets()
        super().tearDown()

    # ── Test 1: Policy Retrieval and UI Configuration Update ──────────────────

    def test_01_whatsapp_policy_api_get_and_update(self):
        url = '/api/v1/tenant/crm/whatsapp-policy/'
        res = self.client.get(url, **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('booking_confirmation_template_name', res.data)
        self.assertEqual(res.data['booking_reminder_offsets_minutes'], [120, 30])
        self.assertEqual(res.data['whatsapp_enabled'], False)

        update_payload = {
            'whatsapp_enabled': True,
            'booking_confirmation_enabled': True,
            'booking_confirmation_template_name': 'sweat_custom_class_confirm',
            'booking_reminder_offsets_minutes': [60, 15],
            'inbound_confirm_keyword': 'YES',
        }
        res_patch = self.client.patch(url, data=update_payload, format='json', **self.auth_headers)
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertTrue(res_patch.data['whatsapp_enabled'])
        self.assertEqual(res_patch.data['booking_confirmation_template_name'], 'sweat_custom_class_confirm')
        self.assertEqual(res_patch.data['booking_reminder_offsets_minutes'], [60, 15])
        self.assertEqual(res_patch.data['inbound_confirm_keyword'], 'YES')

    # ── Test 2: Outbound Class Booking Confirmation Dispatch ──────────────────

    @patch('requests.post')
    def test_02_class_booking_confirmation_outbound_dispatch(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gs-msg-confirm-001',
        }
        mock_post.return_value = mock_resp

        policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        policy.whatsapp_enabled = True
        policy.booking_confirmation_enabled = True
        policy.save(using='tenant_test')

        booking = Booking.objects.using('tenant_test').create(
            booking_number=f"BK-{uuid.uuid4().hex[:8].upper()}",
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            branch=self.branch,
            booking_type='MEMBER',
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        msg = WhatsAppAutomationService.send_class_booking_confirmation(booking, db_alias='tenant_test')
        self.assertIsNotNone(msg)
        self.assertEqual(msg.channel, 'WHATSAPP')
        self.assertEqual(msg.provider, 'GUPSHUP')
        self.assertEqual(msg.status, 'SUBMITTED')
        self.assertEqual(msg.provider_message_id, 'gs-msg-confirm-001')
        self.assertEqual(msg.trigger_type, 'CLASS_CONFIRMATION')

        self.assertTrue(mock_post.called)
        call_kwargs = mock_post.call_args[1]
        data = call_kwargs.get('data', {})
        self.assertEqual(data.get('source'), '919876500000')
        self.assertEqual(data.get('destination'), '919876511223')

        mock_post.reset_mock()
        msg_duplicate = WhatsAppAutomationService.send_class_booking_confirmation(booking, db_alias='tenant_test')
        self.assertEqual(msg_duplicate.id, msg.id)
        mock_post.assert_not_called()

    # ── Test 3: Outbound Trial Booking Confirmation Dispatch ──────────────────

    @patch('requests.post')
    def test_03_trial_booking_confirmation_outbound_dispatch(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gs-msg-trial-001',
        }
        mock_post.return_value = mock_resp

        policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        policy.whatsapp_enabled = True
        policy.trial_confirmation_enabled = True
        policy.save(using='tenant_test')

        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch,
            scheduled_start=self.now + timedelta(days=1),
            scheduled_end=self.now + timedelta(days=1, minutes=50),
            status='BOOKED',
            confirmation_status='PENDING',
        )

        msg = WhatsAppAutomationService.send_trial_booking_confirmation(trial, db_alias='tenant_test')
        self.assertIsNotNone(msg)
        self.assertEqual(msg.recipient, '+919876543210')
        self.assertEqual(msg.trigger_type, 'TRIAL_CONFIRMATION')
        self.assertEqual(msg.related_trial, trial)
        self.assertEqual(msg.provider_message_id, 'gs-msg-trial-001')

    # ── Test 4: Scheduled Reminders Execution (2h before) ─────────────────────

    @patch('requests.post')
    def test_04_scheduled_booking_reminders(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gs-msg-remind-001',
        }
        mock_post.return_value = mock_resp

        policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        policy.whatsapp_enabled = True
        policy.booking_reminder_enabled = True
        policy.booking_reminder_offsets_minutes = [120]
        policy.save(using='tenant_test')

        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.tmpl,
            branch=self.branch,
            start_at=self.now + timedelta(minutes=115),
            end_at=self.now + timedelta(minutes=165),
            capacity=10,
            status='SCHEDULED',
        )
        booking = Booking.objects.using('tenant_test').create(
            booking_number=f"BK-REMIND-{uuid.uuid4().hex[:6]}",
            user_profile=self.member_profile,
            occurrence=occ,
            branch=self.branch,
            booking_type='MEMBER',
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        dispatched = WhatsAppAutomationService.process_scheduled_reminders(db_alias='tenant_test')
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].trigger_type, 'CLASS_REMINDER')

        mock_post.reset_mock()
        dispatched_second = WhatsAppAutomationService.process_scheduled_reminders(db_alias='tenant_test')
        self.assertEqual(len(dispatched_second), 0)
        mock_post.assert_not_called()

    # ── Test 5: Inbound CONFIRM Keyword Handling ──────────────────────────────

    def test_05_inbound_confirm_keyword_confirms_trial_and_notifies_staff(self):
        policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        policy.whatsapp_enabled = True
        policy.inbound_confirm_keyword = 'CONFIRM'
        policy.confirm_action_notify_staff = True
        policy.save(using='tenant_test')

        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch,
            scheduled_start=self.now + timedelta(days=1),
            scheduled_end=self.now + timedelta(days=1, minutes=50),
            status='BOOKED',
            confirmation_status='PENDING',
        )

        inbound_msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            lead=self.lead,
            related_trial=trial,
            channel='WHATSAPP',
            direction='INBOUND',
            sender_identifier=self.lead.phone_normalized,
            recipient='919876500000',
            body_snapshot='CONFIRM',
            status='RECEIVED',
        )

        result = WhatsAppAutomationService.handle_inbound_keyword_action(
            inbound_message=inbound_msg,
            body_text='CONFIRM',
            db_alias='tenant_test',
        )

        self.assertEqual(result.get('action'), 'CONFIRMED')

        trial.refresh_from_db()
        self.assertEqual(trial.confirmation_status, 'CONFIRMED')

        notif = InAppNotification.objects.using('tenant_test').filter(
            organization=self.org,
            user=self.staff_user,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn('Confirmed', notif.title)

    # ── Test 6: Inbound RESCHEDULE Keyword Handling ───────────────────────────

    def test_06_inbound_reschedule_keyword_creates_followup_task(self):
        policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        policy.whatsapp_enabled = True
        policy.inbound_reschedule_keyword = 'RESCHEDULE'
        policy.reschedule_action_create_task = True
        policy.save(using='tenant_test')

        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch,
            scheduled_start=self.now + timedelta(days=1),
            scheduled_end=self.now + timedelta(days=1, minutes=50),
            status='BOOKED',
            confirmation_status='PENDING',
        )

        inbound_msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            lead=self.lead,
            related_trial=trial,
            channel='WHATSAPP',
            direction='INBOUND',
            sender_identifier=self.lead.phone_normalized,
            recipient='919876500000',
            body_snapshot='I need to RESCHEDULE my trial',
            status='RECEIVED',
        )

        result = WhatsAppAutomationService.handle_inbound_keyword_action(
            inbound_message=inbound_msg,
            body_text='I need to RESCHEDULE my trial',
            db_alias='tenant_test',
        )

        self.assertEqual(result.get('action'), 'RESCHEDULE_REQUESTED')

        task = SalesFollowupTask.objects.using('tenant_test').filter(
            lead=self.lead,
            priority='URGENT',
        ).first()
        self.assertIsNotNone(task)
        self.assertIn('Reschedule Request', task.outcome)

    # ── Test 7: Test Send API Endpoint ────────────────────────────────────────

    @patch('requests.post')
    def test_07_test_send_api_endpoint(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gs-msg-test-send-999',
        }
        mock_post.return_value = mock_resp

        url = '/api/v1/tenant/crm/whatsapp-policy/test-send/'
        payload = {
            'recipient': '+919876543210',
            'template_name': 'sweat_test_template',
            'sample_variables': ['John Doe', 'Pilates Reformer', 'Today at 6:00 PM', 'Coach Alex'],
        }

        res = self.client.post(url, data=payload, format='json', **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data.get('success'))
        self.assertEqual(res.data.get('provider_message_id'), 'gs-msg-test-send-999')

    # ── Test 8: CRM Communication Log Reflection ──────────────────────────────

    def test_08_crm_communication_log_reflection(self):
        CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            lead=self.lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            recipient='+919876543210',
            body_snapshot='Hi Karan, your trial session is booked for Pilates Flow at 2:00 PM.',
            provider='GUPSHUP',
            provider_message_id='gs-crm-log-01',
            status='DELIVERED',
            trigger_type='TRIAL_CONFIRMATION',
        )

        url = '/api/v1/tenant/crm/communications/?channel=WHATSAPP'
        res = self.client.get(url, **self.auth_headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        self.assertTrue(any(m['provider_message_id'] == 'gs-crm-log-01' for m in results))
