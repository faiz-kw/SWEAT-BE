"""
Sprint 13 — Notification Templates & Branch Schedule Models Creation
Alters notification_templates field lengths and creates branch_working_hours and branch_operating_exceptions.
"""

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0015_sprint12_iam_governance_constraints'),
    ]

    operations = [
        # 1. notification_templates field length updates
        migrations.AlterField(
            model_name='notificationtemplate',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='notificationtemplate',
            name='channel',
            field=models.CharField(
                choices=[('SMS', 'SMS'), ('WHATSAPP', 'WhatsApp'), ('EMAIL', 'Email'), ('PUSH', 'Push Notification')],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name='notificationtemplate',
            name='language',
            field=models.CharField(default='en', max_length=20),
        ),

        # 2. branch_working_hours model creation
        migrations.CreateModel(
            name='BranchWorkingHours',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('day_of_week', models.SmallIntegerField()),
                ('is_open', models.BooleanField(default=True)),
                ('open_time', models.TimeField(blank=True, null=True)),
                ('close_time', models.TimeField(blank=True, null=True)),
                ('is_24_hours', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='working_hours', to='tenant_core.branch')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_working_hours', to='tenant_core.tenantuser')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_working_hours', to='tenant_core.tenantuser')),
            ],
            options={
                'db_table': 'branch_working_hours',
                'ordering': ['branch', 'day_of_week'],
            },
        ),
        migrations.AddConstraint(
            model_name='branchworkinghours',
            constraint=models.UniqueConstraint(fields=('branch', 'day_of_week'), name='uq_branch_working_hours'),
        ),
        migrations.AddConstraint(
            model_name='branchworkinghours',
            constraint=models.CheckConstraint(condition=models.Q(('day_of_week__gte', 1), ('day_of_week__lte', 7)), name='chk_bwh_day_of_week_range'),
        ),
        migrations.AddConstraint(
            model_name='branchworkinghours',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(('is_24_hours', False), ('is_open', True), ('open_time__isnull', True), _negated=True), models.Q(('close_time__isnull', True), ('is_24_hours', False), ('is_open', True), _negated=True)),
                name='chk_bwh_open_requires_times',
            ),
        ),
        migrations.AddIndex(
            model_name='branchworkinghours',
            index=models.Index(fields=['branch'], name='idx_bwh_branch_id'),
        ),

        # 3. branch_operating_exceptions model creation
        migrations.CreateModel(
            name='BranchOperatingException',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('exception_date', models.DateField()),
                ('is_closed', models.BooleanField(default=False)),
                ('open_time', models.TimeField(blank=True, null=True)),
                ('close_time', models.TimeField(blank=True, null=True)),
                ('reason', models.CharField(blank=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='operating_exceptions', to='tenant_core.branch')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_operating_exceptions', to='tenant_core.tenantuser')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_operating_exceptions', to='tenant_core.tenantuser')),
            ],
            options={
                'db_table': 'branch_operating_exceptions',
                'ordering': ['branch', 'exception_date'],
            },
        ),
        migrations.AddConstraint(
            model_name='branchoperatingexception',
            constraint=models.UniqueConstraint(fields=('branch', 'exception_date'), name='uq_branch_operating_exceptions'),
        ),
        migrations.AddConstraint(
            model_name='branchoperatingexception',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(('is_closed', False), ('open_time__isnull', True), _negated=True), models.Q(('close_time__isnull', True), ('is_closed', False), _negated=True)),
                name='chk_boe_open_requires_times',
            ),
        ),
        migrations.AddIndex(
            model_name='branchoperatingexception',
            index=models.Index(fields=['branch', 'exception_date'], name='idx_boe_branch_date'),
        ),
    ]
