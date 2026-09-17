import os, sys, uuid
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.argv = ['manage.py', 'test']
import django
django.setup()
from django.db import connections
from django.test.runner import DiscoverRunner
suffix = uuid.uuid4().hex[:10]
for alias in connections:
    connections[alias].settings_dict['TEST']['NAME'] = 'test_login_' + alias + '_' + suffix
raise SystemExit(DiscoverRunner(verbosity=1, interactive=False).run_tests(['tests.test_organization_login']))
