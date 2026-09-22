# Generated for Layer 2 Module B: CRM Configurable Agent Assignment
import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0039_crmattentionpolicy'),
    ]

    operations = [
        migrations.CreateModel(
            name='CRMAgentAssignmentConfig',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('allowed_role_codes', models.JSONField(blank=True, default=list)),
                ('excluded_user_ids', models.JSONField(blank=True, default=list)),
                ('allow_all_staff_fallback', models.BooleanField(default=True)),
                ('require_branch_match', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.OneToOneField(
                    db_column='organization_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='crm_agent_assignment_config',
                    to='tenant_core.organization'
                )),
            ],
            options={
                'verbose_name': 'CRM Agent Assignment Config',
                'verbose_name_plural': 'CRM Agent Assignment Configs',
                'db_table': 'crm_agent_assignment_configs',
            },
        ),
    ]
