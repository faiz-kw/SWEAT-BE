from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0086_agentpresence_last_call_assigned_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='discounteligibilityrule',
            name='configuration',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
