"""
scripts/assign_sweat_total_videos.py

One-time bulk activity to assign YouTube workout flows, target muscles, and
equipment requirements to the "Sweat Total" class (Sweat Pilates category) datewise
from 2026-11-08 to 2026-11-30.

Supports:
1. Direct update of existing ClassOccurrence records for that date (online_join_url).
2. Registration of WorkoutContentItem (WOD Library) with volume, date, muscles, equipment.
3. Creation of WorkoutTag (MUSCLE_GROUP, EQUIPMENT) and WorkoutContentTag mappings.
4. ClassContentItem and ClassContentAssignment linking for trainer video stream.
"""

import os
import sys
import re
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.utils import timezone
from django.utils.text import slugify
from django.db.models import Q

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    ClassContentItem,
    ClassContentAssignment,
)
from apps.tenant_core.models_wod import (
    WorkoutContentItem,
    WorkoutTag,
    WorkoutContentTag,
)

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

def clean_youtube_url(url: str) -> tuple[str, str | None]:
    """Returns (clean_url, yt_id)."""
    clean = re.sub(r'\?.*$', '', url.strip())
    yt_id = None
    m = re.search(r'(?:youtu\.be/|youtube\.com/(?:embed/|v/|watch\?v=|watch\?.+&v=))([\w-]{11})', url)
    if m:
        yt_id = m.group(1)
        clean = f"https://youtu.be/{yt_id}"
    return clean, yt_id

def get_or_create_workout_tag(organization, tag_group, name):
    clean_name = name.strip()
    code = slugify(clean_name).upper().replace('-', '_')[:50] or tag_group
    tag = WorkoutTag.objects.filter(
        organization=organization,
        tag_group=tag_group,
    ).filter(
        Q(code=code) | Q(name__iexact=clean_name)
    ).first()
    if not tag:
        tag = WorkoutTag.objects.create(
            organization=organization,
            tag_group=tag_group,
            name=clean_name,
            code=code,
            status='ACTIVE',
        )
    return tag

def assign_videos_for_tenant(tenant: Tenant):
    print(f"\n========================================================")
    print(f"Assigning Sweat Total Videos for Tenant: {tenant.name} ({tenant.slug})")
    print(f"========================================================")

    with tenant_database_context(str(tenant.id)):
        # 1. Resolve Class Template strictly under 'Sweat Pilates' category
        templates = list(ClassTemplate.objects.filter(
            name__icontains='Sweat Total',
            category__name__icontains='Pilates'
        ))
        if not templates:
            print("  [ERROR] No ClassTemplate found matching 'Sweat Total' strictly under 'Sweat Pilates' category!")
            return

        for tmpl in templates:
            cat_name = tmpl.category.name if tmpl.category else 'Sweat Pilates'
            print(f"  Target Template: '{tmpl.name}' (ID: {tmpl.id}) | Category: '{cat_name}' [STRICT PILATES SCOPE]")
        template = templates[0]
        organization = template.organization
        category_name = template.category.name if template.category else 'Sweat Pilates'

        total_occurrences_updated = 0
        total_items_created = 0
        total_items_updated = 0

        for entry in SCHEDULE:
            dt_str = entry['date']
            vol = entry['volume']
            raw_url = entry['video_url']
            clean_url, yt_id = clean_youtube_url(raw_url)
            muscles = entry['muscles']
            equipment = entry['equipment']

            occ_date = datetime.strptime(dt_str, '%Y-%m-%d').date()

            # A. Find or create WorkoutContentItem in WOD library
            wod_title = f"{template.name} - {vol}"
            
            existing_wod = WorkoutContentItem.objects.filter(
                Q(raw_import_data__date=dt_str, raw_import_data__volume=vol) |
                Q(video_url__icontains=yt_id if yt_id else clean_url)
            ).first()

            raw_payload = {
                'date': dt_str,
                'volume': vol,
                'class_template_id': str(template.id),
                'class_template_name': template.name,
                'category_code': 'PILATES',
                'category_name': category_name,
                'sheet': '01. TOTAL',
                'muscles': muscles,
                'equipment': equipment,
                'source': 'WOD_BATCH_SCHEDULE',
            }

            if existing_wod:
                existing_wod.movement_name = wod_title
                existing_wod.video_url = clean_url
                existing_wod.video_provider = 'YOUTUBE' if yt_id else 'EXTERNAL'
                existing_wod.status = 'ACTIVE'
                existing_wod.raw_import_data = raw_payload
                existing_wod.description = f"Volume {vol}: Targeted muscles: {', '.join(muscles)}. Equipment: {', '.join(equipment)}."
                existing_wod.save(update_fields=[
                    'movement_name', 'video_url', 'video_provider',
                    'status', 'raw_import_data', 'description'
                ])
                wod_item = existing_wod
                total_items_updated += 1
            else:
                wod_item = WorkoutContentItem.objects.create(
                    organization=organization,
                    movement_name=wod_title,
                    description=f"Volume {vol}: Targeted muscles: {', '.join(muscles)}. Equipment: {', '.join(equipment)}.",
                    video_url=clean_url,
                    video_provider='YOUTUBE' if yt_id else 'EXTERNAL',
                    content_kind='MOVEMENT',
                    is_wod_eligible=True,
                    status='ACTIVE',
                    raw_import_data=raw_payload,
                )
                total_items_created += 1

            # B. Associate WorkoutTag and WorkoutContentTag
            for m_name in muscles:
                m_clean = m_name.strip()
                if not m_clean: continue
                m_tag = get_or_create_workout_tag(organization, 'MUSCLE_GROUP', m_clean)
                WorkoutContentTag.objects.get_or_create(organization=organization, content_item=wod_item, tag=m_tag)

            for eq_name in equipment:
                eq_clean = eq_name.strip()
                if not eq_clean: continue
                eq_tag = get_or_create_workout_tag(organization, 'EQUIPMENT', eq_clean)
                WorkoutContentTag.objects.get_or_create(organization=organization, content_item=wod_item, tag=eq_tag)

            # C. Sync to ClassContentItem for occurrence assignments
            class_ci, _ = ClassContentItem.objects.get_or_create(
                organization=organization,
                external_url=clean_url,
                defaults={
                    'title': wod_title,
                    'description': wod_item.description,
                    'content_type': 'VIDEO',
                    'status': 'ACTIVE',
                }
            )

            # D. Find ALL occurrences of Sweat Total under Sweat Pilates for this date across all branches/times
            occurrences = list(ClassOccurrence.objects.filter(
                class_template__in=templates,
                occurrence_date=occ_date,
            ))

            for occ in occurrences:
                occ.online_join_url = clean_url
                occ.save(update_fields=['online_join_url'])

                # Create/update ClassContentAssignment
                assignment = occ.content_assignments.first()
                if assignment:
                    assignment.content_item = class_ci
                    assignment.status = 'ACTIVE'
                    assignment.save(update_fields=['content_item', 'status'])
                else:
                    ClassContentAssignment.objects.create(
                        occurrence=occ,
                        content_item=class_ci,
                        assignment_method='SCHEDULED',
                        status='ACTIVE',
                    )
                total_occurrences_updated += 1

            occ_count = len(occurrences)
            print(f"  [{dt_str}] {vol}: '{wod_title}' -> {clean_url} | Muscles: {muscles} | Equip: {equipment} | Occurrences updated: {occ_count}")

        print(f"\nSummary for '{template.name}':")
        print(f"  Total Schedule Entries Processed: {len(SCHEDULE)}")
        print(f"  Content Studio Items Created:     {total_items_created}")
        print(f"  Content Studio Items Updated:     {total_items_updated}")
        print(f"  Class Occurrences Updated:        {total_occurrences_updated}")

def main():
    target_tenant_slug = os.environ.get('TARGET_TENANT_SLUG', 'sweat-demo')
    tenant = Tenant.objects.using('default').filter(slug=target_tenant_slug).first()
    if not tenant:
        tenant = Tenant.objects.using('default').first()

    if not tenant:
        print("[ERROR] No tenant found in master database!")
        return

    assign_videos_for_tenant(tenant)

if __name__ == '__main__':
    main()
