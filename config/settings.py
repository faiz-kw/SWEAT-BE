"""
Django settings for PerformanceOS — Phase 1 Layer 1 Foundation.
Multi-tenant Architecture:
  - Master/Control DB: platform IAM, tenant registry, SaaS billing, marketplace, provisioning
  - Dedicated Tenant DB: per-tenant org/branch hierarchy, RBAC, privacy, audit
"""

import os
import sys
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

# ---------------------------------------------------------------------------
# Security — Critical settings validated at startup
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv('SECRET_KEY', '')
_INSECURE_KEY_PREFIXES = ('django-insecure-', 'insecure-', 'dev-key', 'change-me')

# Validate SECRET_KEY in production
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 't')

if not DEBUG:
    if not SECRET_KEY:
        raise RuntimeError(
            'FATAL: SECRET_KEY environment variable is not set. '
            'Generate a strong key with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"'
        )
    if any(SECRET_KEY.startswith(p) for p in _INSECURE_KEY_PREFIXES):
        raise RuntimeError(
            'FATAL: SECRET_KEY appears to be an insecure development key. '
            'Set a strong, unique SECRET_KEY in production.'
        )
else:
    # In development: fall back to a dev default but warn loudly
    if not SECRET_KEY:
        SECRET_KEY = 'dev-only-insecure-key-do-not-use-in-production'
        import warnings
        warnings.warn(
            'WARNING: No SECRET_KEY set in .env — using dev fallback. '
            'This is NOT acceptable in production.',
            stacklevel=2,
        )

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
    if h.strip()
]

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'drf_spectacular',
]

LOCAL_APPS = [
    'apps.master',           # Master/Control plane (41 tables)
    'apps.tenant_core',      # Dedicated Tenant DB schema (28 tables)
    'apps.authentication',   # JWT auth for platform + tenant users
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Correlation ID propagation — must come before TenantDatabaseMiddleware
    'config.middleware.CorrelationIDMiddleware',
    # Tenant DB routing middleware — resolves active tenant from verified JWT
    'config.tenant_middleware.TenantDatabaseMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ---------------------------------------------------------------------------
# Databases — Master DB + Dynamic Tenant DB Support
# ---------------------------------------------------------------------------
_master_db_url = os.getenv('MASTER_DATABASE_URL', os.getenv('DATABASE_URL', ''))
if not _master_db_url:
    raise RuntimeError(
        'FATAL: MASTER_DATABASE_URL (or DATABASE_URL) environment variable is not set.'
    )

import urllib.parse as _urlparse
_parsed = _urlparse.urlparse(_master_db_url)

DATABASES = {
    # Master / Control Plane DB — platform IAM, tenant registry, billing
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': _parsed.path.lstrip('/') or 'fitness_master',
        'USER': _parsed.username or 'postgres',
        'PASSWORD': _parsed.password or '',
        'HOST': _parsed.hostname or 'localhost',
        'PORT': _parsed.port or 5432,
        'OPTIONS': {
            'connect_timeout': 10,
            'options': '-c statement_timeout=30000',  # 30s query timeout
        },
        'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '60')),
    },
}

# In test mode, register a dedicated tenant test database alias ('tenant_test')
# so Django's test runner creates both Master and Tenant schemas in isolation.
# Master DB protection is strictly preserved: MasterRouter routes master models to 'default',
# TenantRouter routes tenant models to 'tenant_test', and cross-migrations remain blocked.
if 'test' in sys.argv:
    DATABASES['tenant_test'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'fitness_tenant',
        'USER': _parsed.username or 'postgres',
        'PASSWORD': _parsed.password or '',
        'HOST': _parsed.hostname or 'localhost',
        'PORT': _parsed.port or 5432,
        'TEST': {
            'NAME': 'test_fitness_tenant',
        },
    }

# Database routing — master models → default, tenant models → dynamic alias
DATABASE_ROUTERS = ['config.routers.MasterRouter', 'config.routers.TenantRouter']

# ---------------------------------------------------------------------------
# Custom Auth — Platform uses platform_users, Tenant uses tenant users
# AUTH_USER_MODEL is used only for Django admin / session internals
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = 'master.PlatformUser'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.authentication.backends.PlatformJWTAuthentication',
        'apps.authentication.backends.TenantJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    'EXCEPTION_HANDLER': 'rest_framework.views.exception_handler',
}

# ---------------------------------------------------------------------------
# SimpleJWT
# ---------------------------------------------------------------------------
JWT_ACCESS_MINUTES = int(os.getenv('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', '15'))
JWT_REFRESH_DAYS = int(os.getenv('JWT_REFRESH_TOKEN_LIFETIME_DAYS', '7'))

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=JWT_ACCESS_MINUTES),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=JWT_REFRESH_DAYS),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'sub',
    'TOKEN_TYPE_CLAIM': 'token_type',
    'JTI_CLAIM': 'jti',
}

# ---------------------------------------------------------------------------
# CORS — Never allow all origins; always whitelist explicitly
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = False  # NEVER set to True
CORS_ALLOW_CREDENTIALS = True

# Read allowed origins from env — comma-separated list
_cors_origins_env = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:5173,http://127.0.0.1:5173' if DEBUG else ''
)
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in _cors_origins_env.split(',') if o.strip()
]

if not CORS_ALLOWED_ORIGINS and not DEBUG:
    import warnings
    warnings.warn(
        'WARNING: CORS_ALLOWED_ORIGINS is empty in production. '
        'Frontend requests will be blocked by CORS. Set CORS_ALLOWED_ORIGINS in .env.',
        stacklevel=2,
    )

# CSRF trusted origins — read from env, with dev fallback
_csrf_origins_env = os.getenv(
    'CSRF_TRUSTED_ORIGINS',
    'http://localhost:5173,http://localhost:5174,http://localhost:3000' if DEBUG else ''
)
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in _csrf_origins_env.split(',') if o.strip()
]

# ---------------------------------------------------------------------------
# Security hardening — applied in non-DEBUG environments
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False').lower() in ('true', '1')

# ---------------------------------------------------------------------------
# drf-spectacular
# ---------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    'TITLE': 'PerformanceOS API',
    'DESCRIPTION': 'Multi-tenant Fitness SaaS Platform — Phase 1 Layer 1 Foundation',
    'VERSION': '2.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
}

# ---------------------------------------------------------------------------
# Tenant DB Settings
# ---------------------------------------------------------------------------
# Prefix for dynamically-provisioned tenant database names
TENANT_DB_PREFIX = os.getenv('TENANT_DB_PREFIX', 'tenant_')
# Template database used as base for new tenant DB provisioning
TENANT_DB_TEMPLATE = os.getenv('TENANT_DB_TEMPLATE', 'template1')
# PostgreSQL superuser credentials for provisioning new tenant databases
TENANT_PROVISION_DB_USER = os.getenv('PROVISION_DB_USER', _parsed.username or 'postgres')
TENANT_PROVISION_DB_PASSWORD = os.getenv('PROVISION_DB_PASSWORD', str(_parsed.password or ''))
TENANT_PROVISION_DB_HOST = os.getenv('PROVISION_DB_HOST', _parsed.hostname or 'localhost')
TENANT_PROVISION_DB_PORT = int(os.getenv('PROVISION_DB_PORT', str(_parsed.port or 5432)))

# ---------------------------------------------------------------------------
# Structured Logging
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'structured': {
            'format': '[{asctime}] {levelname} {name} | request={correlation_id} | {message}',
            'style': '{',
            'datefmt': '%Y-%m-%dT%H:%M:%S',
            'defaults': {'correlation_id': '-'},
        },
        'simple': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.getenv('LOG_LEVEL', 'DEBUG' if DEBUG else 'INFO'),
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',  # Only log slow/error queries
            'propagate': False,
        },
        'apps': {
            'handlers': ['console'],
            'level': os.getenv('APP_LOG_LEVEL', 'DEBUG' if DEBUG else 'INFO'),
            'propagate': False,
        },
        'config': {
            'handlers': ['console'],
            'level': os.getenv('APP_LOG_LEVEL', 'DEBUG' if DEBUG else 'INFO'),
            'propagate': False,
        },
    },
}

# ---------------------------------------------------------------------------
# Celery & Redis — Asynchronous Job Infrastructure (Sprint 7)
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/1')

# JSON-only task and result serialization (security hardening)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

# Task execution and result expiry
CELERY_RESULT_EXPIRES = int(os.getenv('CELERY_RESULT_EXPIRES', '86400'))  # 24 hours
CELERY_TASK_TIME_LIMIT = int(os.getenv('CELERY_TASK_TIME_LIMIT', '600'))  # 10 minutes hard limit
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', '540'))  # 9 minutes soft limit
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Task queues
CELERY_TASK_DEFAULT_QUEUE = 'default'

# Tenant Migration Concurrency Limit (bounded aggregate parallelism across all Celery workers)
TENANT_MIGRATION_CONCURRENCY_LIMIT = int(os.getenv('TENANT_MIGRATION_CONCURRENCY_LIMIT', '4'))

# STRICT EAGER MODE RULE:
# Eager mode MUST be enabled ONLY when 'test' in sys.argv.
# In normal development and production, real Redis is used.
# If Redis is unavailable outside test mode, Celery will raise an OperationalError (fail fast).
if 'test' in sys.argv:
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True
else:
    CELERY_TASK_ALWAYS_EAGER = False
    CELERY_TASK_EAGER_PROPAGATES = False


# ---------------------------------------------------------------------------
# Zata.ai S3-Compatible Private Object Storage (Sprint 7)
# ---------------------------------------------------------------------------
ZATA_S3_ENDPOINT_URL = os.getenv('ZATA_S3_ENDPOINT_URL', 'https://s3.zata.ai')
ZATA_S3_ACCESS_KEY_ID = os.getenv('ZATA_S3_ACCESS_KEY_ID', '')
ZATA_S3_SECRET_ACCESS_KEY = os.getenv('ZATA_S3_SECRET_ACCESS_KEY', '')
ZATA_S3_BUCKET_NAME = os.getenv('ZATA_S3_BUCKET_NAME', 'fitness-platform-private')
ZATA_S3_REGION_NAME = os.getenv('ZATA_S3_REGION_NAME', 'us-east-1')
ZATA_S3_USE_SSL = True  # Strictly require HTTPS/TLS transport

# Expiry settings (in seconds)
ZATA_S3_UPLOAD_EXPIRES = int(os.getenv('ZATA_S3_UPLOAD_EXPIRES', '900'))    # 15 minutes TTL for presigned PUT
ZATA_S3_DOWNLOAD_EXPIRES = int(os.getenv('ZATA_S3_DOWNLOAD_EXPIRES', '3600'))  # 60 minutes TTL for presigned GET
ZATA_S3_MAX_FILE_SIZE = int(os.getenv('ZATA_S3_MAX_FILE_SIZE', str(50 * 1024 * 1024)))  # 50 MB max


