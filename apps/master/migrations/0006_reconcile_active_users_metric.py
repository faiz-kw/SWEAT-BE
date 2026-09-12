# Generated manually for Sprint 4 canonical ACTIVE_USERS metric reconciliation
from django.db import migrations


def reconcile_active_users_metric(apps, schema_editor):
    ResourceMetric = apps.get_model('master', 'ResourceMetric')
    SaasPlan = apps.get_model('master', 'SaasPlan')
    SaasPlanResourceLimit = apps.get_model('master', 'SaasPlanResourceLimit')
    TenantResourceLimit = apps.get_model('master', 'TenantResourceLimit')
    TenantResourceUsage = apps.get_model('master', 'TenantResourceUsage')
    TenantUsageAlert = apps.get_model('master', 'TenantUsageAlert')

    db_alias = schema_editor.connection.alias
    if db_alias != 'default':
        return

    # Check for existing TRAINERS and ACTIVE_USERS
    trainers_metric = ResourceMetric.objects.using(db_alias).filter(code='TRAINERS').first()
    active_users_metric = ResourceMetric.objects.using(db_alias).filter(code='ACTIVE_USERS').first()

    if trainers_metric and not active_users_metric:
        # Rename TRAINERS in place to preserve UUID PK and all FK relationships
        trainers_metric.code = 'ACTIVE_USERS'
        trainers_metric.name = 'Active Staff & Trainer Users'
        trainers_metric.unit = 'accounts'
        trainers_metric.description = 'Max active and invited staff/coach user accounts'
        trainers_metric.is_billable = True
        trainers_metric.is_active = True
        trainers_metric.save(using=db_alias)
        canonical_metric = trainers_metric
    elif trainers_metric and active_users_metric:
        # Re-point any FKs from TRAINERS to ACTIVE_USERS, then delete TRAINERS
        SaasPlanResourceLimit.objects.using(db_alias).filter(metric=trainers_metric).update(metric=active_users_metric)
        TenantResourceLimit.objects.using(db_alias).filter(metric=trainers_metric).update(metric=active_users_metric)
        TenantResourceUsage.objects.using(db_alias).filter(metric=trainers_metric).update(metric=active_users_metric)
        TenantUsageAlert.objects.using(db_alias).filter(metric=trainers_metric).update(metric=active_users_metric)
        trainers_metric.delete()
        canonical_metric = active_users_metric
    elif not trainers_metric and active_users_metric:
        canonical_metric = active_users_metric
    else:
        # Create ACTIVE_USERS from scratch
        canonical_metric = ResourceMetric.objects.using(db_alias).create(
            code='ACTIVE_USERS',
            name='Active Staff & Trainer Users',
            unit='accounts',
            description='Max active and invited staff/coach user accounts',
            is_billable=True,
            is_active=True,
        )

    # Ensure plan limits are set for all 3 SaaS plans
    plan_limits = {
        'PLAN-STARTER': 5,
        'PLAN-GROWTH': 20,
        'PLAN-ENTERPRISE': -1,
    }
    for plan_code, limit_val in plan_limits.items():
        plan = SaasPlan.objects.using(db_alias).filter(code=plan_code).first()
        if plan:
            SaasPlanResourceLimit.objects.using(db_alias).update_or_create(
                plan=plan,
                metric=canonical_metric,
                defaults={'limit_value': limit_val, 'soft_limit_pct': 80},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0005_seed_canonical_platform_permissions'),
    ]

    operations = [
        migrations.RunPython(reconcile_active_users_metric, migrations.RunPython.noop),
    ]
