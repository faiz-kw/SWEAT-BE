"""
Grants the assessment permissions to the roles that run assessments.

The assessment viewset is deliberately not enforcing TenantRBACPermission yet, because the
permission codes exist in rbac_defaults but are not attached to any role in a live tenant.
Switching the engine on before that would deny every trainer and front-desk user and leave
only org admins, who pass through the super-admin bypass.

This command closes that gap. Run it per tenant, confirm staff can still work, then enable
the engine on the viewset — the per-action map is already written there.

    python manage.py provision_assessment_rbac --tenant=sweat
    python manage.py provision_assessment_rbac --tenant=sweat --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Who does what with an assessment. A trainer runs and records them; a front-desk user books
# and looks them up but does not approve; approval is a coaching judgement, so it sits with
# branch managers and org admins.
ROLE_GRANTS = {
    'ORG_ADMIN': ['view', 'create', 'edit', 'approve'],
    'BRANCH_MANAGER': ['view', 'create', 'edit', 'approve'],
    'TRAINER': ['view', 'create', 'edit'],
    'FRONT_DESK': ['view', 'create'],
    'SALES_REP': ['view'],
}

PERMISSION_CODES = {
    'view': 'performance.assessments.view',
    'create': 'performance.assessments.create',
    'edit': 'performance.assessments.edit',
    'approve': 'performance.assessments.approve',
}


class Command(BaseCommand):
    help = 'Grants assessment permissions to the roles that need them, for one tenant.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', required=True, help='Tenant slug, e.g. sweat')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing anything')

    def handle(self, *args, **options):
        tenant_slug = options['tenant']
        dry_run = options['dry_run']

        from apps.master.models_tenant import Tenant
        from apps.tenant_core.context import tenant_database_context
        from config.routers import get_tenant_db_alias

        tenant = Tenant.objects.using('default').filter(slug=tenant_slug).first()
        if not tenant:
            raise CommandError(f"No tenant with slug '{tenant_slug}'.")

        # Registers and tears down the tenant connection, rather than only setting the alias.
        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            self._grant(alias, dry_run)

    def _grant(self, alias, dry_run):

        # Roles do not hold permissions directly: each role owns one or more permission sets,
        # and the set holds the items. Grants are added to the role's default set.
        from apps.tenant_core.models_rbac import (
            Permission, Role, RolePermissionSet, RolePermissionSetItem,
        )

        permissions = {
            code: Permission.objects.using(alias).filter(code=code).first()
            for code in PERMISSION_CODES.values()
        }
        missing = [code for code, obj in permissions.items() if obj is None]
        if missing:
            self.stdout.write(self.style.WARNING(
                'These permission codes are not in the tenant database yet:\n  '
                + '\n  '.join(missing)
                + '\nRun the tenant catalogue/RBAC sync first, then re-run this command.'
            ))

        granted = skipped = 0
        for role_code, actions in ROLE_GRANTS.items():
            role = Role.objects.using(alias).filter(code=role_code).first()
            if not role:
                self.stdout.write(f'  - {role_code}: no such role in this tenant, skipped')
                continue

            permission_set = (
                RolePermissionSet.objects.using(alias)
                .filter(role=role).order_by('created_at', 'id').first()
            )
            if not permission_set:
                self.stdout.write(
                    f'  - {role_code}: no permission set on this role, skipped')
                continue

            for action in actions:
                permission = permissions.get(PERMISSION_CODES[action])
                if not permission:
                    continue
                exists = RolePermissionSetItem.objects.using(alias).filter(
                    permission_set=permission_set, permission=permission).exists()
                if exists:
                    skipped += 1
                    continue
                if dry_run:
                    self.stdout.write(f'  + {role_code} would gain {permission.code}')
                else:
                    with transaction.atomic(using=alias):
                        RolePermissionSetItem.objects.using(alias).create(
                            permission_set=permission_set, permission=permission)
                    self.stdout.write(f'  + {role_code} granted {permission.code}')
                granted += 1

        verb = 'would be granted' if dry_run else 'granted'
        self.stdout.write(self.style.SUCCESS(
            f'\n{granted} permission(s) {verb}, {skipped} already in place.'
        ))
        if not dry_run and granted:
            self.stdout.write(
                'Now confirm a trainer and a front-desk user can still open and save an '
                'assessment, then switch AssessmentSessionViewSet.permission_classes to '
                '[RequireActiveTenantAndOrg, TenantRBACPermission].'
            )
