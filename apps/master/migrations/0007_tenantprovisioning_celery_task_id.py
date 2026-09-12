# Generated for Sprint 7 Phase 7E Provisioning Foundation
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0006_reconcile_active_users_metric'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantprovisioning',
            name='celery_task_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text='Celery async task ID',
                max_length=255,
            ),
        ),
    ]
