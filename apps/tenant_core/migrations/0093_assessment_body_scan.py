"""
Four-view body scan for member assessments.

Adds the back / left / right captures alongside the existing front and legacy side images,
the scan job state, the demographic inputs the metabolic equation needs, and the separate
`estimated_*` girth fields that keep scan estimates away from tape measurements.

Also drops the default from `posture_score`: it used to arrive as 85.00 on every new row,
which is indistinguishable from a real measurement. It is now nullable with no default, so an
unscanned assessment reads as "not assessed".
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0092_trainerclasscategoryassignment_and_more'),
    ]

    operations = [
        # --- Section 1 demographics -----------------------------------------------------
        migrations.AddField(
            model_name='memberassessment',
            name='member_date_of_birth',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='member_age_years',
            field=models.PositiveIntegerField(blank=True, help_text='Derived from date of birth at assessment time', null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='member_sex',
            field=models.CharField(blank=True, choices=[('FEMALE', 'Female'), ('MALE', 'Male'), ('OTHER', 'Other / self-described'), ('UNDISCLOSED', 'Prefer not to say')], default='', help_text='Used only for the metabolic estimation equation', max_length=20),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='assessment_date',
            field=models.DateField(blank=True, help_text='Date the assessment was conducted, if backdated from created_at', null=True),
        ),

        # --- Captures -------------------------------------------------------------------
        migrations.AddField(
            model_name='memberassessment',
            name='photo_back',
            field=models.ImageField(blank=True, null=True, upload_to='assessments/baseline/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='photo_side_left',
            field=models.ImageField(blank=True, null=True, upload_to='assessments/baseline/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='photo_side_right',
            field=models.ImageField(blank=True, null=True, upload_to='assessments/baseline/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='photo_back_url',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='photo_side_left_url',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='photo_side_right_url',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),

        # --- Posture metrics ------------------------------------------------------------
        migrations.AlterField(
            model_name='memberassessment',
            name='posture_score',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='0-100 posture score, derived from measured joint angles', max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='lateral_shift_degrees',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Frontal-plane deviation of the body midline', max_digits=5, null=True),
        ),
        migrations.AlterField(
            model_name='memberassessment',
            name='cv_analysis_payload',
            field=models.JSONField(blank=True, default=dict, help_text='Per-view keypoints, derived angles and the assumptions behind them'),
        ),

        # --- Scan job -------------------------------------------------------------------
        migrations.AddField(
            model_name='memberassessment',
            name='scan_status',
            field=models.CharField(choices=[('NOT_STARTED', 'No scan requested yet'), ('QUEUED', 'Queued for analysis'), ('PROCESSING', 'Analysis in progress'), ('COMPLETED', 'Analysis complete'), ('PARTIAL', 'Completed with some views unusable'), ('FAILED', 'Analysis failed'), ('UNAVAILABLE', 'Scan provider not configured'), ('SKIPPED_NO_CONSENT', 'Member did not consent to image analysis')], db_index=True, default='NOT_STARTED', max_length=30),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_provider',
            field=models.CharField(blank=True, default='', help_text='e.g. fal.ai', max_length=50),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_model',
            field=models.CharField(blank=True, default='', help_text='Exact model id used, recorded for reproducibility', max_length=120),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_error',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_capture_quality',
            field=models.JSONField(blank=True, default=dict, help_text='Per-view quality verdict: usable, warnings, reason when rejected'),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='scan_raw_payload',
            field=models.JSONField(blank=True, default=dict, help_text='Provider response per view, minus large binaries'),
        ),

        # --- Estimated girths (kept separate from tape measurements) ---------------------
        migrations.AddField(
            model_name='memberassessment',
            name='estimated_waist_cm',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='estimated_hip_cm',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='estimated_chest_cm',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='estimated_shoulder_width_cm',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
    ]
