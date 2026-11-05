from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0077_posproviderconfig_posterminal_postransaction_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- Idempotent schema sync for any tenant database
            ALTER TABLE program_categories 
                ADD COLUMN IF NOT EXISTS consumption_mode VARCHAR(20) DEFAULT 'SESSION_BASED';

            ALTER TABLE membership_policy_configs 
                ADD COLUMN IF NOT EXISTS upgrade_allowed_window_days INTEGER DEFAULT 15;

            ALTER TABLE member_assessments 
                ADD COLUMN IF NOT EXISTS assessment_type VARCHAR(30) DEFAULT 'INITIAL_ONBOARDING',
                ADD COLUMN IF NOT EXISTS training_experience VARCHAR(30) DEFAULT 'BEGINNER',
                ADD COLUMN IF NOT EXISTS current_activity_level VARCHAR(30) DEFAULT 'SEDENTARY',
                ADD COLUMN IF NOT EXISTS preferred_training_style VARCHAR(30) DEFAULT 'GENERAL_FITNESS',
                ADD COLUMN IF NOT EXISTS stated_goal_outcome TEXT DEFAULT '',
                ADD COLUMN IF NOT EXISTS target_timeline_weeks INTEGER DEFAULT 12,
                ADD COLUMN IF NOT EXISTS perceived_barrier VARCHAR(50) DEFAULT 'CONSISTENCY',
                ADD COLUMN IF NOT EXISTS member_confidence_score INTEGER DEFAULT 7,
                ADD COLUMN IF NOT EXISTS health_screening JSONB DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS screening_outcome VARCHAR(30) DEFAULT 'CLEARED',
                ADD COLUMN IF NOT EXISTS screening_coach_notes TEXT DEFAULT '',
                ADD COLUMN IF NOT EXISTS waist_circumference_cm NUMERIC(6, 2) NULL,
                ADD COLUMN IF NOT EXISTS resting_heart_rate_bpm INTEGER NULL,
                ADD COLUMN IF NOT EXISTS resting_blood_pressure VARCHAR(20) DEFAULT '',
                ADD COLUMN IF NOT EXISTS calculated_bmi NUMERIC(5, 2) NULL,
                ADD COLUMN IF NOT EXISTS skeletal_muscle_kg NUMERIC(6, 2) NULL,
                ADD COLUMN IF NOT EXISTS bmr_rmr_kcal INTEGER NULL,
                ADD COLUMN IF NOT EXISTS tdee_kcal INTEGER NULL,
                ADD COLUMN IF NOT EXISTS daily_calorie_target INTEGER NULL,
                ADD COLUMN IF NOT EXISTS nutrition_macros JSONB DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS measurement_metadata JSONB DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS functional_tests_payload JSONB DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS capability_matrix JSONB DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS coach_strengths TEXT DEFAULT '',
                ADD COLUMN IF NOT EXISTS coach_areas_to_develop TEXT DEFAULT '',
                ADD COLUMN IF NOT EXISTS reassessment_interval_weeks INTEGER DEFAULT 4,
                ADD COLUMN IF NOT EXISTS consent_body_measurements BOOLEAN DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS consent_movement_photos BOOLEAN DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS consent_ai_analysis BOOLEAN DEFAULT TRUE;
            """,
            reverse_sql="-- Irreversible without data drop, no-op"
        ),
    ]
