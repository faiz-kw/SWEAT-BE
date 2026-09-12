"""
Sprint 10 Phase 10A — Commercial Billing Schema Expansion (Additive Only)
Expands canonical SaaS billing tables and creates new supporting billing architecture.
"""

from decimal import Decimal
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0014_alter_platformauditevent_resource_id_and_more'),
    ]

    operations = [
        # 1. saas_plans
        migrations.AlterField(
            model_name='saasplan',
            name='code',
            field=models.CharField(help_text='e.g. PLAN-STARTER', max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name='saasplan',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AddField(
            model_name='saasplan',
            name='status',
            field=models.CharField(choices=[('DRAFT', 'Draft'), ('ACTIVE', 'Active'), ('ARCHIVED', 'Archived')], default='DRAFT', max_length=30),
        ),
        migrations.AddField(
            model_name='saasplan',
            name='is_custom',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='saasplan',
            name='is_featured',
            field=models.BooleanField(default=False),
        ),

        # 2. saas_plan_prices
        migrations.AlterField(
            model_name='saasplanprice',
            name='billing_cycle',
            field=models.CharField(choices=[('MONTHLY', 'Monthly'), ('ANNUAL', 'Annual'), ('QUARTERLY', 'Quarterly')], max_length=30),
        ),
        migrations.AlterField(
            model_name='saasplanprice',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name='saasplanprice',
            name='plan',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='prices', to='master.saasplan'),
        ),
        migrations.AddField(
            model_name='saasplanprice',
            name='effective_from',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
        migrations.AddField(
            model_name='saasplanprice',
            name='effective_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='saasplanprice',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('ARCHIVED', 'Archived')], default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='saasplanprice',
            name='updated_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 3. saas_plan_modules
        migrations.AlterField(
            model_name='saasplanmodule',
            name='plan',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='plan_modules', to='master.saasplan'),
        ),
        migrations.AlterField(
            model_name='saasplanmodule',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='plan_inclusions', to='master.productmodule'),
        ),
        migrations.AddField(
            model_name='saasplanmodule',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 4. tenant_subscriptions
        migrations.AlterField(
            model_name='tenantsubscription',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='subscriptions', to='master.tenant'),
        ),
        migrations.AlterField(
            model_name='tenantsubscription',
            name='plan',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='subscriptions', to='master.saasplan'),
        ),
        migrations.AlterField(
            model_name='tenantsubscription',
            name='plan_price',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, to='master.saasplanprice'),
        ),
        migrations.AlterField(
            model_name='tenantsubscription',
            name='billing_cycle',
            field=models.CharField(choices=[('MONTHLY', 'Monthly'), ('ANNUAL', 'Annual'), ('QUARTERLY', 'Quarterly')], default='MONTHLY', max_length=30),
        ),
        migrations.AlterField(
            model_name='tenantsubscription',
            name='status',
            field=models.CharField(choices=[('TRIALING', 'Trialing'), ('ACTIVE', 'Active'), ('PAST_DUE', 'Past Due'), ('PAUSED', 'Paused'), ('SUSPENDED', 'Suspended'), ('CANCELED', 'Canceled'), ('EXPIRED', 'Expired')], default='TRIALING', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantsubscription',
            name='currency',
            field=models.CharField(default='INR', max_length=10),
        ),
        migrations.AddField(
            model_name='tenantsubscription',
            name='billing_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14),
        ),
        migrations.AddField(
            model_name='tenantsubscription',
            name='started_at',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
        migrations.AddField(
            model_name='tenantsubscription',
            name='next_renewal_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RenameField(
            model_name='tenantsubscription',
            old_name='canceled_at',
            new_name='cancelled_at',
        ),
        migrations.AddField(
            model_name='tenantsubscription',
            name='autopay_enabled',
            field=models.BooleanField(default=False),
        ),

        # 5. tenant_billing_methods
        migrations.AlterField(
            model_name='tenantbillingmethod',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='billing_methods', to='master.tenant'),
        ),
        migrations.AlterField(
            model_name='tenantbillingmethod',
            name='method_type',
            field=models.CharField(choices=[('RAZORPAY_MANDATE', 'Razorpay Auto-Pay Mandate'), ('MANDATE', 'Auto-Pay Mandate'), ('BANK_TRANSFER', 'Bank Transfer / NEFT'), ('CARD', 'Credit/Debit Card'), ('CASH', 'Cash'), ('CUSTOM', 'Custom')], max_length=50),
        ),
        migrations.AddField(
            model_name='tenantbillingmethod',
            name='provider_customer_ref',
            field=models.CharField(blank=True, default='', max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='tenantbillingmethod',
            name='provider_method_ref',
            field=models.CharField(blank=True, default='', max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='tenantbillingmethod',
            name='mandate_reference',
            field=models.CharField(blank=True, default='', max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='tenantbillingmethod',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),

        # 6. subscription_invoices
        migrations.AlterField(
            model_name='subscriptioninvoice',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='subscription_invoices', to='master.tenant'),
        ),
        migrations.AlterField(
            model_name='subscriptioninvoice',
            name='subscription',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='invoices', to='master.tenantsubscription'),
        ),
        migrations.AlterField(
            model_name='subscriptioninvoice',
            name='status',
            field=models.CharField(choices=[('DRAFT', 'Draft'), ('ISSUED', 'Issued'), ('OPEN', 'Open'), ('PAID', 'Paid'), ('VOID', 'Void'), ('UNCOLLECTIBLE', 'Uncollectible')], default='DRAFT', max_length=30),
        ),
        migrations.AddField(
            model_name='subscriptioninvoice',
            name='total_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14),
        ),
        migrations.AddField(
            model_name='subscriptioninvoice',
            name='due_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscriptioninvoice',
            name='issued_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscriptioninvoice',
            name='invoice_file_key',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.RemoveField(
            model_name='subscriptioninvoice',
            name='pdf_url',
        ),

        # 7. subscription_payments
        migrations.AlterField(
            model_name='subscriptionpayment',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, to='master.tenant'),
        ),
        migrations.AlterField(
            model_name='subscriptionpayment',
            name='invoice',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='payments', to='master.subscriptioninvoice'),
        ),
        migrations.AlterField(
            model_name='subscriptionpayment',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('PROCESSING', 'Processing'), ('SUCCEEDED', 'Succeeded'), ('FAILED', 'Failed'), ('REFUNDED', 'Refunded')], default='PENDING', max_length=30),
        ),

        # 8. subscription_dunning_events
        migrations.AlterField(
            model_name='subscriptiondunningevent',
            name='subscription',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='dunning_events', to='master.tenantsubscription'),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='tenant',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='dunning_events', to='master.tenant'),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='invoice',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='dunning_events', to='master.subscriptioninvoice'),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='scheduled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='executed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='result',
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='subscriptiondunningevent',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),

        # 9. New Supporting Tables (Outside 751 tally)
        migrations.CreateModel(
            name='SubscriptionInvoiceItem',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('item_type', models.CharField(choices=[('BASE_PLAN', 'Base Plan Subscription'), ('ADDON', 'Addon / Extra Module'), ('USAGE_CHARGE', 'Metered Usage Charge'), ('DISCOUNT', 'Discount / Credit'), ('ADJUSTMENT', 'Manual Adjustment')], default='BASE_PLAN', max_length=30)),
                ('description', models.CharField(max_length=255)),
                ('quantity', models.IntegerField(default=1)),
                ('unit_price', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('subtotal', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('tax_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('tax_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('total_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='master.subscriptioninvoice')),
            ],
            options={
                'db_table': 'subscription_invoice_items',
                'ordering': ['created_at'],
            },
        ),
        migrations.CreateModel(
            name='BillingWebhookEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(max_length=50)),
                ('provider_event_id', models.CharField(max_length=255)),
                ('event_type', models.CharField(max_length=100)),
                ('status', models.CharField(choices=[('RECEIVED', 'Received'), ('PROCESSING', 'Processing'), ('PROCESSED', 'Processed'), ('FAILED', 'Failed'), ('IGNORED', 'Ignored')], default='RECEIVED', max_length=20)),
                ('signature_verified', models.BooleanField(default=False)),
                ('retry_count', models.IntegerField(default=0)),
                ('payload', models.JSONField(default=dict)),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True, default='')),
            ],
            options={
                'db_table': 'billing_webhook_events',
                'indexes': [
                    models.Index(fields=['provider', 'status'], name='billing_web_provide_1f6213_idx'),
                    models.Index(fields=['received_at'], name='billing_web_receive_8cf68d_idx'),
                ],
                'unique_together': {('provider', 'provider_event_id')},
            },
        ),
        migrations.CreateModel(
            name='TenantInvoiceSequence',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('year', models.IntegerField()),
                ('last_sequence', models.IntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='invoice_sequences', to='master.tenant')),
            ],
            options={
                'db_table': 'tenant_invoice_sequences',
                'unique_together': {('tenant', 'year')},
            },
        ),
    ]
