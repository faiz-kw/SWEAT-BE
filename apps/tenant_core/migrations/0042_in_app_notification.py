import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0041_merge_20260924_1205'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notificationtemplate',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('MEMBERSHIP_WELCOME', 'Membership Welcome'),
                    ('MEMBERSHIP_EXPIRY', 'Membership Expiry Reminder'),
                    ('MEMBERSHIP_RENEWAL', 'Membership Renewal Confirmation'),
                    ('BOOKING_CONFIRMATION', 'Booking Confirmation'),
                    ('BOOKING_REMINDER', 'Booking Reminder'),
                    ('BOOKING_CANCELLATION', 'Booking Cancellation'),
                    ('PAYMENT_RECEIVED', 'Payment Received'),
                    ('PAYMENT_FAILED', 'Payment Failed'),
                    ('LEAD_WELCOME', 'Lead Welcome'),
                    ('LEAD_ASSIGNED', 'Lead Assigned to Agent'),
                    ('TRIAL_SCHEDULED', 'Trial Scheduled'),
                    ('CUSTOM', 'Custom'),
                ],
                max_length=50,
            ),
        ),
        migrations.CreateModel(
            name='InAppNotification',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('notification_type', models.CharField(default='LEAD_ASSIGNED', max_length=50)),
                ('title', models.CharField(max_length=200)),
                ('message', models.TextField()),
                ('data', models.JSONField(blank=True, default=dict)),
                ('deep_link', models.CharField(blank=True, default='', max_length=255)),
                ('is_read', models.BooleanField(default=False)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('idempotency_key', models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('organization', models.ForeignKey(
                    db_column='organization_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='in_app_notifications',
                    to='tenant_core.organization',
                )),
                ('user', models.ForeignKey(
                    db_column='user_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='in_app_notifications',
                    to='tenant_core.tenantuser',
                )),
            ],
            options={
                'verbose_name': 'In-App Notification',
                'verbose_name_plural': 'In-App Notifications',
                'db_table': 'in_app_notifications',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='inappnotification',
            index=models.Index(fields=['user', 'is_read', '-created_at'], name='idx_notif_user_read'),
        ),
        migrations.AddIndex(
            model_name='inappnotification',
            index=models.Index(fields=['organization', '-created_at'], name='idx_notif_org_created'),
        ),
        migrations.AddIndex(
            model_name='inappnotification',
            index=models.Index(fields=['idempotency_key'], name='idx_notif_idem_key'),
        ),
    ]
