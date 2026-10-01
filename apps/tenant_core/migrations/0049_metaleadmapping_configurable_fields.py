# Generated manually for Meta Lead Ads UI configuration

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0048_metaleadimport_metaleadmapping'),
    ]

    operations = [
        migrations.AddField(
            model_name='metaleadmapping',
            name='field_defaults',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='unmatched_branch_policy',
            field=models.CharField(
                choices=[('HOLD', 'Hold for review (Needs branch assignment)'), ('FALLBACK_BRANCH', 'Route to fallback branch')],
                default='HOLD',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='fallback_branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenant_core.branch'),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='initial_stage',
            field=models.CharField(default='NEW_LEAD', max_length=40),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='assignment_mode',
            field=models.CharField(
                choices=[('TENANT_POLICY', 'Follow tenant assignment policy'), ('SPECIFIC_USER', 'Assign to specific staff member')],
                default='TENANT_POLICY',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='assigned_sales_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='tenant_core.tenantuser'),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='create_followup_task',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='followup_task_type',
            field=models.CharField(default='CALL', max_length=30),
        ),
        migrations.AddField(
            model_name='metaleadmapping',
            name='followup_due_hours',
            field=models.PositiveIntegerField(default=24),
        ),
    ]
