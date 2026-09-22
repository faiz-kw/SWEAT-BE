"""
backend/tests/test_crm_phase5_communication.py — Targeted Tests for CRM Phase 5: Omnichannel Communication Layer

Verifies:
1. Deterministic Webhook Tenant Routing (resolves tenant in Master DB before tenant DB query)
2. Unknown inbound sender persists as UNRESOLVED (lead=None, never guesses)
3. Ambiguous inbound sender persists as AMBIGUOUS (lead=None, never guesses)
4. Out-of-order webhook delivery protection (READ cannot regress to DELIVERED)
5. Append-only status event database idempotency
6. Inbound customer reply created as separate CommunicationMessage
7. Trial booking succeeds even when communication provider is unavailable
8. Provider failure does not rollback TrialBooking transaction
9. Cancelled trial reminder is suppressed
10. Rescheduled old trial reminder is suppressed
11. Durable reminders survive worker delay and retry without duplication
12. Policy change behavior is deterministic
13. Do Not Contact (DNC) and channel consent strictly enforced on marketing
14. Transactional vs Marketing communication purpose differentiation
15. Template deletion/editing preserves immutable historical message snapshot
16. Secret references and credentials are NEVER exposed to frontend APIs
17. Correct provider adapter resolved from ProviderRegistry by (channel, provider)
18. Database-level message idempotency (organization + idempotency_key)
19. RBAC permission checks (crm.communications.view and crm.communications.send)
20. Follow-up task database idempotency closeout (lead + external_reference)
"""

import uuid
from datetime import timedelta, date, time
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from django.db import IntegrityError

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
    SalesFollowupTask,
    CRMTrialReminderPolicy,
)
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
)
from apps.tenant_core.models_govern import NotificationTemplate
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_communication import CommunicationMessage, CommunicationStatusEvent
from apps.tenant_core.communication.service import CommunicationService, ConsentViolationError
from apps.tenant_core.communication.registry import CommunicationProviderRegistry
from apps.tenant_core.communication.adapters.whatsapp_meta import MetaWhatsAppAdapter
from apps.tenant_core.communication.adapters.email_smtp import SMTPEmailAdapter
from apps.tenant_core.communication.adapters.sms_twilio import TwilioSMSAdapter
from apps.tenant_core.services_crm import CRMLeadService


class CRMPhase5CommunicationTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A5',
            name='Tenant A5 Fitness',
            slug='tenant-a5-fitness',
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
            code='ENT-PLAN-P5',
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
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A5',
            name='Org A5 Core',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A5',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-A5-1',
            name='Downtown Club',
            status='ACTIVE',
        )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin@tenanta5.com',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a,
            is_primary=True,
            is_active=True,
        )
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ORG_ADMIN',
            name='Org Admin',
            is_system=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin,
            role=self.role_admin,
            scope_type='ORG',
        )

        self.user_sales = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='sales@tenanta5.com',
            first_name='Sales',
            last_name='Rep',
            status='ACTIVE',
            is_login_allowed=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_sales,
            branch=self.branch_a,
            is_primary=True,
            is_active=True,
        )
        self.role_sales = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='SALES_REP',
            name='Sales Rep',
            is_system=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_sales,
            role=self.role_sales,
            scope_type='BRANCH',
            branch=self.branch_a,
        )

        # Module / RBAC catalog
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

        self.perm_set_sales, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.role_sales, name='Sales Permissions'
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_sales, module=self.mod_crm, defaults={'can_access': True, 'permission_set': self.perm_set_sales}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_sales, submodule=self.submod_leads, defaults={'can_access': True, 'permission_set': self.perm_set_sales}
        )
        for p_code in [
            'crm.leads.view', 'crm.leads.edit', 'crm.leads.create',
            'crm.communications.view', 'crm.communications.send',
            'crm.settings.view',
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
                permission_set=self.perm_set_sales, permission=perm
            )
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=self.perm_set_admin, permission=perm
            )

        # Standard Lead
        self.source_walkin = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='WALK_IN', name='Walk In', source_type='WALK_IN', status='ACTIVE'
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.source_walkin,
            first_name='Rohan',
            last_name='Sharma',
            phone_normalized='+919876543210',
            email_normalized='rohan.sharma@example.com',
            current_status='NEW_LEAD',
            do_not_contact=False,
            consent_whatsapp=True,
            consent_email=True,
            consent_sms=True,
        )

        # Auth Token
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_sales = str(_build_tenant_token(self.user_sales, self.tenant_a, 'tenant_test').access_token)

    def test_01_webhook_resolves_tenant_before_tenant_db_query(self):
        """Unknown public integration ID returns 404 without guessing or querying tenant databases."""
        url_unknown = f"/api/v1/webhooks/communications/META/{uuid.uuid4()}/"
        res = self.client.post(url_unknown, {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Valid tenant resolves cleanly
        url_valid = f"/api/v1/webhooks/communications/META/{self.tenant_a.id}/"
        res_valid = self.client.post(url_valid, {'entry': []}, format='json')
        self.assertEqual(res_valid.status_code, status.HTTP_200_OK)

    def test_02_unknown_inbound_sender_persists_as_unresolved(self):
        """Inbound WhatsApp from unknown phone persists as UNRESOLVED and never links to an arbitrary lead."""
        inbound_payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'id': 'wamid_unknown_001',
                            'from': '919999988888',
                            'type': 'text',
                            'text': {'body': 'Hello, tell me your membership fees'},
                        }]
                    }
                }]
            }]
        }
        url = f"/api/v1/webhooks/communications/META/{self.tenant_a.id}/"
        res = self.client.post(url, inbound_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['resolution_status'], 'UNRESOLVED')

        msg = CommunicationMessage.objects.using('tenant_test').get(provider_message_id='wamid_unknown_001')
        self.assertIsNone(msg.lead)
        self.assertEqual(msg.resolution_status, 'UNRESOLVED')
        self.assertEqual(msg.direction, 'INBOUND')
        self.assertEqual(msg.body_snapshot, 'Hello, tell me your membership fees')

    def test_03_ambiguous_inbound_sender_persists_as_ambiguous(self):
        """Multiple leads sharing a phone number persists as AMBIGUOUS and does not guess."""
        # Create second lead with same phone
        Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Rohan',
            last_name='Duplicate',
            phone_normalized='+919876543210',
            current_status='NEW_LEAD',
        )

        inbound_payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'id': 'wamid_ambig_002',
                            'from': '919876543210',
                            'type': 'text',
                            'text': {'body': 'I want to reschedule'},
                        }]
                    }
                }]
            }]
        }
        url = f"/api/v1/webhooks/communications/META/{self.tenant_a.id}/"
        res = self.client.post(url, inbound_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['resolution_status'], 'AMBIGUOUS')

        msg = CommunicationMessage.objects.using('tenant_test').get(provider_message_id='wamid_ambig_002')
        self.assertIsNone(msg.lead)
        self.assertEqual(msg.resolution_status, 'AMBIGUOUS')

    def test_04_status_cannot_regress_read_to_delivered(self):
        """A message at READ status cannot regress to DELIVERED when an out-of-order webhook arrives."""
        msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            recipient=self.lead.phone_normalized,
            body_snapshot='Your session is booked',
            status='READ',
            read_at=timezone.now(),
            provider='META',
            provider_message_id='wamid_msg_regress_test',
        )

        # Delayed DELIVERED webhook arrives
        webhook_payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'statuses': [{
                            'id': 'wamid_msg_regress_test',
                            'status': 'delivered',
                            'timestamp': str(int(timezone.now().timestamp())),
                        }]
                    }
                }]
            }]
        }
        url = f"/api/v1/webhooks/communications/META/{self.tenant_a.id}/"
        res = self.client.post(url, webhook_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        msg = CommunicationMessage.objects.using('tenant_test').get(id=msg.id)
        self.assertEqual(msg.status, 'READ', "Status must NOT regress from READ to DELIVERED")

        # But status event is still appended
        event = CommunicationStatusEvent.objects.using('tenant_test').filter(message=msg, to_status='DELIVERED').first()
        self.assertIsNotNone(event, "Append-only status event must be recorded for audit trail")

    def test_05_duplicate_provider_event_blocked_at_database_level(self):
        """CommunicationStatusEvent enforces database-level idempotency on provider_event_id."""
        msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead,
            channel='WHATSAPP',
            recipient=self.lead.phone_normalized,
            body_snapshot='Test msg',
            status='SENT',
        )

        CommunicationService.record_status_event(
            message=msg,
            to_status='DELIVERED',
            provider_event_id='unique_meta_evt_99',
        )

        # Direct duplicate DB insert attempt triggers constraint
        with self.assertRaises(IntegrityError):
            CommunicationStatusEvent.objects.using('tenant_test').create(
                message=msg,
                to_status='DELIVERED',
                provider_event_id='unique_meta_evt_99',
            )

    def test_06_inbound_reply_created_as_separate_communication_message(self):
        """Customer reply is persisted as a distinct INBOUND CommunicationMessage record."""
        outbound = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            recipient=self.lead.phone_normalized,
            body_snapshot='Will you attend class today?',
            status='DELIVERED',
            provider='META',
            provider_message_id='wamid_outbound_parent',
        )

        reply = CommunicationService.ingest_inbound_message(
            organization=self.org_a,
            channel='WHATSAPP',
            sender_identifier='+919876543210',
            body='Yes I will be there!',
            provider='META',
            provider_message_id='wamid_reply_child',
            in_reply_to_provider_id='wamid_outbound_parent',
        )

        self.assertNotEqual(reply.id, outbound.id)
        self.assertEqual(reply.direction, 'INBOUND')
        self.assertEqual(reply.in_reply_to_id, outbound.id)
        self.assertEqual(reply.lead_id, self.lead.id)

        outbound = CommunicationMessage.objects.using('tenant_test').get(id=outbound.id)
        self.assertEqual(outbound.status, 'REPLIED')
        self.assertIsNotNone(outbound.replied_at)

    def test_07_booking_succeeds_even_when_communication_provider_unavailable(self):
        """TrialBooking succeeds and commits even if messaging provider is unavailable or unconfigured."""
        now = timezone.now()
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Strength', code='STR')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='HIIT Blast', code='HIIT', default_capacity=20
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(days=2)).date(),
            start_at=now + timedelta(days=2),
            end_at=now + timedelta(days=2, hours=1),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )

        # Booking executes without error
        trial = CRMLeadService.book_trial(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            actor_user=self.user_admin,
            db_alias='tenant_test',
        )
        self.assertIsNotNone(trial)
        self.assertEqual(trial.status, 'BOOKED')
        self.assertEqual(trial.confirmation_status, 'PENDING')

    def test_08_provider_failure_does_not_rollback_trial_booking(self):
        """CommunicationService.send_trial_confirmation never throws or rollbacks TrialBooking."""
        now = timezone.now()
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Yoga', code='YOG')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='Morning Flow', code='YOGA', default_capacity=15
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(days=3)).date(),
            start_at=now + timedelta(days=3),
            end_at=now + timedelta(days=3, hours=1),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='BOOKED',
            confirmation_status='PENDING',
        )

        # Dispatches safely without throwing exception
        msg = CommunicationService.send_trial_confirmation(trial, channel='WHATSAPP')
        # Since WhatsApp is not live configured, msg fails gracefully without crashing
        self.assertIsNotNone(msg)
        self.assertEqual(msg.status, 'FAILED')
        self.assertEqual(msg.failure_code, 'UNCONFIGURED_PROVIDER')

        # Trial remains intact in database
        trial = TrialBooking.objects.using('tenant_test').get(id=trial.id)
        self.assertEqual(trial.status, 'BOOKED')

    def test_09_cancelled_trial_reminder_is_not_sent(self):
        """Cancelled trials are suppressed from reminder dispatch."""
        now = timezone.now()
        CRMTrialReminderPolicy.objects.using('tenant_test').update_or_create(
            organization=self.org_a,
            defaults={
                'immediate_whatsapp': True,
                'reminder_offsets': [24, 2],
            }
        )
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Spin', code='SPN')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='Spin Rush', code='SPIN', default_capacity=15
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(hours=20)).date(),
            start_at=now + timedelta(hours=20),
            end_at=now + timedelta(hours=21),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='CANCELLED',  # Cancelled!
            cancellation_reason='Member requested cancellation',
        )

        dispatched = CommunicationService.process_due_trial_reminders(as_of_time=now)
        # Should not dispatch reminder for cancelled trial
        reminders = [m for m in dispatched if m.related_trial_id == trial.id]
        self.assertEqual(len(reminders), 0)

    def test_10_rescheduled_old_trial_reminder_is_not_sent(self):
        """Rescheduled original trial does not send reminders."""
        now = timezone.now()
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Boxing', code='BOX')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='Boxing Fit', code='BOX', default_capacity=15
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(hours=20)).date(),
            start_at=now + timedelta(hours=20),
            end_at=now + timedelta(hours=21),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )
        old_trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='RESCHEDULED',  # Rescheduled!
        )

        dispatched = CommunicationService.process_due_trial_reminders(as_of_time=now)
        reminders = [m for m in dispatched if m.related_trial_id == old_trial.id]
        self.assertEqual(len(reminders), 0)

    def test_11_reminder_survives_worker_delay_and_retry(self):
        """Reminders due within the delivery window are dispatched and not duplicated on retry."""
        now = timezone.now()
        CRMTrialReminderPolicy.objects.using('tenant_test').update_or_create(
            organization=self.org_a,
            defaults={
                'immediate_whatsapp': True,
                'immediate_email': False,
                'immediate_sms': False,
                'reminder_offsets': [24],
            }
        )
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Pilates', code='PIL')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='Reformer Pilates', code='PIL', default_capacity=10
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(hours=18)).date(),
            start_at=now + timedelta(hours=18),  # 24h reminder is due
            end_at=now + timedelta(hours=19),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='BOOKED',
            confirmation_status='PENDING',
        )

        # First worker run: dispatches
        run1 = CommunicationService.process_due_trial_reminders(as_of_time=now)
        self.assertEqual(len(run1), 1)
        self.assertEqual(run1[0].related_trial_id, trial.id)

        # Second worker run (retry/delay): idempotency suppresses duplicate
        run2 = CommunicationService.process_due_trial_reminders(as_of_time=now + timedelta(minutes=30))
        self.assertEqual(len(run2), 0)

    def test_12_policy_change_behavior_is_deterministic(self):
        """Historical sent reminders remain immutable; updated policy alters future unsent reminders."""
        now = timezone.now()
        policy, _ = CRMTrialReminderPolicy.objects.using('tenant_test').update_or_create(
            organization=self.org_a,
            defaults={
                'immediate_whatsapp': True,
                'immediate_email': False,
                'immediate_sms': False,
                'reminder_offsets': [24],
            }
        )
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org_a, name='Circuit', code='CIR')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org_a, category=cat, name='Circuit X', code='CIR', default_capacity=12
        )
        occ = ClassOccurrence.objects.using('tenant_test').create(
            branch=self.branch_a,
            class_template=tmpl,
            occurrence_date=(now + timedelta(hours=15)).date(),
            start_at=now + timedelta(hours=15),
            end_at=now + timedelta(hours=16),
            capacity=10,
            trial_capacity=5,
            status='OPEN',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_a,
            class_occurrence_id=occ.id,
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='BOOKED',
        )

        # 24h reminder dispatched
        dispatched = CommunicationService.process_due_trial_reminders(as_of_time=now)
        self.assertEqual(len(dispatched), 1)

        # Admin updates policy to add 2h reminder
        policy.reminder_offsets = [24, 2]
        policy.save(using='tenant_test')

        # Now advances to 1 hour before class (2h reminder point has passed)
        advance_now = occ.start_at - timedelta(hours=1)
        run_updated = CommunicationService.process_due_trial_reminders(as_of_time=advance_now)
        # Should dispatch newly configured 2h reminder
        self.assertEqual(len(run_updated), 1)
        self.assertTrue('TRIAL_REMINDER:' in run_updated[0].idempotency_key)

    def test_13_do_not_contact_and_consent_enforced(self):
        """Marketing messages fail closed if Lead is Do Not Contact or has not consented to the channel."""
        # 1. Lead has DNC=True
        self.lead.do_not_contact = True
        self.lead.save(using='tenant_test')

        with self.assertRaises(ConsentViolationError):
            CommunicationService.send_communication(
                organization=self.org_a,
                channel='WHATSAPP',
                recipient=self.lead.phone_normalized,
                lead=self.lead,
                body='Exclusive 50% discount on annual plan!',
                purpose='MARKETING',
            )

        # 2. Reset DNC, remove SMS consent
        self.lead.do_not_contact = False
        self.lead.consent_sms = False
        self.lead.save(using='tenant_test')

        with self.assertRaises(ConsentViolationError):
            CommunicationService.send_communication(
                organization=self.org_a,
                channel='SMS',
                recipient=self.lead.phone_normalized,
                lead=self.lead,
                body='Flash sale!',
                purpose='MARKETING',
            )

        # 3. Transactional message to DNC lead is permitted (service continuity)
        self.lead.do_not_contact = True
        self.lead.save(using='tenant_test')

        tx_msg = CommunicationService.send_communication(
            organization=self.org_a,
            channel='EMAIL',
            recipient=self.lead.email_normalized,
            lead=self.lead,
            body='Here is your security OTP: 123456',
            purpose='TRANSACTIONAL',
        )
        self.assertIsNotNone(tx_msg)

    def test_14_marketing_vs_transactional_purpose_differentiated(self):
        """API distinguishes TRANSACTIONAL vs MARKETING purposes."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        url = '/api/v1/tenant/crm/communications/send/'

        self.lead.do_not_contact = True
        self.lead.save(using='tenant_test')

        payload_marketing = {
            'channel': 'WHATSAPP',
            'recipient': self.lead.phone_normalized,
            'lead_id': str(self.lead.id),
            'body': 'Special promotion for you!',
            'purpose': 'MARKETING',
        }
        res_m = self.client.post(url, payload_marketing, format='json')
        self.assertEqual(res_m.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_m.data.get('code'), 'CONSENT_DENIED')

        payload_tx = {
            'channel': 'WHATSAPP',
            'recipient': self.lead.phone_normalized,
            'lead_id': str(self.lead.id),
            'body': 'Your trial reminder.',
            'purpose': 'TRANSACTIONAL',
        }
        res_tx = self.client.post(url, payload_tx, format='json')
        self.assertEqual(res_tx.status_code, status.HTTP_201_CREATED)

    def test_15_template_deletion_preserves_immutable_snapshot(self):
        """Editing or deleting a NotificationTemplate does not alter historical body_snapshot."""
        tpl = NotificationTemplate.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Original Template',
            body='Hello {{lead_name}}, welcome to our fitness club!',
            channel='WHATSAPP',
            event_type='LEAD_WELCOME',
        )

        msg = CommunicationService.send_communication(
            organization=self.org_a,
            channel='WHATSAPP',
            recipient=self.lead.phone_normalized,
            lead=self.lead,
            template=tpl,
            context_data={'lead_name': self.lead.first_name},
            purpose='TRANSACTIONAL',
        )
        self.assertEqual(msg.body_snapshot, 'Hello Rohan, welcome to our fitness club!')

        # Later, admin edits template
        tpl.body = 'NEW EDITED CONTENT: Hey {{lead_name}}!'
        tpl.save(using='tenant_test')

        msg = CommunicationMessage.objects.using('tenant_test').get(id=msg.id)
        self.assertEqual(msg.body_snapshot, 'Hello Rohan, welcome to our fitness club!')

        # Later, admin deletes template
        tpl.delete(using='tenant_test')
        msg = CommunicationMessage.objects.using('tenant_test').get(id=msg.id)
        self.assertIsNone(msg.template)
        self.assertEqual(msg.template_reference, 'Original Template')
        self.assertEqual(msg.body_snapshot, 'Hello Rohan, welcome to our fitness club!')

    def test_16_no_secret_reference_exposed_to_frontend(self):
        """GET /crm/channels/ NEVER exposes secret references, Vault paths, or API keys."""
        Integration.objects.using('tenant_test').create(
            integration_type='WHATSAPP',
            provider='Meta',
            secret_reference='secret/data/tenants/tenant_a/meta_whatsapp_token',
            configuration={'phone_number_id': '10987654321', 'verify_token': 'secret_token_123'},
            status='ACTIVE',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get('/api/v1/tenant/crm/channels/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        body_text = str(res.content)
        self.assertNotIn('secret/data', body_text)
        self.assertNotIn('secret_reference', body_text)
        self.assertNotIn('secret_token_123', body_text)
        self.assertNotIn('vault', body_text.lower())

    def test_17_correct_provider_adapter_resolved_by_channel_and_provider(self):
        """CommunicationProviderRegistry maps (channel, provider) to concrete adapter classes."""
        self.assertEqual(
            CommunicationProviderRegistry.get_adapter_class('WHATSAPP', 'META'),
            MetaWhatsAppAdapter
        )
        self.assertEqual(
            CommunicationProviderRegistry.get_adapter_class('EMAIL', 'SMTP'),
            SMTPEmailAdapter
        )
        self.assertEqual(
            CommunicationProviderRegistry.get_adapter_class('SMS', 'TWILIO'),
            TwilioSMSAdapter
        )

    def test_18_message_db_idempotency(self):
        """UniqueConstraint(organization, idempotency_key) prevents duplicate message insertion."""
        key = f"IDEM_TEST_KEY_{uuid.uuid4()}"
        m1 = CommunicationService.send_communication(
            organization=self.org_a,
            channel='WHATSAPP',
            recipient=self.lead.phone_normalized,
            lead=self.lead,
            body='Idempotency test',
            idempotency_key=key,
        )
        m2 = CommunicationService.send_communication(
            organization=self.org_a,
            channel='WHATSAPP',
            recipient=self.lead.phone_normalized,
            lead=self.lead,
            body='Idempotency test duplicate call',
            idempotency_key=key,
        )
        self.assertEqual(m1.id, m2.id)

    def test_19_rbac_read_only_user_cannot_send_communication(self):
        """User without crm.communications.send permission receives 403 Forbidden."""
        # Create read-only user
        user_ro = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='readonly@tenanta5.com',
            first_name='Read',
            last_name='Only',
            status='ACTIVE',
            is_login_allowed=True,
        )
        role_ro = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_AUDITOR',
            name='CRM Auditor',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=user_ro,
            role=role_ro,
            scope_type='ORG',
        )
        pset_ro = RolePermissionSet.objects.using('tenant_test').create(role=role_ro, name='RO Perms')
        RoleModuleAccess.objects.using('tenant_test').create(role=role_ro, module=self.mod_crm, permission_set=pset_ro, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=role_ro, submodule=self.submod_leads, permission_set=pset_ro, can_access=True)
        perm_view = Permission.objects.using('tenant_test').get(permission_code='crm.communications.view')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=pset_ro, permission=perm_view)

        token_ro = str(_build_tenant_token(user_ro, self.tenant_a, 'tenant_test').access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token_ro}')

        # List communications: 200 OK
        res_list = self.client.get('/api/v1/tenant/crm/communications/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)

        # Send communication: 403 Forbidden
        res_send = self.client.post('/api/v1/tenant/crm/communications/send/', {
            'channel': 'WHATSAPP',
            'recipient': self.lead.phone_normalized,
            'lead_id': str(self.lead.id),
            'body': 'Unauthorized message',
        }, format='json')
        self.assertEqual(res_send.status_code, status.HTTP_403_FORBIDDEN)

    def test_20_followup_task_database_idempotency_closeout(self):
        """Step 0 Closeout: SalesFollowupTask enforces uq_sft_lead_ext_ref on (lead, external_reference)."""
        ext_ref = f"TRIAL_ATTENDED_{uuid.uuid4()}"
        t1 = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead,
            assigned_to_user=self.user_sales,
            created_by_user=self.user_admin,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(hours=2),
            external_reference=ext_ref,
        )
        self.assertIsNotNone(t1.id)

        # Attempt to insert identical external_reference for the same lead fails at DB level
        with self.assertRaises(IntegrityError):
            SalesFollowupTask.objects.using('tenant_test').create(
                lead=self.lead,
                assigned_to_user=self.user_sales,
                created_by_user=self.user_admin,
                task_type='CALL',
                priority='HIGH',
                due_at=timezone.now() + timedelta(hours=4),
                external_reference=ext_ref,
            )
