# Generated manually for Meta Page Registry

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0029_tenantbranding_email_footer_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetaPageRegistry',
            fields=[
                ('page_id', models.CharField(max_length=100, primary_key=True, serialize=False)),
                ('page_name', models.CharField(blank=True, default='', max_length=255)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='meta_page_registrations', to='master.tenant')),
            ],
            options={
                'db_table': 'master_meta_page_registry',
                'indexes': [models.Index(fields=['tenant', 'is_active'], name='master_meta_tenant__e7f41a_idx')],
            },
        ),
    ]
