"""
Sprint 13 — Notification Templates & Schedule Constraints Hardening
Enforces column defaults and NOT DEFERRABLE ON DELETE RESTRICT / SET NULL constraints.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0016_sprint13_notification_templates_and_schedule_expand'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- ==========================================================
            -- 1. notification_templates defaults and FK hardening
            -- ==========================================================
            ALTER TABLE notification_templates ALTER COLUMN version SET DEFAULT 1;
            ALTER TABLE notification_templates ALTER COLUMN is_active SET DEFAULT TRUE;

            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS notification_templat_organization_id_027ca196_fk_organizat;
            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS notification_templates_branch_id_ae397c83_fk_branches_id;
            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS fk_notification_templates_organization;
            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS fk_notification_templates_branch;

            ALTER TABLE notification_templates
                ADD CONSTRAINT fk_notification_templates_organization
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                ON DELETE RESTRICT NOT DEFERRABLE;

            ALTER TABLE notification_templates
                ADD CONSTRAINT fk_notification_templates_branch
                FOREIGN KEY (branch_id) REFERENCES branches(id)
                ON DELETE RESTRICT NOT DEFERRABLE;

            -- ==========================================================
            -- 2. branch_working_hours defaults & FK hardening (NOT DEFERRABLE)
            -- ==========================================================
            ALTER TABLE branch_working_hours ALTER COLUMN is_open SET DEFAULT TRUE;
            ALTER TABLE branch_working_hours ALTER COLUMN is_24_hours SET DEFAULT FALSE;

            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint c
                    JOIN pg_class t ON c.conrelid = t.oid
                    WHERE t.relname = 'branch_working_hours' AND c.contype = 'f'
                ) LOOP
                    EXECUTE 'ALTER TABLE branch_working_hours DROP CONSTRAINT ' || quote_ident(r.conname);
                END LOOP;
            END $$;

            ALTER TABLE branch_working_hours
                ADD CONSTRAINT fk_bwh_branch
                FOREIGN KEY (branch_id) REFERENCES branches(id)
                ON DELETE RESTRICT NOT DEFERRABLE;

            ALTER TABLE branch_working_hours
                ADD CONSTRAINT fk_bwh_created_by
                FOREIGN KEY (created_by_id) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;

            ALTER TABLE branch_working_hours
                ADD CONSTRAINT fk_bwh_updated_by
                FOREIGN KEY (updated_by_id) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;

            -- ==========================================================
            -- 3. branch_operating_exceptions defaults & FK hardening (NOT DEFERRABLE)
            -- ==========================================================
            ALTER TABLE branch_operating_exceptions ALTER COLUMN is_closed SET DEFAULT FALSE;

            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint c
                    JOIN pg_class t ON c.conrelid = t.oid
                    WHERE t.relname = 'branch_operating_exceptions' AND c.contype = 'f'
                ) LOOP
                    EXECUTE 'ALTER TABLE branch_operating_exceptions DROP CONSTRAINT ' || quote_ident(r.conname);
                END LOOP;
            END $$;

            ALTER TABLE branch_operating_exceptions
                ADD CONSTRAINT fk_boe_branch
                FOREIGN KEY (branch_id) REFERENCES branches(id)
                ON DELETE RESTRICT NOT DEFERRABLE;

            ALTER TABLE branch_operating_exceptions
                ADD CONSTRAINT fk_boe_created_by
                FOREIGN KEY (created_by_id) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;

            ALTER TABLE branch_operating_exceptions
                ADD CONSTRAINT fk_boe_updated_by
                FOREIGN KEY (updated_by_id) REFERENCES users(id)
                ON DELETE SET NULL NOT DEFERRABLE;
            """,
            reverse_sql="""
            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS fk_notification_templates_organization;
            ALTER TABLE notification_templates DROP CONSTRAINT IF EXISTS fk_notification_templates_branch;
            ALTER TABLE branch_working_hours DROP CONSTRAINT IF EXISTS fk_bwh_branch;
            ALTER TABLE branch_working_hours DROP CONSTRAINT IF EXISTS fk_bwh_created_by;
            ALTER TABLE branch_working_hours DROP CONSTRAINT IF EXISTS fk_bwh_updated_by;
            ALTER TABLE branch_operating_exceptions DROP CONSTRAINT IF EXISTS fk_boe_branch;
            ALTER TABLE branch_operating_exceptions DROP CONSTRAINT IF EXISTS fk_boe_created_by;
            ALTER TABLE branch_operating_exceptions DROP CONSTRAINT IF EXISTS fk_boe_updated_by;
            """
        ),
    ]
