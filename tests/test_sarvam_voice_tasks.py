"""
backend/tests/test_sarvam_voice_tasks.py — Unit Tests for Sarvam Voice Calling Celery Tasks.

Validates:
1. trigger_automated_sarvam_call_task dispatches outbound call within calling hours.
2. trigger_automated_sarvam_call_task calculates morning reschedule when outside calling hours.
3. trigger_automated_sarvam_call_task skips lead when voice calling consent is missing.
4. process_trial_voice_reminders_task dispatches reminder calls for trials in the 24h window.
"""

import uuid
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_crm import Lead, LeadSource, TrialBooking
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord
from apps.tenant_core.tasks_voice_calling import (
    trigger_automated_sarvam_call_task,
    process_trial_voice_reminders_task,
)


@override_settings(
    COMMUNICATIONS_OUTBOUND_ENABLED=True,
    VOICE_CALLING_ENABLED=True,
    VOICE_CALLING_ENFORCE_HOURS=True,
    VOICE_CALLING_HOURS_START=9,
    VOICE_CALLING_HOURS_END=21,
)
class SarvamVoiceTasksTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')

        self.tenant = Tenant.objects.using('default').create(
            code='CRM-SARVAM-TASK',
            name='Tenant Voice Tasks Gym',
            slug='tenant-voice-tasks-gym',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        self.org = Organization.objects.using('tenant_test').create(
            name='SWEAT Tasks Org',
            status='ACTIVE',
        )
        self.staff_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='staff_tasks@sarvam.test',
            first_name='Staff',
            last_name='User',
            status='ACTIVE',
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Mumbai Central',
            city='Mumbai',
            state='Maharashtra',
            country='IN',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='SWEAT Tasks Studio',
            status='ACTIVE',
        )
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            name='Meta Ads',
            source_type='META',
            status='ACTIVE',
        )

        self.sarvam_integration = Integration.objects.using('tenant_test').create(
            integration_type='CALLING',
            provider='SARVAM',
            status='ACTIVE',
            configuration={
                'org_id': 'org-tasks-123',
                'workspace_id': 'ws-tasks-123',
                'app_id': 'app-tasks-123',
                'connection_id': 'conn-tasks-123',
                'agent_phone_number': '+917971443277',
            },
            secret_reference='env://SARVAM_API_KEY',
        )

        self.voice_purpose, _ = ProcessingPurpose.objects.using('tenant_test').get_or_create(
            code='VOICE_CALL',
            defaults={
                'name': 'AI Voice Calling',
                'description': 'Explicit consent for automated and AI voice calls',
            },
        )

    @patch('apps.tenant_core.services_voice_calling.VoiceCallingService.initiate_outbound_call')
    def test_automated_call_dispatched_during_calling_hours(self, mock_initiate):
        """During 09:00 - 21:00 IST with voice consent, task initiates outbound call."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Ananya',
            last_name='Pandey',
            phone_normalized='+919820111222',
            current_status='NEW_LEAD',
        )
        ConsentRecord.objects.using('tenant_test').create(
            user=self.staff_user,
            purpose=self.voice_purpose,
            subject_type='lead',
            subject_id=lead.id,
            status='GRANTED',
            capture_source='META_LEAD_FORM',
            notice_version='1.0',
        )

        # Mock noon time in IST (14:00 IST)
        mock_now = timezone.now().replace(hour=8, minute=30)  # UTC 08:30 is IST 14:00
        mock_session = MagicMock(id=uuid.uuid4(), status='INITIATED')
        mock_initiate.return_value = mock_session

        with patch('django.utils.timezone.now', return_value=mock_now):
            res = trigger_automated_sarvam_call_task(
                tenant_id=str(self.tenant.id),
                lead_id=str(lead.id),
            )

        self.assertEqual(res['status'], 'INITIATED')
        self.assertEqual(res['lead_id'], str(lead.id))
        mock_initiate.assert_called_once()

    def test_automated_call_skipped_without_voice_consent(self):
        """Lead without explicit voice consent must be skipped without dispatch."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Rohit',
            last_name='Sharma',
            phone_normalized='+919820333444',
            current_status='NEW_LEAD',
        )
        # Note: Zero ConsentRecord created for this lead

        mock_now = timezone.now().replace(hour=8, minute=30)  # IST 14:00
        with patch('django.utils.timezone.now', return_value=mock_now):
            res = trigger_automated_sarvam_call_task(
                tenant_id=str(self.tenant.id),
                lead_id=str(lead.id),
            )

        self.assertEqual(res['status'], 'SKIPPED_NO_CONSENT')

    @patch('apps.tenant_core.tasks_voice_calling.trigger_automated_sarvam_call_task.apply_async')
    def test_automated_call_rescheduled_outside_calling_hours(self, mock_apply_async):
        """Lead arriving late at night (e.g. 23:00 IST) must be scheduled for next morning 09:15 IST."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Kavita',
            last_name='Singh',
            phone_normalized='+919820555666',
            current_status='NEW_LEAD',
        )
        ConsentRecord.objects.using('tenant_test').create(
            user=self.staff_user,
            purpose=self.voice_purpose,
            subject_type='lead',
            subject_id=lead.id,
            status='GRANTED',
            capture_source='META_LEAD_FORM',
            notice_version='1.0',
        )

        # UTC 17:45 is IST 23:15 (night)
        mock_now = timezone.now().replace(hour=17, minute=45)
        with patch('django.utils.timezone.now', return_value=mock_now):
            res = trigger_automated_sarvam_call_task(
                tenant_id=str(self.tenant.id),
                lead_id=str(lead.id),
            )

        self.assertEqual(res['status'], 'SCHEDULED_FOR_MORNING')
        mock_apply_async.assert_called_once()
        _, kwargs = mock_apply_async.call_args
        self.assertIn('countdown', kwargs)
        self.assertGreater(kwargs['countdown'], 0)

    @patch('apps.tenant_core.services_voice_calling.VoiceCallingService.initiate_outbound_call')
    def test_process_trial_voice_reminders_task_dispatches_for_upcoming_trial(self, mock_initiate):
        """Trial booked for 24h from now receives voice reminder call."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Vikram',
            last_name='Batra',
            phone_normalized='+919820777888',
            current_status='TRIAL_CONFIRMED',
        )
        ConsentRecord.objects.using('tenant_test').create(
            user=self.staff_user,
            purpose=self.voice_purpose,
            subject_type='lead',
            subject_id=lead.id,
            status='GRANTED',
            capture_source='META_LEAD_FORM',
            notice_version='1.0',
        )

        trial = TrialBooking.objects.using('tenant_test').create(
            lead=lead,
            branch=self.branch,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
            scheduled_start=timezone.now() + timedelta(hours=24),
            scheduled_end=timezone.now() + timedelta(hours=25),
        )

        res = process_trial_voice_reminders_task(tenant_id=str(self.tenant.id))

        self.assertEqual(res['status'], 'COMPLETED')
        self.assertEqual(res['dispatched_count'], 1)
        mock_initiate.assert_called_once()
