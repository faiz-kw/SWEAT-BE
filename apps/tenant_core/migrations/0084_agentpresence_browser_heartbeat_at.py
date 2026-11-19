from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0083_activity_audit_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentpresence',
            name='browser_heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
