"""
Migration: 0099 — TurnstileDevice model

Adds the `turnstile_devices` table to every tenant database.
Safe to run on production — creates a new table only, touches nothing else.
"""
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0098_merge_20261125_0717'),
    ]

    operations = [
        migrations.CreateModel(
            name='TurnstileDevice',
            fields=[
                ('id', models.UUIDField(
                    default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                )),
                ('branch', models.ForeignKey(
                    help_text='Which studio branch this terminal belongs to.',
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='turnstile_devices',
                    to='tenant_core.branch',
                )),
                ('name', models.CharField(
                    help_text=(
                        'e.g. "Malad Entry", "Andheri Pilates Entry", '
                        '"Andheri Bootcamp Exit"'
                    ),
                    max_length=100,
                )),
                ('direction', models.CharField(
                    choices=[('ENTRY', 'Entry'), ('EXIT', 'Exit')],
                    default='ENTRY',
                    help_text='Whether this terminal controls entry or exit.',
                    max_length=10,
                )),
                ('host', models.CharField(
                    help_text=(
                        'IP or hostname reachable from the cloud. '
                        'Private IP (192.168.x.x) for pre-port-forwarding testing; '
                        'public IP or domain after port forwarding is set up.'
                    ),
                    max_length=255,
                )),
                ('port', models.PositiveIntegerField(
                    default=80,
                    help_text=(
                        'HTTP port of the terminal management API. '
                        '80 for LAN direct access; use the forwarded external port '
                        '(e.g. 8114, 8152) when routing via public IP.'
                    ),
                )),
                ('device_serial', models.CharField(blank=True, default='', max_length=100)),
                ('class_category_code', models.CharField(
                    blank=True,
                    default='',
                    help_text=(
                        'Optional. If set, only tokens for bookings in this class '
                        'category are pushed to this device. Leave blank for '
                        'universal access (Malad, Goregaon). Set to e.g. '
                        '"PILATES" or "BOOTCAMP" for Andheri.'
                    ),
                    max_length=100,
                )),
                ('is_active', models.BooleanField(
                    default=True,
                    help_text=(
                        'Deactivate to stop syncing tokens to this device '
                        'without deleting it.'
                    ),
                )),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'turnstile_devices',
                'ordering': ['branch', 'name'],
                'indexes': [
                    models.Index(fields=['branch', 'is_active'], name='idx_tdev_branch_active'),
                    models.Index(fields=['is_active', 'direction'], name='idx_tdev_active_dir'),
                ],
            },
        ),
    ]
