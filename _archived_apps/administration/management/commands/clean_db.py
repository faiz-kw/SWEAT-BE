"""
Management command to clean database to fresh starting state.
Usage: python manage.py clean_db
"""

from django.core.management.base import BaseCommand
from django.apps import apps
from apps.users.models import User, RoleDefinition, PermissionDefinition
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = 'Cleans the database to a fresh state while preserving root admin and system definitions.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.WARNING("--- CLEANING DATABASE TO FRESH STATE ---"))

        # Models to keep populated (Global catalog / System RBAC)
        KEEP_MODELS = {'platformplan', 'permissiondefinition', 'roledefinition', 'rolepermission'}

        for model in apps.get_models():
            model_name = model._meta.model_name
            app_label = model._meta.app_label

            if app_label.startswith('django') or app_label in ['contenttypes', 'sessions', 'admin', 'token_blacklist']:
                continue

            if model_name in KEEP_MODELS:
                if model_name == 'roledefinition':
                    model.objects.filter(is_system=False).delete()
                continue

            if model_name == 'user':
                model.objects.exclude(email='admin').delete()
                continue

            try:
                count, _ = model.objects.all().delete()
                if count > 0:
                    self.stdout.write(f"Cleaned {count} records from {app_label}.{model.__name__}")
            except Exception as e:
                self.stdout.write(self.style.NOTICE(f"Note: Could not delete {model.__name__}: {e}"))

        # Ensure root admin user
        admin_role = RoleDefinition.objects.filter(code='super_admin').first()
        admin_user, _ = User.objects.get_or_create(id='USR-ADMIN', defaults={'email': 'admin'})
        admin_user.email = 'admin'
        admin_user.first_name = 'Super'
        admin_user.last_name = 'Admin'
        admin_user.role = 'Super Admin'
        admin_user.role_definition = admin_role
        admin_user.tenant = None
        admin_user.is_superuser = True
        admin_user.is_staff = True
        admin_user.set_password('123')
        admin_user.save()

        self.stdout.write(self.style.SUCCESS("\n>>> DATABASE SUCCESSFULLY CLEANED TO FRESH STARTING STATE! <<<"))
        self.stdout.write(f"Active Users: {list(User.objects.values('id', 'email', 'role'))}")
        self.stdout.write(f"Active Tenants: {Tenant.objects.count()}")
        self.stdout.write(f"System Roles: {RoleDefinition.objects.count()}")
        self.stdout.write(f"Granular Permissions: {PermissionDefinition.objects.count()}")
