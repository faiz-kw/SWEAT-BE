import os
import json
import re

SCHEDULE = [
    {
        'date': '2026-11-08',
        'volume': '7',
        'video_url': 'https://youtu.be/Wvw0V8fsmc8',
        'muscles': ['Speed & Agility'],
        'equipment': ['Reformer Footbar', 'TRX'],
    },
    {
        'date': '2026-11-14',
        'volume': '12',
        'video_url': 'https://youtu.be/kP094073z-s',
        'muscles': ['Upper-Body Strength & Lower-Body Speed'],
        'equipment': ['Reformer', 'Box', 'Dumbbells', 'TRX'],
    },
    {
        'date': '2026-11-15',
        'volume': '16',
        'video_url': 'https://youtu.be/0-281bxrvF8',
        'muscles': ['Balance & Coordination'],
        'equipment': ['Reformer', 'Box', 'TRX'],
    },
    {
        'date': '2026-11-21',
        'volume': '19',
        'video_url': 'https://youtu.be/U0fAD0xv1mM',
        'muscles': ['Serratus Anterior, Obliques & Explosive Movement'],
        'equipment': ['Reformer', 'Box', 'Weight Plate', 'TRX'],
    },
    {
        'date': '2026-11-22',
        'volume': '20',
        'video_url': 'https://youtu.be/XSNVCpUEjWM',
        'muscles': ['Full-Body Muscular Endurance'],
        'equipment': ['Reformer', 'Box', 'TRX', 'Weight Plate'],
    },
    {
        'date': '2026-11-28',
        'volume': '13',
        'video_url': 'https://youtu.be/3-W0cmEgOWw',
        'muscles': ['Transverse Rotation & Agility'],
        'equipment': ['Reformer', 'Fitness Circle', 'ViPR'],
    },
    {
        'date': '2026-11-29',
        'volume': '21',
        'video_url': 'https://youtu.be/0qCpN6F69J0',
        'muscles': ['Full-Body Balance, Core & Hypertrophy'],
        'equipment': ['Reformer', 'Box', 'Dumbbell', 'TRX'],
    },
]

def clean_youtube_url(url: str):
    m = re.search(r'(?:youtu\.be/|youtube\.com/(?:embed/|v/|watch\?v=|watch\?.+&v=))([\w-]{11})', url)
    if m:
        return f"https://youtu.be/{m.group(1)}", m.group(1)
    return re.sub(r'\?.*$', '', url.strip()), None

def generate_sql():
    lines = []
    lines.append("-- ============================================================================")
    lines.append("-- BULK SCRIPT: Assign YouTube Video URLs, Muscle Groups, & Equipment")
    lines.append("-- STRICT SCOPE: Class 'Sweat Athletic' under Category 'Sweat Pilates'")
    lines.append("-- DATE RANGE:  November 2026 Weekend Sessions across all branches")
    lines.append("-- ============================================================================\n")
    lines.append("BEGIN;\n")

    lines.append("""
-- 1. Resolve Organization and Athletic Pilates Templates
DO $$
DECLARE
    v_org_id UUID;
    v_tmpl_count INT;
BEGIN
    SELECT organization_id INTO v_org_id
    FROM class_templates
    WHERE name ILIKE '%Athletic%'
      AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')
    LIMIT 1;

    IF v_org_id IS NULL THEN
        SELECT id INTO v_org_id FROM organizations LIMIT 1;
    END IF;

    SELECT count(*) INTO v_tmpl_count
    FROM class_templates
    WHERE name ILIKE '%Athletic%'
      AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%');

    RAISE NOTICE 'Found % template(s) for Sweat Athletic under Sweat Pilates. Org ID: %', v_tmpl_count, v_org_id;
END $$;
""")

    for idx, item in enumerate(SCHEDULE, 1):
        dt = item['date']
        raw_vol = str(item['volume']).strip()
        vol = f"V{raw_vol}" if not raw_vol.upper().startswith('V') else raw_vol.upper()
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
            'sheet': '02. ATHLETIC',
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
        lines.append("    WHERE name ILIKE '%Athletic%'")
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
        lines.append(f"          WHERE name ILIKE '%Athletic%'")
        lines.append(f"            AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')")
        lines.append(f"      );")
        lines.append(f"    GET DIAGNOSTICS v_occ_count = ROW_COUNT;\n")

        # WorkoutContentItem
        title = f"Sweat Athletic (Pilates) - {vol}"
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
            lines.append(f"        INSERT INTO workout_tags (id, organization_id, tag_group, code, name, display_order, status, created_at, updated_at)")
            lines.append(f"        VALUES (v_tag_id, v_org_id, 'MUSCLE_GROUP', '{m_code}', '{m_clean}', 0, 'ACTIVE', NOW(), NOW());")
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
            lines.append(f"        INSERT INTO workout_tags (id, organization_id, tag_group, code, name, display_order, status, created_at, updated_at)")
            lines.append(f"        VALUES (v_tag_id, v_org_id, 'EQUIPMENT', '{e_code}', '{e_clean}', 0, 'ACTIVE', NOW(), NOW());")
            lines.append(f"    END IF;")
            lines.append(f"    INSERT INTO workout_content_tags (id, organization_id, content_item_id, tag_id, created_at)")
            lines.append(f"    VALUES (gen_random_uuid(), v_org_id, v_wod_id, v_tag_id, NOW())")
            lines.append(f"    ON CONFLICT DO NOTHING;\n")

        # ClassContentItem & Assignment
        lines.append(f"    -- E. Link ClassContentItem & ContentAssignments")
        lines.append(f"    SELECT id INTO v_ci_id FROM class_content_items WHERE external_url = '{clean_url}' AND organization_id = v_org_id LIMIT 1;")
        lines.append(f"    IF v_ci_id IS NULL THEN")
        lines.append(f"        v_ci_id := gen_random_uuid();")
        lines.append(f"        INSERT INTO class_content_items (id, organization_id, title, description, content_type, external_url, display_order, status, created_at, updated_at)")
        lines.append(f"        VALUES (v_ci_id, v_org_id, '{title}', '{desc}', 'VIDEO', '{clean_url}', 0, 'ACTIVE', NOW(), NOW());")
        lines.append(f"    END IF;\n")
        lines.append(f"    -- Assign to occurrences of date {dt}")
        lines.append(f"    INSERT INTO class_content_assignments (id, occurrence_id, content_item_id, rotation_cycle_number, rotation_position, assignment_method, assigned_at, status, created_at)")
        lines.append(f"    SELECT gen_random_uuid(), co.id, v_ci_id, 1, 1, 'SCHEDULED', NOW(), 'ACTIVE', NOW()")
        lines.append(f"    FROM class_occurrences co")
        lines.append(f"    WHERE co.occurrence_date = '{dt}'")
        lines.append(f"      AND co.class_template_id IN (")
        lines.append(f"          SELECT id FROM class_templates")
        lines.append(f"          WHERE name ILIKE '%Athletic%'")
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
          WHERE name ILIKE '%Athletic%'
            AND category_id IN (SELECT id FROM class_categories WHERE name ILIKE '%Pilates%')
      );

    SELECT count(*) INTO v_total_wods
    FROM workout_content_items
    WHERE raw_import_data->>'category_code' = 'PILATES'
      AND raw_import_data->>'sheet' = '02. ATHLETIC'
      AND raw_import_data->>'source' = 'WOD_BATCH_SCHEDULE';

    RAISE NOTICE '===============================================================';
    RAISE NOTICE 'SUCCESS: Video assignment completed for Sweat Athletic (Pilates)!';
    RAISE NOTICE 'Total Sweat Athletic occurrences updated: %', v_total_updated;
    RAISE NOTICE 'Total Content Studio / WOD Library items: %', v_total_wods;
    RAISE NOTICE '===============================================================';
END $$;

COMMIT;
""")

    sql_content = "\n".join(lines)
    out_file1 = r"c:\Users\Masheera\Downloads\VibeCopilot_Projects\New_folder\SWEAT-BE\update_sweat_athletic_videos_nov2026.sql"
    
    with open(out_file1, 'w', encoding='utf-8') as f:
        f.write(sql_content)
        
    print(f"Generated SQL file at: {out_file1}")
    print(f"File size: {len(sql_content)} bytes")

if __name__ == '__main__':
    generate_sql()
