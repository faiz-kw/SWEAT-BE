"""
Management command to import video library and muscle group tags from bpcadmin.sql without downloading any video files.
"""

import os
import re
from django.core.management.base import BaseCommand
from django.utils.text import slugify
from apps.master.models_tenant import Tenant
from apps.authentication.views import _register_and_resolve_tenant
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_wod import WorkoutContentItem, WorkoutTag, WorkoutContentTag


class Command(BaseCommand):
    help = 'Imports unique exercise videos and muscle group tags from bpcadmin.sql into WorkoutContentItem'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, default='sweat-demo', help='Tenant slug')
        parser.add_argument('--sql-file', type=str, default='bpcadmin.sql', help='Path to bpcadmin.sql')

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant') or 'sweat-demo'
        sql_file = options.get('sql_file') or 'bpcadmin.sql'

        tenant = Tenant.objects.get(slug=tenant_slug)
        alias = _register_and_resolve_tenant(tenant)
        set_tenant_db_alias(alias)

        org = Organization.objects.using(alias).first()
        if not org:
            self.stdout.write(self.style.ERROR(f"No Organization found in {alias}"))
            return

        if not os.path.exists(sql_file):
            self.stdout.write(self.style.ERROR(f"SQL file '{sql_file}' not found."))
            return

        self.stdout.write(f"Scanning '{sql_file}' for video URLs and muscle group tags...")

        # Map: video_url -> { 'class_names': set(), 'muscles': set() }
        video_map = {}
        all_muscles = set()

        with open(sql_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if 'https://youtu' in line or 'http://youtu' in line or 'youtube.com' in line:
                    matches = re.finditer(r"\(\s*(\d+),\s*(\d+),\s*(\d+),\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*(\d+),\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", line)
                    for m in matches:
                        c_name = m.group(17).strip()
                        v_url = m.group(18).strip()
                        muscle = m.group(20).strip()

                        if v_url and ('youtu' in v_url):
                            if v_url not in video_map:
                                video_map[v_url] = {
                                    'class_names': set(),
                                    'muscles': set(),
                                }
                            if c_name and c_name != 'NULL':
                                video_map[v_url]['class_names'].add(c_name)
                            if muscle and muscle != 'NULL' and len(muscle) > 1:
                                # Clean up tabs and whitespace
                                clean_m = " ".join(muscle.split())
                                video_map[v_url]['muscles'].add(clean_m)
                                all_muscles.add(clean_m)

        self.stdout.write(f"Discovered {len(video_map)} unique video URLs and {len(all_muscles)} unique muscle tags.")

        # 1. Create or get WorkoutTags
        tag_cache = {}
        tags_created = 0
        for m_name in all_muscles:
            base_code = slugify(m_name).upper().replace('-', '_')[:80] or 'GENERAL'
            code = base_code
            counter = 1
            while WorkoutTag.objects.using(alias).filter(organization=org, tag_group='MUSCLE_GROUP', code=code).exists():
                existing = WorkoutTag.objects.using(alias).filter(organization=org, tag_group='MUSCLE_GROUP', code=code).first()
                if existing.name.lower() == m_name.lower():
                    tag_cache[m_name] = existing
                    break
                code = f"{base_code[:70]}_{counter}"
                counter += 1

            if m_name not in tag_cache:
                tag = WorkoutTag.objects.using(alias).create(
                    organization=org,
                    tag_group='MUSCLE_GROUP',
                    code=code,
                    name=m_name,
                    status='ACTIVE',
                )
                tag_cache[m_name] = tag
                tags_created += 1

        self.stdout.write(f"Seeded {tags_created} new WorkoutTags.")

        # 2. Create WorkoutContentItem records
        items_created = 0
        items_updated = 0
        links_created = 0

        for v_url, v_data in video_map.items():
            classes_str = ", ".join(sorted(list(v_data['class_names']))) or "Workout"
            muscles_str = ", ".join(sorted(list(v_data['muscles'])))
            movement_title = f"{classes_str} - {muscles_str}" if muscles_str else classes_str
            movement_title = movement_title[:250]

            item, created = WorkoutContentItem.objects.using(alias).get_or_create(
                organization=org,
                video_url=v_url,
                defaults={
                    'movement_name': movement_title,
                    'description': f"Target areas: {muscles_str}" if muscles_str else f"Demonstration for {classes_str}",
                    'video_provider': 'YOUTUBE',
                    'content_kind': 'MOVEMENT',
                    'status': 'ACTIVE',
                    'classification_status': 'COMPLETE',
                    'is_wod_eligible': True,
                    'video_download_status': 'NONE',  # No local video download
                }
            )

            if created:
                items_created += 1
            else:
                items_updated += 1

            # Link tags
            for m_name in v_data['muscles']:
                tag_obj = tag_cache.get(m_name)
                if tag_obj:
                    _, link_created = WorkoutContentTag.objects.using(alias).get_or_create(
                        organization=org,
                        content_item=item,
                        tag=tag_obj,
                    )
                    if link_created:
                        links_created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Successfully processed video library: {items_created} created, {items_updated} existing, "
            f"{links_created} tag associations linked. (Zero video files downloaded)."
        ))
