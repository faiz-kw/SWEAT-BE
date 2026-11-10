# Generated migration: Multi-account Meta Lead Ads — sync health fields + connection FK
# - Adds last_lead_received_at, last_reconcile_attempted_at, last_reconcile_leads_found
#   to MetaPageConnection for per-page sync health tracking.
# - Adds nullable connection FK to MetaLeadMapping so mappings can be traced to their
#   owning Facebook account without breaking existing records (SET_NULL on delete).
# All changes are purely additive — no existing data is altered or removed.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0080_cmsbanner_cmsmediaasset_programcategorymedia'),
    ]

    operations = [
        # --- MetaPageConnection: add sync-health tracking fields ---
        migrations.AddField(
            model_name='metapageconnection',
            name='last_lead_received_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Timestamp of the most recently received lead event for this Page.',
            ),
        ),
        migrations.AddField(
            model_name='metapageconnection',
            name='last_reconcile_attempted_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Last time the scheduled reconciliation task ran for this Page.',
            ),
        ),
        migrations.AddField(
            model_name='metapageconnection',
            name='last_reconcile_leads_found',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Number of leads backfilled during the most recent reconciliation run.',
            ),
        ),
        # --- MetaLeadMapping: add nullable FK to owning MetaConnection ---
        migrations.AddField(
            model_name='metaleadmapping',
            name='connection',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='mappings',
                to='tenant_core.metaconnection',
                help_text='The Facebook account connection that owns this Page/Form mapping.',
            ),
        ),
    ]

