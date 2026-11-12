"""
Clean up the ClassScheduleRule table:
  - delete test rules (class template name contains 'Test'),
  - collapse exact-duplicate rules to a single canonical row,
  - repoint any occurrences of a removed rule to the kept rule (schedule_rule is SET_NULL
    on delete, so this just preserves lineage).

Dry-run by default. Pass --commit to actually write. Backs up removed rule ids to JSON.

    python manage.py cleanup_schedule_rules            # preview
    python manage.py cleanup_schedule_rules --commit    # apply
"""
import json
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "De-duplicate and de-junk ClassScheduleRule rows (dry-run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Apply changes (default: dry-run).')
        parser.add_argument('--drop-test', action='store_true', default=True,
                            help="Also delete rules whose template name contains 'Test' (default on).")

    def handle(self, *args, **opts):
        from apps.master.models_tenant import Tenant
        from apps.authentication.views import _register_and_resolve_tenant
        from apps.tenant_core.models_classes import ClassScheduleRule, ClassOccurrence

        commit = opts['commit']
        drop_test = opts['drop_test']
        mode = 'COMMIT' if commit else 'DRY-RUN'

        for t in Tenant.objects.using('default').filter(status='ACTIVE'):
            alias = _register_and_resolve_tenant(t)
            if not alias:
                continue
            rules = list(ClassScheduleRule.objects.using(alias).select_related('class_template', 'branch'))
            if not rules:
                continue

            to_delete = []        # (rule, keeper_or_None, reason)
            # 1) test rules
            if drop_test:
                for r in rules:
                    nm = (r.class_template.name if r.class_template else '') or ''
                    if 'test' in nm.lower():
                        to_delete.append((r, None, 'test rule'))

            test_ids = {r.id for r, _, _ in to_delete}
            # 2) exact-duplicate signatures among the remaining rules
            groups = defaultdict(list)
            for r in rules:
                if r.id in test_ids:
                    continue
                sig = (r.branch_id, r.class_template_id, str(r.start_time), str(r.end_time),
                       r.recurrence_type, json.dumps(r.days_of_week, sort_keys=True), r.status)
                groups[sig].append(r)
            for sig, grp in groups.items():
                if len(grp) < 2:
                    continue
                grp.sort(key=lambda r: (r.created_at, str(r.id)))
                keeper = grp[0]
                for r in grp[1:]:
                    to_delete.append((r, keeper, 'duplicate of %s' % str(keeper.id)[:8]))

            self.stdout.write("=== %s | tenant=%s | rules=%d | to remove=%d (%s) ===" % (
                mode, t.slug, len(rules), len(to_delete), 'test+dupes'))
            if not to_delete:
                continue

            backup = [{'rule_id': str(r.id), 'template': (r.class_template.name if r.class_template else None),
                       'branch': (r.branch.name if r.branch else None), 'start_time': str(r.start_time),
                       'reason': reason, 'keeper': (str(k.id) if k else None)}
                      for r, k, reason in to_delete]

            if not commit:
                from collections import Counter
                c = Counter(reason.split(' of ')[0] for _, _, reason in to_delete)
                for k, n in c.items():
                    self.stdout.write("    %s: %d" % (k, n))
                self.stdout.write("    [DRY-RUN] no changes written.")
                continue

            with transaction.atomic(using=alias):
                for r, keeper, reason in to_delete:
                    if keeper is not None:
                        ClassOccurrence.objects.using(alias).filter(schedule_rule=r).update(schedule_rule=keeper)
                    else:
                        ClassOccurrence.objects.using(alias).filter(schedule_rule=r).update(schedule_rule=None)
                    r.delete(using=alias)
            bpath = "/tmp/schedule_rules_cleanup_%s_%s.json" % (t.slug, timezone.now().strftime("%Y%m%d-%H%M%S"))
            open(bpath, 'w').write(json.dumps(backup, indent=2))
            self.stdout.write("    BACKUP: %s" % bpath)
            self.stdout.write(self.style.SUCCESS("    DELETED %d rules." % len(to_delete)))
