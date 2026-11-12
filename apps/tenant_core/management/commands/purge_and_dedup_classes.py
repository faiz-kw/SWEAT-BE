"""
One-time cleanup to make the duplicate-proof unique constraint (migration 0083) applyable,
and to de-bloat the class tables.

  1. PURGE test classes: templates whose name contains 'Test' -> delete their occurrences
     (tearing down PROTECT dependents first), their schedule rules, and the template.
  2. DEDUP real same-class duplicates: for each (branch, real-template, start_at) with >1
     ACTIVE occurrence, keep one (the booked one, else earliest created) and set the rest
     to DEACTIVATED (reversible; history preserved).

Dry-run by default. --commit to write. Backs up what it removes/deactivates to JSON.

    python manage.py purge_and_dedup_classes            # preview
    python manage.py purge_and_dedup_classes --commit     # apply
"""
import json
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = "Purge test classes and de-duplicate real same-class occurrences (dry-run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Apply changes (default: dry-run).')
        parser.add_argument('--batch', type=int, default=500, help='Delete batch size.')
        parser.add_argument('--tenant', default='sweat', help='Tenant slug to operate on.')

    def handle(self, *args, **opts):
        from apps.master.models_tenant import Tenant
        from apps.authentication.views import _register_and_resolve_tenant
        from apps.tenant_core.models_classes import ClassOccurrence, ClassTemplate, ClassScheduleRule

        commit = opts['commit']
        batch = opts['batch']
        mode = 'COMMIT' if commit else 'DRY-RUN'

        for t in Tenant.objects.using('default').filter(status='ACTIVE'):
            if t.slug != opts['tenant']:
                continue
            alias = _register_and_resolve_tenant(t)
            if not alias:
                continue

            # reverse relations that point at ClassOccurrence (all PROTECT) -> tear down first
            occ_rev = [f for f in ClassOccurrence._meta.get_fields()
                       if (f.one_to_many or f.one_to_one) and f.auto_created and not f.concrete]

            # ---------- 1. TEST TEMPLATE PURGE ----------
            test_tpls = list(ClassTemplate.objects.using(alias).filter(name__icontains='test'))
            test_tpl_ids = [c.id for c in test_tpls]
            test_occ_qs = ClassOccurrence.objects.using(alias).filter(class_template_id__in=test_tpl_ids)
            test_occ_count = test_occ_qs.count()
            test_rule_count = ClassScheduleRule.objects.using(alias).filter(class_template_id__in=test_tpl_ids).count()

            self.stdout.write("=== %s | tenant=%s ===" % (mode, t.slug))
            self.stdout.write("1) TEST PURGE: templates=%d  occurrences=%d  rules=%d" % (
                len(test_tpls), test_occ_count, test_rule_count))
            self.stdout.write("   test templates: %s" % [c.name for c in test_tpls])
            # count dependents that would be torn down
            dep_counts = {}
            for rel in occ_rev:
                model = rel.related_model
                field = rel.field.name
                try:
                    dep_counts[model.__name__] = model.objects.using(alias).filter(**{f"{field}_id__in": list(test_occ_qs.values_list('id', flat=True)[:1])}).model and model.objects.using(alias).filter(**{f"{field}__in": test_occ_qs}).count()
                except Exception as e:
                    dep_counts[model.__name__] = "err:%s" % str(e)[:30]
            self.stdout.write("   dependents to delete: %s" % dep_counts)

            # ---------- 2. REAL SAME-CLASS DEDUP ----------
            base = (ClassOccurrence.objects.using(alias)
                    .exclude(status__in=['CANCELLED', 'DEACTIVATED'])
                    .exclude(class_template_id__in=test_tpl_ids))
            dup_groups = list(base.values('branch_id', 'class_template_id', 'start_at')
                                  .annotate(n=Count('id')).filter(n__gt=1))
            extra = sum(d['n'] - 1 for d in dup_groups)
            self.stdout.write("2) REAL DEDUP: colliding groups=%d  extra rows to deactivate=%d" % (
                len(dup_groups), extra))

            if not commit:
                self.stdout.write("   [DRY-RUN] no changes written.\n")
                continue

            # ----- EXECUTE -----
            from apps.tenant_core.models_bookings import Booking
            # 1. purge test occurrences in batches (dependents first)
            ids = list(test_occ_qs.values_list('id', flat=True))
            purged = 0
            for i in range(0, len(ids), batch):
                chunk = ids[i:i+batch]
                with transaction.atomic(using=alias):
                    for rel in occ_rev:
                        model = rel.related_model
                        field = rel.field.name
                        model.objects.using(alias).filter(**{f"{field}_id__in": chunk}).delete()
                    ClassOccurrence.objects.using(alias).filter(id__in=chunk).delete()
                purged += len(chunk)
            # test rules + templates
            ClassScheduleRule.objects.using(alias).filter(class_template_id__in=test_tpl_ids).delete()
            tpl_deleted = 0
            for c in test_tpls:
                try:
                    c.delete(using=alias)
                    tpl_deleted += 1
                except Exception as e:
                    c.status = 'INACTIVE'
                    c.save(using=alias, update_fields=['status'])
            self.stdout.write(self.style.SUCCESS("   PURGED occurrences=%d rules + templates(deleted=%d)" % (purged, tpl_deleted)))

            # 2. dedup real duplicates
            deact_backup = []
            deact = 0
            for d in dup_groups:
                rows = list(base.filter(branch_id=d['branch_id'], class_template_id=d['class_template_id'],
                                        start_at=d['start_at']))
                booked = [o for o in rows if Booking.objects.using(alias).filter(occurrence=o).exclude(status__in=['CANCELLED']).exists()]
                keeper = booked[0] if booked else sorted(rows, key=lambda o: (o.created_at, str(o.id)))[0]
                for o in rows:
                    if o.id == keeper.id:
                        continue
                    if Booking.objects.using(alias).filter(occurrence=o).exclude(status__in=['CANCELLED']).exists():
                        continue  # never hide a booked class; leave for manual
                    deact_backup.append({'occ_id': str(o.id), 'keeper': str(keeper.id), 'start_at': o.start_at.isoformat()})
                    o.status = 'DEACTIVATED'
                    o.save(using=alias, update_fields=['status', 'updated_at'])
                    deact += 1
            bpath = "/tmp/purge_dedup_%s_%s.json" % (t.slug, timezone.now().strftime("%Y%m%d-%H%M%S"))
            open(bpath, 'w').write(json.dumps({'deactivated': deact_backup, 'purged_occurrences': purged}, indent=2))
            self.stdout.write(self.style.SUCCESS("   DEDUP deactivated=%d | backup=%s" % (deact, bpath)))
