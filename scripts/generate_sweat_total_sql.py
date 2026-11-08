import os
import json
import re
import uuid

SCHEDULE = [
    {
        'date': '2026-11-08',
        'volume': 'V42',
        'video_url': 'https://youtu.be/XcKPIZWzYA8',
        'muscles': ['Posterior chain'],
        'equipment': ['Reformer', 'DB'],
    },
    {
        'date': '2026-11-09',
        'volume': 'V46',
        'video_url': 'https://youtu.be/M_slHuWjZx0',
        'muscles': ['Abdomen', 'Obliques', 'HIIT'],
        'equipment': ['Reformer', 'Box'],
    },
    {
        'date': '2026-11-10',
        'volume': 'V27',
        'video_url': 'https://youtu.be/fbVfNmcvwP0',
        'muscles': ['Biceps', 'Triceps', 'Core'],
        'equipment': ['Reformer footbar', 'DB'],
    },
    {
        'date': '2026-11-11',
        'volume': 'V14',
        'video_url': 'https://youtu.be/6p-W5VfMR5A',
        'muscles': ['Upper back', 'Legs'],
        'equipment': ['Reformer', 'Plate'],
    },
    {
        'date': '2026-11-12',
        'volume': 'V49',
        'video_url': 'https://youtu.be/CrmPm9xR46M',
        'muscles': ['Upper-body isolation', 'Abductors'],
        'equipment': ['Reformer straps'],
    },
    {
        'date': '2026-11-13',
        'volume': 'V13',
        'video_url': 'https://youtu.be/BDDmgzCaElA',
        'muscles': ['Full-body HIIT'],
        'equipment': ['Reformer', 'Plate'],
    },
    {
        'date': '2026-11-14',
        'volume': 'V22',
        'video_url': 'https://youtu.be/kMFgaN9eODo',
        'muscles': ['Legs', 'Core'],
        'equipment': ['Reformer straps', 'DB'],
    },
    {
        'date': '2026-11-15',
        'volume': 'V39',
        'video_url': 'https://youtu.be/gkOUEwUcjsY',
        'muscles': ['Upper body', 'Obliques'],
        'equipment': ['Reformer', 'Box'],
    },
    {
        'date': '2026-11-16',
        'volume': 'V36',
        'video_url': 'https://youtu.be/Z9cG9OK41L8',
        'muscles': ['Triceps', 'Core'],
        'equipment': ['Reformer', 'Box', 'DB'],
    },
    {
        'date': '2026-11-17',
        'volume': 'V25',
        'video_url': 'https://youtu.be/BUVIqKe2sJs',
        'muscles': ['Lower body', 'Spine'],
        'equipment': ['Reformer', 'DB'],
    },
    {
        'date': '2026-11-18',
        'volume': 'V20',
        'video_url': 'https://youtu.be/TD7HPAkExRA',
        'muscles': ['Shoulders', 'Core'],
        'equipment': ['Reformer', 'Plate'],
    },
    {
        'date': '2026-11-19',
        'volume': 'V33',
        'video_url': 'https://youtu.be/UjZzuzEfCFc',
        'muscles': ['Glutes', 'Biceps', 'Core'],
        'equipment': ['Reformer', 'DB'],
    },
    {
        'date': '2026-11-20',
        'volume': 'V28',
        'video_url': 'https://youtu.be/8hVxh4c3nyI',
        'muscles': ['Upper-body isolation'],
        'equipment': ['Reformer straps'],
    },
    {
        'date': '2026-11-21',
        'volume': 'V53',
        'video_url': 'https://youtu.be/RQvIW8yGFSc',
        'muscles': ['Adductors', 'Balance', 'Core'],
        'equipment': ['Reformer', 'Box', 'BOSU', 'Firm roller'],
    },
    {
        'date': '2026-11-22',
        'volume': 'V19',
        'video_url': 'https://youtu.be/ogD7gg4c_nA',
        'muscles': ['Scapula', 'Triceps', 'Leg finisher'],
        'equipment': ['Reformer', 'Box', 'DB'],
    },
    {
        'date': '2026-11-23',
        'volume': 'V47',
        'video_url': 'https://youtu.be/QdOYRfrOsGM',
        'muscles': ['Legs', 'Core finisher'],
        'equipment': ['Reformer', 'DB'],
    },
    {
        'date': '2026-11-24',
        'volume': 'V23',
        'video_url': 'https://youtu.be/ru00x_n4nvw',
        'muscles': ['Shoulders', 'Triceps', 'Core'],
        'equipment': ['Reformer footbar', 'DB'],
    },
    {
        'date': '2026-11-25',
        'volume': 'V29',
        'video_url': 'https://youtu.be/xP8vlTwcxaQ',
        'muscles': ['Full-body compound'],
        'equipment': ['Reformer', 'Resistance band', '1 DB'],
    },
    {
        'date': '2026-11-26',
        'volume': 'V37',
        'video_url': 'https://youtu.be/_o31w1aT0CM',
        'muscles': ['Balance', 'Upper-body isolation'],
        'equipment': ['Reformer', 'Box', 'DB'],
    },
    {
        'date': '2026-11-27',
        'volume': 'V44',
        'video_url': 'https://youtu.be/TUquS8Ah3nw',
        'muscles': ['Core', 'Obliques'],
        'equipment': ['Reformer', 'Box', 'DB'],
    },
    {
        'date': '2026-11-28',
        'volume': 'V52',
        'video_url': 'https://youtu.be/xM9ihqNqB4g',
        'muscles': ['Speed', 'Adductors', 'Core'],
        'equipment': ['Reformer', 'DB', 'Resistance band'],
    },
    {
        'date': '2026-11-29',
        'volume': 'V48',
        'video_url': 'https://youtu.be/xGawwgnI5ZU',
        'muscles': ['Full body', 'Core', 'Obliques'],
        'equipment': ['Reformer', 'Box', 'DB', 'Ankle weights'],
    },
    {
        'date': '2026-11-30',
        'volume': 'V16',
        'video_url': 'https://youtu.be/YEuRCZSlXqg',
        'muscles': ['Full-body compound', 'Core'],
        'equipment': ['Reformer', 'Box', 'DB'],
    },
]

def clean_youtube_url(url: str):
    m = re.search(r'(?:youtu\.be/|youtube\.com/(?:embed/|v/|watch\?v=|watch\?.+&v=))([\w-]{11})', url)
    if m:
        return f"https://youtu.be/{m.group(1)}", m.group(1)
    return re.sub(r'\?.*$', '', url.strip()), None

def escape_sql(val):
    if val is None:
        return 'NULL'
    return "'" + str(val).replace("'", "''") + "'"

def generate_sql():
    lines = []
    lines.append("-- ============================================================================")
    lines.append("-- BULK SCRIPT: Assign YouTube Video URLs, Muscle Groups, & Equipment")
    lines.append("-- STRICT SCOPE: Class 'Sweat Total' under Category 'Sweat Pilates'")
    lines.append("-- DATE RANGE:  2026-11-08 to 2026-11-30 (23 Days / 106+ Sessions across all branches)")
    lines.append("-- ============================================================================\n")
    lines.append("BEGIN;\n")

    lines.append("""
-- 1. Resolve Organization and Pilates Templates
DO $$
DECLARE
    v_org_id UUID;
    v_tmpl_count INT;
BEGIN
    SELECT organization_id INTO v_org_id
    FROM class_templates
    WHERE name ILIKE 'Sweat Total%'
      AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')
    LIMIT 1;

    IF v_org_id IS NULL THEN
        SELECT id INTO v_org_id FROM organizations LIMIT 1;
    END IF;

    SELECT count(*) INTO v_tmpl_count
    FROM class_templates
    WHERE name ILIKE 'Sweat Total%'
      AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%');

    RAISE NOTICE 'Found % template(s) for Sweat Total under Sweat Pilates. Org ID: %', v_tmpl_count, v_org_id;
END $$;
""")

    for idx, item in enumerate(SCHEDULE, 1):
        dt = item['date']
        vol = item['volume']
        raw_url = item['video_url']
        clean_url, yt_id = clean_youtube_url(raw_url)
        muscles = item['muscles']
        equipment = item['equipment']
        m_str = ', '.join(muscles)
        e_str = ', '.join(equipment)
        desc = f"Volume {vol}: Targeted muscles: {m_str}. Equipment: {e_str}."

        payload = {
            'date': dt,
            'volume': vol,
            'category_code': 'PILATES',
            'category_name': 'Sweat Pilates',
            'sheet': '01. TOTAL',
            'muscles': muscles,
            'equipment': equipment,
            'source': 'WOD_BATCH_SCHEDULE',
        }
        json_str = json.dumps(payload).replace("'", "''")

        lines.append(f"-- ============================================================================")
        lines.append(f"-- Day {idx:02d}: {dt} | {vol} | {clean_url}")
        lines.append(f"-- Muscles: {m_str} | Equipment: {e_str}")
        lines.append(f"-- ============================================================================")
        lines.append("DO $$")
        lines.append("DECLARE")
        lines.append("    v_org_id UUID;")
        lines.append("    v_wod_id UUID;")
        lines.append("    v_tag_id UUID;")
        lines.append("    v_ci_id UUID;")
        lines.append("    v_occ_count INT;")
        lines.append("BEGIN")
        lines.append("    -- Get Org ID")
        lines.append("    SELECT organization_id INTO v_org_id")
        lines.append("    FROM class_templates")
        lines.append("    WHERE name ILIKE 'Sweat Total%'")
        lines.append("      AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')")
        lines.append("    LIMIT 1;")
        lines.append("    IF v_org_id IS NULL THEN SELECT id INTO v_org_id FROM organizations LIMIT 1; END IF;\n")

        # Update occurrences
        lines.append(f"    -- A. Update Class Occurrences for {dt} (strictly Sweat Pilates category)")
        lines.append(f"    UPDATE class_occurrences")
        lines.append(f"    SET online_join_url = '{clean_url}', updated_at = NOW()")
        lines.append(f"    WHERE occurrence_date = '{dt}'")
        lines.append(f"      AND class_template_id IN (")
        lines.append(f"          SELECT id FROM class_templates")
        lines.append(f"          WHERE name ILIKE 'Sweat Total%'")
        lines.append(f"            AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')")
        lines.append(f"      );")
        lines.append(f"    GET DIAGNOSTICS v_occ_count = ROW_COUNT;\n")

        # WorkoutContentItem
        title = f"Sweat Total (Pilates) - {vol}"
        lines.append(f"    -- B. Upsert WOD Library record (WorkoutContentItem)")
        lines.append(f"    SELECT id INTO v_wod_id FROM workout_content_items")
        lines.append(f"    WHERE (raw_import_data->>'date' = '{dt}' AND raw_import_data->>'volume' = '{vol}')")
        if yt_id:
            lines.append(f"       OR video_url ILIKE '%{yt_id}%'")
        lines.append(f"    LIMIT 1;\n")

        lines.append(f"    IF v_wod_id IS NOT NULL THEN")
        lines.append(f"        UPDATE workout_content_items")
        lines.append(f"        SET movement_name = '{title}',")
        lines.append(f"            video_url = '{clean_url}',")
        lines.append(f"            video_provider = 'YOUTUBE',")
        lines.append(f"            description = '{desc}',")
        lines.append(f"            raw_import_data = '{json_str}'::jsonb,")
        lines.append(f"            status = 'ACTIVE',")
        lines.append(f"            updated_at = NOW()")
        lines.append(f"        WHERE id = v_wod_id;")
        lines.append(f"    ELSE")
        lines.append(f"        v_wod_id := gen_random_uuid();")
        lines.append(f"        INSERT INTO workout_content_items (")
        lines.append(f"            id, organization_id, movement_name, description, video_url,")
        lines.append(f"            video_provider, content_kind, is_wod_eligible, status,")
        lines.append(f"            raw_import_data, created_at, updated_at")
        lines.append(f"        ) VALUES (")
        lines.append(f"            v_wod_id, v_org_id, '{title}', '{desc}', '{clean_url}',")
        lines.append(f"            'YOUTUBE', 'MOVEMENT', TRUE, 'ACTIVE',")
        lines.append(f"            '{json_str}'::jsonb, NOW(), NOW()")
        lines.append(f"        );")
        lines.append(f"    END IF;\n")

        # Muscle tags
        lines.append(f"    -- C. Tag Muscle Groups")
        for m in muscles:
            m_clean = m.strip()
            m_code = re.sub(r'[^A-Z0-9]+', '_', m_clean.upper()).strip('_')[:50]
            lines.append(f"    -- Muscle: {m_clean}")
            lines.append(f"    SELECT id INTO v_tag_id FROM workout_tags WHERE organization_id = v_org_id AND tag_group = 'MUSCLE_GROUP' AND (code = '{m_code}' OR name ILIKE '{m_clean}') LIMIT 1;")
            lines.append(f"    IF v_tag_id IS NULL THEN")
            lines.append(f"        v_tag_id := gen_random_uuid();")
            lines.append(f"        INSERT INTO workout_tags (id, organization_id, tag_group, code, name, status, created_at, updated_at)")
            lines.append(f"        VALUES (v_tag_id, v_org_id, 'MUSCLE_GROUP', '{m_code}', '{m_clean}', 'ACTIVE', NOW(), NOW());")
            lines.append(f"    END IF;")
            lines.append(f"    INSERT INTO workout_content_tags (id, organization_id, content_item_id, tag_id, created_at)")
            lines.append(f"    VALUES (gen_random_uuid(), v_org_id, v_wod_id, v_tag_id, NOW())")
            lines.append(f"    ON CONFLICT DO NOTHING;\n")

        # Equipment tags
        lines.append(f"    -- D. Tag Equipment Needed")
        for e in equipment:
            e_clean = e.strip()
            e_code = re.sub(r'[^A-Z0-9]+', '_', e_clean.upper()).strip('_')[:50]
            lines.append(f"    -- Equip: {e_clean}")
            lines.append(f"    SELECT id INTO v_tag_id FROM workout_tags WHERE organization_id = v_org_id AND tag_group = 'EQUIPMENT' AND (code = '{e_code}' OR name ILIKE '{e_clean}') LIMIT 1;")
            lines.append(f"    IF v_tag_id IS NULL THEN")
            lines.append(f"        v_tag_id := gen_random_uuid();")
            lines.append(f"        INSERT INTO workout_tags (id, organization_id, tag_group, code, name, status, created_at, updated_at)")
            lines.append(f"        VALUES (v_tag_id, v_org_id, 'EQUIPMENT', '{e_code}', '{e_clean}', 'ACTIVE', NOW(), NOW());")
            lines.append(f"    END IF;")
            lines.append(f"    INSERT INTO workout_content_tags (id, organization_id, content_item_id, tag_id, created_at)")
            lines.append(f"    VALUES (gen_random_uuid(), v_org_id, v_wod_id, v_tag_id, NOW())")
            lines.append(f"    ON CONFLICT DO NOTHING;\n")

        # ClassContentItem & Assignment
        lines.append(f"    -- E. Link ClassContentItem & ContentAssignments")
        lines.append(f"    SELECT id INTO v_ci_id FROM class_content_items WHERE external_url = '{clean_url}' AND organization_id = v_org_id LIMIT 1;")
        lines.append(f"    IF v_ci_id IS NULL THEN")
        lines.append(f"        v_ci_id := gen_random_uuid();")
        lines.append(f"        INSERT INTO class_content_items (id, organization_id, title, description, content_type, external_url, status, created_at, updated_at)")
        lines.append(f"        VALUES (v_ci_id, v_org_id, '{title}', '{desc}', 'VIDEO', '{clean_url}', 'ACTIVE', NOW(), NOW());")
        lines.append(f"    END IF;\n")
        lines.append(f"    -- Assign to occurrences of date {dt}")
        lines.append(f"    INSERT INTO class_content_assignments (id, occurrence_id, content_item_id, assignment_method, status, created_at)")
        lines.append(f"    SELECT gen_random_uuid(), co.id, v_ci_id, 'SCHEDULED', 'ACTIVE', NOW()")
        lines.append(f"    FROM class_occurrences co")
        lines.append(f"    WHERE co.occurrence_date = '{dt}'")
        lines.append(f"      AND co.class_template_id IN (")
        lines.append(f"          SELECT id FROM class_templates")
        lines.append(f"          WHERE name ILIKE 'Sweat Total%'")
        lines.append(f"            AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')")
        lines.append(f"      )")
        lines.append(f"      AND NOT EXISTS (")
        lines.append(f"          SELECT 1 FROM class_content_assignments cca")
        lines.append(f"          WHERE cca.occurrence_id = co.id")
        lines.append(f"      );")
        lines.append(f"    RAISE NOTICE '[{dt}] {vol} -> % (Occurrences updated: %)', '{clean_url}', v_occ_count;")
        lines.append("END $$;\n")

    lines.append("""
-- Verification Summary
DO $$
DECLARE
    v_total_updated INT;
    v_total_wods INT;
BEGIN
    SELECT count(*) INTO v_total_updated
    FROM class_occurrences
    WHERE occurrence_date >= '2026-11-08' AND occurrence_date <= '2026-11-30'
      AND online_join_url IS NOT NULL
      AND class_template_id IN (
          SELECT id FROM class_templates
          WHERE name ILIKE 'Sweat Total%'
            AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')
      );

    SELECT count(*) INTO v_total_wods
    FROM workout_content_items
    WHERE raw_import_data->>'category_code' = 'PILATES'
      AND raw_import_data->>'source' = 'WOD_BATCH_SCHEDULE';

    RAISE NOTICE '===============================================================';
    RAISE NOTICE 'SUCCESS: Video assignment completed for Sweat Total (Pilates)!';
    RAISE NOTICE 'Total Sweat Pilates occurrences updated: %', v_total_updated;
    RAISE NOTICE 'Total Content Studio / WOD Library items: %', v_total_wods;
    RAISE NOTICE '===============================================================';
END $$;

COMMIT;
""")

    sql_content = "\n".join(lines)
    
    out_file1 = r"c:\Users\Masheera\Downloads\VibeCopilot_Projects\New_folder\SWEAT-BE\update_sweat_total_videos_nov2026.sql"
    
    with open(out_file1, 'w', encoding='utf-8') as f:
        f.write(sql_content)
        
    print(f"Generated SQL file at: {out_file1}")
    print(f"File size: {len(sql_content)} bytes")

if __name__ == '__main__':
    generate_sql()
