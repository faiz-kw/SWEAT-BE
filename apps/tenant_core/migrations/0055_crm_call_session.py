# Generated for SWEAT Sarvam AI Voice Calling Foundation
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0054_intakesubmission_signature_data_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='CallSession',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(default='SARVAM', help_text='Voice provider e.g. SARVAM', max_length=50)),
                ('provider_attempt_id', models.CharField(blank=True, db_index=True, help_text='Provider attempt/session identifier e.g. Sarvam attempt_id', max_length=255, null=True)),
                ('status', models.CharField(choices=[('QUEUED', 'Queued'), ('INITIATED', 'Initiated'), ('RINGING', 'Ringing'), ('CONNECTED', 'Connected / In Progress'), ('COMPLETED', 'Completed'), ('BUSY', 'Busy'), ('NO_ANSWER', 'No Answer'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled')], default='QUEUED', max_length=30)),
                ('direction', models.CharField(choices=[('OUTBOUND', 'Outbound'), ('INBOUND', 'Inbound')], default='OUTBOUND', max_length=20)),
                ('agent_phone_number', models.CharField(blank=True, default='', help_text='Sender / Rented phone number', max_length=50)),
                ('user_phone_number', models.CharField(blank=True, default='', help_text='Recipient phone number in E.164', max_length=50)),
                ('duration_seconds', models.FloatField(default=0.0, help_text='Billable call duration in seconds')),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('transcript', models.JSONField(blank=True, default=list, help_text='Conversation turns list: [{"role": "agent"|"user", "en_text": "...", "indic_text": "..."}]')),
                ('final_agent_variables', models.JSONField(blank=True, default=dict, help_text='Variables extracted/updated by Sarvam voice agent during dialogue')),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Custom correlation metadata echoed by provider callback')),
                ('error_code', models.CharField(blank=True, default='', max_length=100)),
                ('error_message', models.TextField(blank=True, default='')),
                ('idempotency_key', models.CharField(blank=True, db_index=True, help_text='Deterministic client idempotency key to prevent duplicate call placement', max_length=255, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('lead', models.ForeignKey(blank=True, db_column='lead_id', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='call_sessions', to='tenant_core.lead')),
                ('organization', models.ForeignKey(db_column='organization_id', on_delete=django.db.models.deletion.CASCADE, related_name='call_sessions', to='tenant_core.organization')),
            ],
            options={
                'db_table': 'crm_call_sessions',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='callsession',
            index=models.Index(fields=['organization', 'status'], name='idx_call_org_status'),
        ),
        migrations.AddIndex(
            model_name='callsession',
            index=models.Index(fields=['lead', 'created_at'], name='idx_call_lead_created'),
        ),
        migrations.AddIndex(
            model_name='callsession',
            index=models.Index(fields=['provider', 'provider_attempt_id'], name='idx_call_prov_attempt'),
        ),
        migrations.AddConstraint(
            model_name='callsession',
            constraint=models.UniqueConstraint(condition=models.Q(('idempotency_key__isnull', False)), fields=('organization', 'idempotency_key'), name='uq_call_org_idempotency_key'),
        ),
        migrations.AddConstraint(
            model_name='callsession',
            constraint=models.UniqueConstraint(condition=models.Q(('provider_attempt_id__isnull', False)), fields=('organization', 'provider_attempt_id'), name='uq_call_org_attempt_id'),
        ),
    ]
