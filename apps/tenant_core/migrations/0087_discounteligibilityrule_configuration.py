from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0085_attendancepolicyset_restriction_duration_days_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='discounteligibilityrule',
            name='configuration',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
