"""
Sprint 10 Phase 10B — Commercial Billing Data Backfill
Derives billing_amount, currency, started_at, next_renewal_at strictly from authoritative relational data.
Zero hardcoded financial constants.
"""

from django.db import migrations


def backfill_commercial_billing_data(apps, schema_editor):
    SaasPlan = apps.get_model('master', 'SaasPlan')
    SaasPlanPrice = apps.get_model('master', 'SaasPlanPrice')
    SaasPlanModule = apps.get_model('master', 'SaasPlanModule')
    TenantSubscription = apps.get_model('master', 'TenantSubscription')
    TenantBillingMethod = apps.get_model('master', 'TenantBillingMethod')
    SubscriptionInvoice = apps.get_model('master', 'SubscriptionInvoice')
    SubscriptionDunningEvent = apps.get_model('master', 'SubscriptionDunningEvent')

    # 1. Backfill SaasPlan
    plans_updated = 0
    for plan in SaasPlan.objects.all():
        plan.status = 'ACTIVE' if plan.is_active else 'DRAFT'
        plan.is_custom = False
        plan.is_featured = False
        plan.save(update_fields=['status', 'is_custom', 'is_featured'])
        plans_updated += 1

    # 2. Backfill SaasPlanPrice
    prices_updated = 0
    for price in SaasPlanPrice.objects.all():
        if not price.effective_from:
            price.effective_from = price.created_at
        if not price.updated_at:
            price.updated_at = price.created_at
        price.status = 'ACTIVE' if price.is_active else 'INACTIVE'
        price.save(update_fields=['effective_from', 'updated_at', 'status'])
        prices_updated += 1

    # 3. Backfill SaasPlanModule
    modules_updated = 0
    for mod in SaasPlanModule.objects.all():
        if not mod.created_at and mod.plan and mod.plan.created_at:
            mod.created_at = mod.plan.created_at
            mod.save(update_fields=['created_at'])
            modules_updated += 1

    # 4. Authoritative relational backfill for TenantSubscription
    # Derives billing_amount and currency directly from linked SaasPlanPrice
    subscriptions_updated = 0
    unresolved_fks = 0
    for sub in TenantSubscription.objects.all():
        if sub.plan_price:
            price = sub.plan_price
            sub.billing_amount = price.amount
            sub.currency = price.currency
            sub.billing_cycle = price.billing_cycle or sub.billing_cycle
        else:
            unresolved_fks += 1

        if not sub.started_at:
            sub.started_at = sub.current_period_start or sub.created_at

        if not sub.current_period_start:
            sub.current_period_start = sub.started_at or sub.created_at

        if not sub.current_period_end:
            sub.current_period_end = sub.trial_ends_at or sub.created_at

        if not sub.next_renewal_at:
            sub.next_renewal_at = sub.trial_ends_at or sub.current_period_end

        sub.save(update_fields=[
            'billing_amount', 'currency', 'billing_cycle', 'started_at',
            'current_period_start', 'current_period_end', 'next_renewal_at'
        ])
        subscriptions_updated += 1

    # 5. Backfill TenantBillingMethod (0 rows expected, but idempotent logic)
    for bm in TenantBillingMethod.objects.all():
        if not bm.provider_customer_ref and bm.provider_customer_id:
            bm.provider_customer_ref = bm.provider_customer_id
        if not bm.provider_method_ref and bm.provider_method_id:
            bm.provider_method_ref = bm.provider_method_id
        bm.status = 'ACTIVE' if bm.is_active else 'INACTIVE'
        bm.save(update_fields=['provider_customer_ref', 'provider_method_ref', 'status'])

    # 6. Backfill SubscriptionInvoice (0 rows expected, but idempotent logic)
    for inv in SubscriptionInvoice.objects.all():
        if not inv.total_amount and inv.total:
            inv.total_amount = inv.total
        if not inv.due_at and inv.due_date:
            from datetime import datetime, time
            import django.utils.timezone
            inv.due_at = django.utils.timezone.make_aware(datetime.combine(inv.due_date, time.min))
        inv.save(update_fields=['total_amount', 'due_at'])

    # 7. Backfill SubscriptionDunningEvent (0 rows expected, but idempotent logic)
    for de in SubscriptionDunningEvent.objects.all():
        if not de.tenant and de.subscription:
            de.tenant = de.subscription.tenant
            de.save(update_fields=['tenant'])

    print(
        f"[Sprint 10 Migration 0016] Backfilled: plans={plans_updated}, prices={prices_updated}, "
        f"plan_modules={modules_updated}, subscriptions={subscriptions_updated} (unresolved FKs={unresolved_fks})"
    )


def rollback_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0015_sprint10_commercial_billing_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_commercial_billing_data, rollback_backfill),
    ]
