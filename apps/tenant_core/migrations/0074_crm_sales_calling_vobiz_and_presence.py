# Generated for SWEAT Vobiz Telephony, Sales Calling & Presence Engine
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0073_memberassessment_alternative_packages_payload_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentPresence',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sip_username', models.CharField(help_text='e.g. agent_priya', max_length=100, unique=True)),
                ('status', models.CharField(choices=[('OFFLINE', 'Offline'), ('AVAILABLE', 'Available'), ('ON_CALL', 'On Active Call'), ('WRAP_UP', 'In Wrap-Up (Completing Notes)')], db_index=True, default='OFFLINE', max_length=20)),
                ('last_heartbeat', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'crm_telephony_agent_presence',
            },
        ),
        migrations.AddField(
            model_name='callsession',
            name='bill_duration_seconds',
            field=models.IntegerField(default=0, help_text='Billed seconds based on 60s pulse'),
        ),
        migrations.AddField(
            model_name='callsession',
            name='branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='call_sessions', to='tenant_core.branch'),
        ),
        migrations.AddField(
            model_name='callsession',
            name='call_mode',
            field=models.CharField(choices=[('AI_AUTOMATED', 'AI Automated (Sarvam)'), ('HUMAN_MANUAL', 'Human Manual (Vobiz/Zoiper)')], default='AI_AUTOMATED', max_length=30),
        ),
        migrations.AddField(
            model_name='callsession',
            name='disposition',
            field=models.CharField(blank=True, choices=[('TRIAL_BOOKED', 'Trial Booked / Scheduled'), ('INTERESTED_FOLLOWUP', 'Interested (Follow-up Scheduled)'), ('PRICE_OBJECTION', 'Price / Budget Objection'), ('LOCATION_DISTANCE', 'Location / Distance Issue'), ('BUSY_CALLBACK', 'Busy - Asked for Callback'), ('RINGING_NO_ANSWER', 'Ringing / No Answer'), ('INVALID_WRONG_NUMBER', 'Invalid / Wrong Number'), ('NOT_INTERESTED', 'Not Interested'), ('CONVERTED_MEMBER', 'Converted to Member')], db_index=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='callsession',
            name='disposition_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='callsession',
            name='disposition_submitted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='callsession',
            name='hangup_cause',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='callsession',
            name='is_meaningful',
            field=models.BooleanField(db_index=True, default=False, help_text='True if connected and duration >= 30 seconds'),
        ),
        migrations.AddField(
            model_name='callsession',
            name='recording_storage_key',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='callsession',
            name='recording_url',
            field=models.URLField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='callsession',
            name='sales_rep',
            field=models.ForeignKey(blank=True, help_text='Sales representative who handled this call', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='handled_call_sessions', to='tenant_core.tenantuser'),
        ),
        migrations.AddIndex(
            model_name='callsession',
            index=models.Index(fields=['sales_rep', 'created_at'], name='idx_call_rep_created'),
        ),
        migrations.AddIndex(
            model_name='callsession',
            index=models.Index(fields=['organization', 'is_meaningful'], name='idx_call_org_meaningful'),
        ),
        migrations.AddField(
            model_name='agentpresence',
            name='active_session',
            field=models.ForeignKey(blank=True, db_column='active_session_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='tenant_core.callsession'),
        ),
        migrations.AddField(
            model_name='agentpresence',
            name='organization',
            field=models.ForeignKey(db_column='organization_id', on_delete=django.db.models.deletion.CASCADE, related_name='agent_presences', to='tenant_core.organization'),
        ),
        migrations.AddField(
            model_name='agentpresence',
            name='user',
            field=models.OneToOneField(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='telephony_presence', to='tenant_core.tenantuser'),
        ),
        migrations.AddIndex(
            model_name='agentpresence',
            index=models.Index(fields=['organization', 'status'], name='idx_pres_org_status'),
        ),
    ]
