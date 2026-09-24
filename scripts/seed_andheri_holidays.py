"""Seed sample closures for the demo Andheri branch; preserve existing dates.

Run: python manage.py shell -c "exec(open('scripts/seed_andheri_holidays.py').read())"
"""
from datetime import date

from django.db import transaction

from apps.authentication.views import _register_and_resolve_tenant
from apps.master.models import Tenant
from apps.tenant_core.audit import emit_audit_event, snapshot_model_state
from apps.tenant_core.models_govern import BranchOperatingException
from apps.tenant_core.models_org import Branch


tenant = Tenant.objects.using('default').get(slug='sweat-demo', status='ACTIVE')
db = _register_and_resolve_tenant(tenant)
branch = Branch.objects.using(db).get(
    pk='8f6790e0-1a66-4106-bd46-7aa5ca769f58',
    name='Andheri Demo Branch',
)
holidays = [
    (date(2026, 10, 2), 'Gandhi Jayanti'),
    (date(2026, 12, 25), 'Christmas'),
    (date(2027, 1, 1), "New Year's Day"),
    (date(2027, 1, 26), 'Republic Day'),
]
created_count = 0
with transaction.atomic(using=db):
    for holiday_date, name in holidays:
        entry, created = BranchOperatingException.objects.using(db).get_or_create(
            branch=branch,
            exception_date=holiday_date,
            defaults={
                'is_closed': True,
                'open_time': None,
                'close_time': None,
                'reason': name + ' (sample holiday)',
            },
        )
        if created:
            emit_audit_event(
                action='CREATE',
                resource_type='BranchOperatingException',
                resource_id=str(entry.pk),
                actor_type='SYSTEM_JOB',
                instance=entry,
                before_state=None,
                after_state=snapshot_model_state(entry),
                db_alias=db,
            )
            created_count += 1

print(f'{branch.name}: created {created_count}; existing dates preserved.')
for entry in BranchOperatingException.objects.using(db).filter(
    branch=branch, exception_date__in=[day for day, _ in holidays],
).order_by('exception_date'):
    print(f'{entry.exception_date}: {entry.reason}; closed={entry.is_closed}')
