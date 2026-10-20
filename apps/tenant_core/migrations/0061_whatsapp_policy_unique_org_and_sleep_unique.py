# Generated manually for SWEAT Nutrition MVP Milestone 1.1: Targeted API Hardening & Constraint Remediation

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0060_wellness_tracking'),
    ]

    operations = [
        # 1. WhatsApp Automation Policy: Ensure physical UNIQUE constraint on organization_id
        # (Fixes divergence between 0059 Django state and raw database DDL for fresh tenant DBs)
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON c.conrelid = t.oid
                    WHERE t.relname = 'whatsapp_automation_policy'
                      AND c.contype = 'u'
                ) THEN
                    ALTER TABLE "whatsapp_automation_policy"
                    ADD CONSTRAINT "whatsapp_automation_policy_organization_id_key" UNIQUE ("organization_id");
                END IF;
            END $$;
            """,
            reverse_sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'whatsapp_automation_policy_organization_id_key'
                ) THEN
                    ALTER TABLE "whatsapp_automation_policy"
                    DROP CONSTRAINT "whatsapp_automation_policy_organization_id_key";
                END IF;
            END $$;
            """
        ),

        # 2. SleepLog: Add uniqueness constraint on (user, logged_date)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name='sleeplog',
                    constraint=models.UniqueConstraint(fields=['user', 'logged_date'], name='uq_sleep_log_user_date'),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'uq_sleep_log_user_date'
                        ) THEN
                            ALTER TABLE "sleep_logs"
                            ADD CONSTRAINT "uq_sleep_log_user_date" UNIQUE ("user_id", "logged_date");
                        END IF;
                    END $$;
                    """,
                    reverse_sql="""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'uq_sleep_log_user_date'
                        ) THEN
                            ALTER TABLE "sleep_logs"
                            DROP CONSTRAINT "uq_sleep_log_user_date";
                        END IF;
                    END $$;
                    """
                ),
            ],
        ),
    ]
