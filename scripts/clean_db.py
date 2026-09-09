import os
import sys
import django

# Ensure the backend root directory is on the Python path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.apps import apps

def clean_database():
    print("--- CLEANING DATABASE TO FRESH STATE ---")

    # List of models to keep populated (Global catalog / System RBAC)
    KEEP_MODELS = {'platformplan', 'permissiondefinition', 'roledefinition', 'rolepermission'}

    # Wipe business models
    for model in apps.get_models():
        model_name = model._meta.model_name
        app_label = model._meta.app_label
        
        if app_label.startswith('django') or app_label in ['contenttypes', 'sessions', 'admin', 'token_blacklist']:
            continue
        
        if model_name in KEEP_MODELS:
            if model_name == 'roledefinition':
                # Keep only built-in system roles
                model.objects.filter(is_system=False).delete()
            continue
        
        if model_name == 'user':
            # Keep only root admin
            model.objects.exclude(email='admin').delete()
            continue
        
        # Delete all records
        try:
            count, _ = model.objects.all().delete()
            if count > 0:
                print(f"Cleaned {count} records from {app_label}.{model.__name__}")
        except Exception as e:
            print(f"Note: Could not delete {model.__name__}: {e}")

    # Ensure root admin is clean with password 123
    from apps.users.models import User, RoleDefinition, PermissionDefinition
    from apps.tenants.models import Tenant

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

    print("\n>>> DATABASE SUCCESSFULLY CLEANED TO FRESH STARTING STATE! <<<")
    print(f"Active Users: {list(User.objects.values('id', 'email', 'role'))}")
    print(f"Active Tenants: {Tenant.objects.count()}")
    print(f"System Roles: {RoleDefinition.objects.count()}")
    print(f"Granular Permissions: {PermissionDefinition.objects.count()}")

if __name__ == '__main__':
    clean_database()
