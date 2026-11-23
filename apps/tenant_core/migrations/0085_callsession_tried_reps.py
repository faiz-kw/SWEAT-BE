from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0084_agentpresence_browser_heartbeat_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='callsession',
            name='tried_reps',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
