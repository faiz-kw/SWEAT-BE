# Adds expiry and single-use tracking to the studio screen pairing code.
# Existing rows get an expiry of (migration time + 4 hours) and are unused.

import apps.tenant_core.models_assessment
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0088_merge_20261120_1352'),
    ]

    operations = [
        migrations.AddField(
            model_name='memberassessment',
            name='pairing_code_expires_at',
            field=models.DateTimeField(default=apps.tenant_core.models_assessment._default_pairing_code_expiry),
        ),
        migrations.AddField(
            model_name='memberassessment',
            name='pairing_code_used_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
