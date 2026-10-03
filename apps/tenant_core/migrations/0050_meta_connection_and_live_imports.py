# Generated manually for MetaConnection, MetaPageConnection and live import fields

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0049_metaleadmapping_configurable_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetaConnection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('NOT_CONNECTED', 'Not Connected'), ('CONNECTED', 'Connected'), ('TOKEN_EXPIRED', 'Token Expired'), ('REVOKED', 'Revoked'), ('DISCONNECTED', 'Disconnected')], default='NOT_CONNECTED', max_length=30)),
                ('meta_user_id', models.CharField(blank=True, default='', max_length=100)),
                ('meta_user_name', models.CharField(blank=True, default='', max_length=200)),
                ('encrypted_user_access_token', models.TextField(blank=True, default='')),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('scopes', models.JSONField(blank=True, default=list)),
                ('last_connected_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='tenant_core.organization')),
            ],
            options={
                'db_table': 'crm_meta_connections',
                'constraints': [models.UniqueConstraint(fields=('organization',), name='uq_meta_connection_org')],
            },
        ),
        migrations.CreateModel(
            name='MetaPageConnection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('page_id', models.CharField(max_length=100)),
                ('page_name', models.CharField(max_length=255)),
                ('encrypted_page_access_token', models.TextField(blank=True, default='')),
                ('is_subscribed_to_webhooks', models.BooleanField(default=False)),
                ('subscribed_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('connection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pages', to='tenant_core.metaconnection')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='tenant_core.organization')),
            ],
            options={
                'db_table': 'crm_meta_page_connections',
                'ordering': ['page_name', 'page_id'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'page_id'), name='uq_meta_page_connection_org_page')],
            },
        ),
        migrations.AlterField(
            model_name='metaleadimport',
            name='mode',
            field=models.CharField(choices=[('SIMULATOR', 'Simulator'), ('LIVE', 'Live Webhook')], default='SIMULATOR', max_length=20),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='campaign_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='campaign_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='adset_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='adset_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='ad_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='ad_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='metaleadimport',
            name='is_organic',
            field=models.BooleanField(default=False),
        ),
    ]
