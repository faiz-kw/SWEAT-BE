"""
backend/scripts/setup_sarvam_integration.py

Seeds or updates the Sarvam AI Calling Integration record for all active tenant databases.
Run on the server via:
    python scripts/setup_sarvam_integration.py
"""

import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.models_infra import Integration


def main():
    print("--- Setting up Sarvam AI Voice Calling for Tenants ---")
    tenants = Tenant.objects.using('default').all()
    count = 0
    for tenant in tenants:
        ds = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
        if not ds or not ds.db_name:
            continue

        db_alias = build_tenant_db_alias(tenant.id)
        _register_tenant_connection(db_alias, ds.db_name, data_source=ds, tenant_id=tenant.id)
        set_tenant_db_alias(db_alias)

        integ, created = Integration.objects.using(db_alias).update_or_create(
            provider='SARVAM',
            defaults={
                'integration_type': 'CALLING',
                'secret_reference': 'env://SARVAM_API_KEY',
                'configuration': {
                    'org_id': os.getenv('SARVAM_ORG_ID', '01a07bde-3218-733b-ab77-04a1471f05fe'),
                    'workspace_id': os.getenv('SARVAM_WORKSPACE_ID', '01a07bde-3220-7f17-8eaf-4e72b5899657'),
                    'app_id': os.getenv('SARVAM_APP_ID', 'Conversatio-556997a2-b9bf'),
                    'app_version': 1,
                    'connection_id': os.getenv('SARVAM_CONNECTION_ID', 'b352f251-b1-c3ad4184-2187'),
                    'agent_phone_number': os.getenv('SARVAM_AGENT_PHONE_NUMBER', '+917971443277'),
                    'webhook_token': os.getenv('SARVAM_WEBHOOK_SECRET', 'r7LpmuA8tRzfFbNUKfGr9rW4e6vMkeYDWRn6VPkM80I'),
                    'webhook_base_url': os.getenv('SARVAM_WEBHOOK_BASE_URL', 'https://api.sweatfitwellness.com'),
                },
                'status': 'ACTIVE',
            }
        )
        status_str = "CREATED" if created else "UPDATED"
        print(f"[OK] Tenant '{tenant.slug}' ({tenant.name}): Sarvam Integration {status_str} (ID: {integ.id})")
        count += 1

    print(f"\nDone! Configured {count} tenant(s) for Sarvam AI calling.")


if __name__ == '__main__':
    main()

