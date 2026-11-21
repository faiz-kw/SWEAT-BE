import os, sys
backend_dir = r'F:\fitness-command-center\backend'
sys.path.insert(0, backend_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()
from apps.master.models_tenant import Tenant
for t in Tenant.objects.all():
    print(t.id, t.name, t.slug, [d.domain for d in t.domains.all()])
