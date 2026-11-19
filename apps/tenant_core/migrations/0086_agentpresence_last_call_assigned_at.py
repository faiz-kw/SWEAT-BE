from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0085_callsession_tried_reps'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentpresence',
            name='last_call_assigned_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
