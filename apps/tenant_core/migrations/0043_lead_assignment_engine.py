# Migration 0043: Lead Assignment Engine - Auto Assign, Strategies, History & Availability
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0042_in_app_notification'),
    ]

    operations = [
        # LeadAssignment enhancements
        migrations.AddField(
            model_name='leadassignment',
            name='assignment_source',
            field=models.CharField(
                choices=[('MANUAL', 'Manual'), ('AUTO', 'Auto')],
                default='MANUAL',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='leadassignment',
            name='assignment_strategy',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
        migrations.AddField(
            model_name='leadassignment',
            name='branch',
            field=models.ForeignKey(
                blank=True,
                db_column='branch_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='lead_assignments',
                to='tenant_core.branch',
            ),
        ),
        migrations.AddField(
            model_name='leadassignment',
            name='previous_assignment',
            field=models.ForeignKey(
                blank=True,
                db_column='previous_assignment_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='subsequent_assignments',
                to='tenant_core.leadassignment',
            ),
        ),
        migrations.AddField(
            model_name='leadassignment',
            name='reason',
            field=models.TextField(blank=True, default=''),
        ),
        # CRMAgentAssignmentConfig enhancements
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='assignment_mode_allowed',
            field=models.CharField(
                choices=[('MANUAL', 'Manual Only'), ('AUTO', 'Auto Only'), ('BOTH', 'Both Manual & Auto')],
                default='BOTH',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='default_assignment_mode',
            field=models.CharField(
                choices=[('MANUAL', 'Manual'), ('AUTO', 'Auto Assign')],
                default='MANUAL',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='auto_assignment_strategy',
            field=models.CharField(
                choices=[('ROUND_ROBIN', 'Round Robin'), ('LEAST_OPEN_LEADS', 'Least Open Leads'), ('MANUAL_ONLY', 'Manual Only')],
                default='ROUND_ROBIN',
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='allow_unassigned_fallback',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='consider_leave_availability',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='notify_manager_on_unassigned',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='crmagentassignmentconfig',
            name='round_robin_state',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
