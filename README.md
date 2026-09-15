# SWEAT Platform Backend (SWEAT-BE)

Enterprise multi-tenant Django REST Framework backend powering the **Fitness Command Center / PerformanceOS** business platform.

---

## Architectural Topology

The backend implements a **Database-per-Tenant** multi-tenant isolation model with a centralized **Master Control Plane**:

1. **Master Database (`default`)**:
   - Manages tenant accounts, SaaS subscriptions, commercial plans, catalog entitlements, platform IAM, global audit logs, and infrastructure provisioning.
   - Hosted under `apps/master/`.

2. **Tenant Databases (`tenant_<slug>`)**:
   - Isolated schema per tenant for organizations, branches, departments, members, bookings, finance, role definitions, permission matrices, privacy DSR, and tenant file storage metadata.
   - Hosted under `apps/tenant_core/`.

3. **Database Routing & Isolation**:
   - Centralized database router: [`config/routers.py`](./config/routers.py).
   - Middleware-driven tenant database resolution: [`config/tenant_middleware.py`](./config/tenant_middleware.py).
   - Thread-local context protection: [`apps/tenant_core/context.py`](./apps/tenant_core/context.py).

---

## Directory Organization

```
backend/
├── manage.py                 # Django management CLI
├── requirements.txt          # Python dependencies (Production)
├── requirements-dev.txt      # Development & testing dependencies (fakeredis, etc.)
├── .env.example              # Development environment variables template
├── .gitignore                # Git ignore specifications
├── apps/
│   ├── authentication/       # Dual-mode JWT authentication (Platform & Tenant tokens)
│   ├── master/               # Control plane, tenant provisioning, SaaS billing & metering
│   └── tenant_core/          # Tenant domain: RBAC engine, IAM, storage, privacy, scheduling
├── config/
│   ├── settings.py           # Core Django settings & third-party integrations
│   ├── urls.py               # Root API routing & OpenAPI 3.1 documentation
│   ├── celery.py             # Distributed task worker configuration
│   ├── routers.py            # Master vs Tenant database routing engine
│   ├── tenant_middleware.py  # Request lifecycle tenant resolution & DB connection registration
│   ├── middleware.py         # Correlation ID and security middleware
│   └── secrets.py            # KMS / Secrets Manager integration layer
├── tests/                    # Authoritative regression test suite (344 tests)
├── scripts/                  # Development & runtime verification utilities
└── _archived_apps/           # Historical monolithic prototypes (archived)
```

---

## Environment Setup

```bash
# Production environment
pip install -r requirements.txt

# Development / testing environment
pip install -r requirements-dev.txt
```

---

## Verification & Testing

### Running Full Test Suite
```bash
python manage.py test
```
All 344 multi-tenant regression tests execute against isolated test databases (`test_fitness_master` and `test_fitness_tenant`).

### System Checks & Migrations
```bash
# Verify system configuration
python manage.py check

# Check migration status
python manage.py showmigrations
```

### Starting Background Workers
```bash
# Celery worker (solo pool recommended on Windows dev)
celery -A config worker -l INFO -P solo
```
