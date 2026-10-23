import os
import sys
import uuid
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.contrib.auth.hashers import make_password
from apps.master.models_iam import PlatformUser
from apps.master.models_tenant import Tenant
from apps.master.services_auth_directory import sync_platform_user_identity, sync_tenant_user_identity

email = "admin@sweatfitwellness.com"
password = "Admin@12345"
hashed_pw = make_password(password)

# 1. Master DB PlatformUser
pu, created = PlatformUser.objects.get_or_create(
    email=email,
    defaults={
        "id": uuid.uuid4(),
        "first_name": "Admin",
        "last_name": "Sweat",
        "is_active": True,
        "is_staff": True,
        "is_superuser": True,
    }
)
pu.set_password(password)
pu.is_active = True
pu.is_staff = True
pu.is_superuser = True
pu.save()
sync_platform_user_identity(pu)
print(f"Master PlatformUser ready: {pu.email}")

# 2. Tenant details
tenant = Tenant.objects.filter(status="ACTIVE").first()
tenant_id = tenant.id
print(f"Target Tenant: {tenant.code} ({tenant_id})")

# 3. Tenant DB (tenant_sweat_demo) users table
conn = psycopg2.connect("postgresql://postgres:masheera11@localhost:5432/tenant_sweat_demo")
cur = conn.cursor()
cur.execute("SELECT id FROM organizations LIMIT 1;")
org_row = cur.fetchone()
org_id = org_row[0] if org_row else None

cur.execute("SELECT id FROM branches LIMIT 1;")
br_row = cur.fetchone()
branch_id = br_row[0] if br_row else None

cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
row = cur.fetchone()
if row:
    tu_id = row[0]
    cur.execute("""
        UPDATE users 
        SET password_hash = %s, status = 'ACTIVE', user_type = 'STAFF', is_login_allowed = True, updated_at = NOW()
        WHERE id = %s;
    """, (hashed_pw, tu_id))
    print(f"TenantUser updated: {tu_id}")
else:
    tu_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO users (
            id, organization_id, home_branch_id, email, first_name, last_name, display_name,
            password_hash, user_type, status, is_login_allowed, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, 'Admin', 'Sweat', 'Admin Sweat',
            %s, 'STAFF', 'ACTIVE', True, NOW(), NOW()
        );
    """, (tu_id, org_id, branch_id, email, hashed_pw))
    print(f"TenantUser created: {tu_id}")

# 4. Role Assignment ORG_ADMIN
cur.execute("SELECT id FROM roles WHERE code = 'ORG_ADMIN' OR code = 'TENANT_ADMIN' LIMIT 1;")
role_row = cur.fetchone()
if role_row:
    role_id = role_row[0]
    cur.execute("SELECT id FROM role_assignments WHERE user_id = %s;", (tu_id,))
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO role_assignments (
                id, organization_id, user_id, role_id, scope_type, status, is_active, assigned_at, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, 'ORGANIZATION', 'ACTIVE', True, NOW(), NOW(), NOW()
            );
        """, (str(uuid.uuid4()), org_id, tu_id, role_id))
        print("RoleAssignment ORG_ADMIN granted!")

conn.commit()
conn.close()

# 5. Sync in Universal Auth Directory
class MockTenantUser:
    def __init__(self, uid, uemail):
        self.id = uuid.UUID(str(uid))
        self.email = uemail
        self.username = None
        self.status = "ACTIVE"
        self.is_login_allowed = True

sync_tenant_user_identity(MockTenantUser(tu_id, email), tenant_id=tenant_id)
print(f"SUCCESS: {email} is fully configured in Master & Tenant databases with password {password}!")
