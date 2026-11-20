"""
Management command to backfill and repair multi-scope configurations for DiscountCampaign,
DiscountEligibilityRule, and DiscountCode across tenant databases.

Usage:
    python manage.py fix_multiscope_discounts
    python manage.py fix_multiscope_discounts --tenant=sweat
"""

import json
import logging
import re
from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Repairs and backfills multi-package and multi-branch scope configurations.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            help='Slug of specific tenant to process',
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        qs = Tenant.objects.using('default').filter(status='ACTIVE')
        if tenant_slug:
            qs = qs.filter(slug=tenant_slug)

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.WARNING("No active tenants found."))
            return

        for tenant in tenants:
            data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
            if not data_source or not data_source.db_name:
                continue

            db_name = data_source.database_name or data_source.db_name
            db_alias = build_tenant_db_alias(tenant.id)
            _register_tenant_connection(db_alias, db_name, data_source=data_source, tenant_id=tenant.id)
            set_tenant_db_alias(db_alias)

            self.stdout.write(self.style.SUCCESS(f"Processing tenant: {tenant.name} ({tenant.slug}) on {db_alias}"))

            from apps.tenant_core.models_discounts import DiscountCampaign, DiscountEligibilityRule, DiscountCode

            # 1. Fix Campaigns
            for camp in DiscountCampaign.objects.using(db_alias).all():
                cfg = dict(camp.configuration or {})
                changed = False
                pkg_ids = cfg.get('package_ids')
                if isinstance(pkg_ids, list) and len(pkg_ids) > 1:
                    if cfg.get('package_id') is not None:
                        cfg['package_id'] = None
                        changed = True

                branch_ids = cfg.get('branch_ids')
                if isinstance(branch_ids, list) and len(branch_ids) > 1:
                    if cfg.get('branch_id') is not None:
                        cfg['branch_id'] = None
                        changed = True

                if changed:
                    camp.configuration = cfg
                    camp.save(using=db_alias, update_fields=['configuration'])
                    self.stdout.write(f"  [Fixed Campaign Scope] {camp.name}")

            # 2. Fix Rules
            for rule in DiscountEligibilityRule.objects.using(db_alias).all():
                cfg = dict(rule.configuration or {})
                changed = False

                # Backfill from legacy description SCOPE comment if cfg is empty
                if not cfg.get('package_ids') and rule.description and '<!-- SCOPE:' in rule.description:
                    m = re.search(r'<!-- SCOPE:\s*(\{.*?\})\s*-->', rule.description)
                    if m:
                        try:
                            sdata = json.loads(m.group(1))
                            if sdata.get('package_ids'):
                                cfg['package_ids'] = sdata['package_ids']
                                changed = True
                            if sdata.get('program_ids'):
                                cfg['program_ids'] = sdata['program_ids']
                                changed = True
                            if sdata.get('branch_ids'):
                                cfg['branch_ids'] = sdata['branch_ids']
                                changed = True
                        except Exception:
                            pass

                update_fields = []
                pkg_ids = cfg.get('package_ids')
                if isinstance(pkg_ids, list) and len(pkg_ids) > 1:
                    if cfg.get('package_id') is not None:
                        cfg['package_id'] = None
                        changed = True
                    if rule.target_package_id is not None:
                        rule.target_package = None
                        update_fields.append('target_package')

                branch_ids = cfg.get('branch_ids')
                if isinstance(branch_ids, list) and len(branch_ids) > 1:
                    if cfg.get('branch_id') is not None:
                        cfg['branch_id'] = None
                        changed = True
                    if rule.branch_id is not None:
                        rule.branch = None
                        update_fields.append('branch')

                if changed:
                    rule.configuration = cfg
                    update_fields.append('configuration')

                if update_fields:
                    rule.save(using=db_alias, update_fields=update_fields)
                    self.stdout.write(f"  [Fixed Rule Scope] {rule.name}")

            # 3. Fix Codes
            for code in DiscountCode.objects.using(db_alias).select_related('campaign').all():
                if code.campaign and code.package_id:
                    camp_cfg = code.campaign.configuration or {}
                    camp_pkg_ids = camp_cfg.get('package_ids') or []
                    # If campaign targets multiple packages, clear code-level package locking
                    if len(camp_pkg_ids) > 1 and str(code.package_id) in [str(x) for x in camp_pkg_ids]:
                        code.package = None
                        code.save(using=db_alias, update_fields=['package'])
                        self.stdout.write(f"  [Unlocked Multi-Package Code] {code.code} for campaign {code.campaign.name}")

        self.stdout.write(self.style.SUCCESS("Multi-scope discount data repair completed successfully."))
