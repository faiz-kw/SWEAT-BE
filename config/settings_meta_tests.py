"""Isolated SQLite functional checks; never imports .env or production settings.

These tests do not certify PostgreSQL concurrency or existing SQL migrations.
Run only with --settings=config.settings_meta_tests.
"""
SECRET_KEY = 'isolated-meta-tests-not-a-deployment-key'
DEBUG = True
DEPLOYMENT_ENVIRONMENT = 'test'
META_LEAD_SIMULATOR_ENABLED = True
COMMUNICATIONS_OUTBOUND_ENABLED = False
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'rest_framework',
                  'apps.master', 'apps.tenant_core']
DATABASES = {name: {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}
             for name in ('default', 'tenant_test', 'tenant_other')}
DATABASE_ROUTERS = ['config.routers.MasterRouter', 'config.routers.TenantRouter']
AUTH_USER_MODEL = 'master.PlatformUser'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
ROOT_URLCONF = 'tests.meta_test_urls'
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [], 'UNAUTHENTICATED_USER': None}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
# Existing migrations contain PostgreSQL-specific SQL. Sync current models only.
MIGRATION_MODULES = {'master': None, 'tenant_core': None, 'auth': None, 'contenttypes': None}
