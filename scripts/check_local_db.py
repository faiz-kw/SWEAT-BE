import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.master.models_tenant import Tenant
from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.context import tenant_database_context

tenants = Tenant.objects.using('default').all()
print(f"Local Tenants count: {tenants.count()}")
for t in tenants:
    print(f"Tenant: {t.name} (slug: {t.slug}, id: {t.id})")
    with tenant_database_context(str(t.id)):
        branches = Branch.objects.all()
        print(f"  Branches count: {branches.count()}")
        for b in branches:
            print(f"    Branch: {b.name} (id: {b.id}, code: {b.code}, status: {b.status})")
        classes = ClassTemplate.objects.all()
        print(f"  Class Templates count: {classes.count()}")
        for c in classes:
            print(f"    Class: {c.name} (code: {c.code}, allow_trial: {c.allow_trial}, allow_waitlist: {c.allow_waitlist})")
        occurrences = ClassOccurrence.objects.all()
        print(f"  Class Occurrences count: {occurrences.count()}")
