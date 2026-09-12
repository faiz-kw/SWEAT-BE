"""
Sprint 15 — Dedicated Tenant DB Foreign Key Hardening
Implements:
1. privacy_requests.assigned_to:
   - Remove legacy duplicate deferrable foreign key constraint (privacy_requests_assigned_to_6f39be8c_fk_users_id)
   - Retain authoritative foreign key (fk_privacy_requests_assigned_to) ON DELETE SET NULL NOT DEFERRABLE
2. files.uploaded_by:
   - Remove legacy deferrable foreign key constraint (files_uploaded_by_id_ec4ad3e1_fk_users_id)
   - Add authoritative foreign key (fk_files_uploaded_by) FOREIGN KEY (uploaded_by_id) REFERENCES users(id) ON DELETE SET NULL NOT DEFERRABLE
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0018_sprint14_user_lifecycle_expand'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- ==========================================================
            -- 1. privacy_requests.assigned_to: remove duplicate FK, harden authoritative FK
            -- ==========================================================
            ALTER TABLE privacy_requests DROP CONSTRAINT IF EXISTS privacy_requests_assigned_to_6f39be8c_fk_users_id;

            ALTER TABLE privacy_requests DROP CONSTRAINT IF EXISTS fk_privacy_requests_assigned_to;
            ALTER TABLE privacy_requests
                ADD CONSTRAINT fk_privacy_requests_assigned_to
                FOREIGN KEY (assigned_to) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;

            -- ==========================================================
            -- 2. files.uploaded_by: harden FK to ON DELETE SET NULL NOT DEFERRABLE
            -- ==========================================================
            ALTER TABLE files DROP CONSTRAINT IF EXISTS files_uploaded_by_id_ec4ad3e1_fk_users_id;
            ALTER TABLE files DROP CONSTRAINT IF EXISTS fk_files_uploaded_by;
            ALTER TABLE files
                ADD CONSTRAINT fk_files_uploaded_by
                FOREIGN KEY (uploaded_by_id) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;
            """,
            reverse_sql="""
            -- Reverse: re-add default Django constraints if needed
            ALTER TABLE privacy_requests DROP CONSTRAINT IF EXISTS fk_privacy_requests_assigned_to;
            ALTER TABLE privacy_requests
                ADD CONSTRAINT fk_privacy_requests_assigned_to
                FOREIGN KEY (assigned_to) REFERENCES users(id)
                ON DELETE SET NULL;

            ALTER TABLE files DROP CONSTRAINT IF EXISTS fk_files_uploaded_by;
            ALTER TABLE files
                ADD CONSTRAINT files_uploaded_by_id_ec4ad3e1_fk_users_id
                FOREIGN KEY (uploaded_by_id) REFERENCES users(id)
                DEFERRABLE INITIALLY DEFERRED;
            """
        ),
    ]
