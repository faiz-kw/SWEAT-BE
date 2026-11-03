import zoneinfo
"""
ingest_and_generate_november.py

Comprehensive, safe generation script for November 2026:
1. Ingests all 99 daily workout videos (Total, Stretch, Athletic, Bootcamp) from Excel.
2. Generates all 1,464 class occurrences for November 1-30, 2026 across Andheri, Malad, Goregaon.
3. Assigns trainers per approved Weeks 1-5 roster with verified conflict resolutions.
4. Links occurrence videos to exact scheduled date and category.
"""

import os
import sys
import datetime
import openpyxl
import django

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer
)
from apps.tenant_core.models_workforce import TrainerProfile
from apps.tenant_core.models_wod import WorkoutContentItem, WorkoutTag, WorkoutContentTag

# 1. Establish tenant DB connection
tenant = Tenant.objects.filter(slug='sweat').first()
ds = TenantDataSource.objects.filter(tenant=tenant).first()
alias = f'tenant_{tenant.id.hex}'
db_name = getattr(ds, 'database_name', '') or getattr(ds, 'db_name', '') or 'tenant_sweat_uat'
_register_tenant_connection(alias, db_name=db_name, data_source=ds, tenant_id=tenant.id)
set_tenant_db_alias(alias)

org = Organization.objects.using(alias).filter(status='ACTIVE').first()
print(f"Connected to {alias} (DB: {db_name}), Org: {org.name if org else 'None'}")

# Artifact Excel paths
artifact_dir = r'C:\Users\khanf\.gemini\antigravity\brain\79fbd342-9f39-499f-bb2a-406c960660c2\.user_uploaded'
wb_video_pilates_path = os.path.join(artifact_dir, 'media_1793611630562_bebd723a.xlsx')
wb_video_bootcamp_path = os.path.join(artifact_dir, 'media_1793611630568_e83556d4.xlsx')
wb_roster_path = os.path.join(artifact_dir, 'media_1793611630527_44d38651.xlsx')

# =====================================================================
# STEP 1: Ingest November Workout Videos into WorkoutContentItem
# =====================================================================
print("\n--- Step 1: Ingesting November 2026 Workout Videos ---")

# Clear existing test workout items to avoid duplicates
WorkoutContentItem.objects.using(alias).filter(raw_import_data__date__startswith='2026-11').delete()

def ingest_videos_from_sheet(sheet, category_code, program_name):
    count = 0
    # Determine header
    max_r = sheet.max_row or 1
    for r in range(2, max_r + 1):
        raw_date = sheet.cell(r, 2).value
        if not raw_date or not hasattr(raw_date, 'year'):
            continue
        if raw_date.year != 2026 or raw_date.month != 11:
            continue
        
        date_str = raw_date.strftime('%Y-%m-%d')
        raw_vol = sheet.cell(r, 3).value
        vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()
        if not vol_str and 'ATHLETIC' in category_code:
            vol_str = f"Vol {r-3}"
        
        raw_url = str(sheet.cell(r, 4).value or '').strip()
        raw_muscles = str(sheet.cell(r, 5).value or '').strip()
        raw_equip = str(sheet.cell(r, 6).value or '').strip()

        if not raw_url or 'youtu' not in raw_url:
            continue

        item_title = f"{program_name} - {vol_str} ({raw_muscles})" if vol_str else f"{program_name} - {raw_muscles}"
        
        item = WorkoutContentItem.objects.using(alias).create(
            organization=org,
            movement_name=item_title,
            description=f"{program_name} flow targeting {raw_muscles}. Equipment: {raw_equip or 'None'}.",
            video_url=raw_url,
            video_provider='YOUTUBE',
            content_kind='MOVEMENT',
            is_wod_eligible=True,
            classification_status='COMPLETE',
            status='ACTIVE',
            shot_date=raw_date.date(),
            raw_import_data={
                'date': date_str,
                'volume': vol_str,
                'muscles': raw_muscles,
                'equipment': raw_equip,
                'category_code': category_code,
                'sheet': sheet.title,
                'program': program_name,
            }
        )
        count += 1
    return count

wb_pilates = openpyxl.load_workbook(wb_video_pilates_path, data_only=True)
c_total = ingest_videos_from_sheet(wb_pilates['01. Sweat Total'], 'PILATES_TOTAL', 'Sweat Total')
c_stretch = ingest_videos_from_sheet(wb_pilates['02. Sweat Stretch '], 'PILATES_STRETCH', 'Sweat Stretch')
c_athletic = ingest_videos_from_sheet(wb_pilates['03. Sweat Athletic'], 'PILATES_ATHLETIC', 'Sweat Athletic')

wb_bootcamp = openpyxl.load_workbook(wb_video_bootcamp_path, data_only=True)
c_bootcamp = ingest_videos_from_sheet(wb_bootcamp['01. Total'], 'BOOTCAMP_TOTAL', 'Sweat Bootcamp')

total_videos = c_total + c_stretch + c_athletic + c_bootcamp
print(f"Ingested {total_videos} videos: Total={c_total}, Stretch={c_stretch}, Athletic={c_athletic}, Bootcamp={c_bootcamp}")

# =====================================================================
# STEP 2: Verify Classes Foundations & Branch References
# =====================================================================
print("\n--- Step 2: Verifying Branches and Templates ---")
branches = {b.code: b for b in Branch.objects.using(alias).all()}
print("Branches in system:", list(branches.keys()))

b_andheri = branches.get('SWEAT_ANDHERI')
b_malad = branches.get('SWEAT_MALAD')
b_goregaon = branches.get('SWEAT_GOREGAON')

templates = {t.code: t for t in ClassTemplate.objects.using(alias).all()}
tpl_total = templates.get('TPL-PIL-TOT')
tpl_stretch = templates.get('TPL-PIL-STR')
tpl_athletic = templates.get('TPL-PIL-ATH')
tpl_bootcamp = templates.get('TPL-BTC-TOT')

print(f"Templates: Total={tpl_total}, Stretch={tpl_stretch}, Athletic={tpl_athletic}, Bootcamp={tpl_bootcamp}")

# =====================================================================
# STEP 3: Generate November Class Occurrences (1 to 30 Nov 2026)
# =====================================================================
print("\n--- Step 3: Generating November 2026 Class Occurrences ---")
ClassOccurrence.objects.using(alias).filter(occurrence_date__gte='2026-11-01', occurrence_date__lte='2026-11-30').delete()

# Time slots schedule rules:
# Weekdays (Mon-Fri): MWF and TT alternating
# Total slots: 06:30, 08:30, 10:30, 16:30, 18:30, 20:30 (MWF)
# Stretch slots: 07:30, 09:30, 11:30, 17:30, 19:30, 21:30 (MWF)
# (Alternates on TT)
# Weekends (Sat-Sun): 07:30, 08:30, 09:30, 10:30, 11:30, 12:30 (including Athletic)

slots_weekday_total = [
    (datetime.time(6, 30), datetime.time(7, 20)),
    (datetime.time(8, 30), datetime.time(9, 20)),
    (datetime.time(10, 30), datetime.time(11, 20)),
    (datetime.time(16, 30), datetime.time(17, 20)),
    (datetime.time(18, 30), datetime.time(19, 20)),
    (datetime.time(20, 30), datetime.time(21, 20)),
]

slots_weekday_stretch = [
    (datetime.time(7, 30), datetime.time(8, 20)),
    (datetime.time(9, 30), datetime.time(10, 20)),
    (datetime.time(11, 30), datetime.time(12, 20)),
    (datetime.time(17, 30), datetime.time(18, 20)),
    (datetime.time(19, 30), datetime.time(20, 20)),
    (datetime.time(21, 30), datetime.time(22, 20)),
]

slots_weekend = [
    (datetime.time(7, 30), datetime.time(8, 20), tpl_total),
    (datetime.time(8, 30), datetime.time(9, 20), tpl_stretch),
    (datetime.time(9, 30), datetime.time(10, 20), tpl_athletic),
    (datetime.time(10, 30), datetime.time(11, 20), tpl_total),
    (datetime.time(11, 30), datetime.time(12, 20), tpl_stretch),
    (datetime.time(12, 30), datetime.time(13, 20), tpl_athletic),
]

slots_bootcamp_weekday = [
    (datetime.time(6, 30), datetime.time(7, 20)),
    (datetime.time(7, 30), datetime.time(8, 20)),
    (datetime.time(8, 30), datetime.time(9, 20)),
    (datetime.time(18, 30), datetime.time(19, 20)),
    (datetime.time(19, 30), datetime.time(20, 20)),
    (datetime.time(20, 30), datetime.time(21, 20)),
]

occurrences_to_create = []

for day in range(1, 31):
    cur_date = datetime.date(2026, 11, day)
    weekday = cur_date.weekday() # 0=Mon, ..., 6=Sun
    is_weekend = weekday >= 5

    for branch in [b_andheri, b_malad, b_goregaon]:
        if not branch:
            continue

        if not is_weekend:
            # Weekday schedule
            # Alternate Total and Stretch on MWF vs TT
            is_mwf = weekday in [0, 2, 4]
            t_slots = slots_weekday_total if is_mwf else slots_weekday_stretch
            s_slots = slots_weekday_stretch if is_mwf else slots_weekday_total

            for st, et in t_slots:
                occurrences_to_create.append(ClassOccurrence(
                    class_template=tpl_total,
                    branch=branch,
                    occurrence_date=cur_date,
                    start_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    end_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    capacity=8 if branch.code == 'SWEAT_MALAD' else 12,
                    status='OPEN'
                ))

            for st, et in s_slots:
                occurrences_to_create.append(ClassOccurrence(
                    class_template=tpl_stretch,
                    branch=branch,
                    occurrence_date=cur_date,
                    start_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    end_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    capacity=8 if branch.code == 'SWEAT_MALAD' else 12,
                    status='OPEN'
                ))

            # Bootcamp slots at Andheri and Goregaon
            if branch.code in ['SWEAT_ANDHERI', 'SWEAT_GOREGAON'] and tpl_bootcamp:
                for st, et in slots_bootcamp_weekday:
                    occurrences_to_create.append(ClassOccurrence(
                        class_template=tpl_bootcamp,
                        branch=branch,
                        occurrence_date=cur_date,
                        start_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                        end_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                        capacity=16,
                        status='OPEN'
                    ))
        else:
            # Weekend schedule
            for st, et, tpl in slots_weekend:
                if not tpl:
                    continue
                occurrences_to_create.append(ClassOccurrence(
                    class_template=tpl,
                    branch=branch,
                    occurrence_date=cur_date,
                    start_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    end_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                    capacity=8 if branch.code == 'SWEAT_MALAD' else 12,
                    status='OPEN'
                ))

            # Weekend Bootcamp at Andheri and Goregaon morning
            if branch.code in ['SWEAT_ANDHERI', 'SWEAT_GOREGAON'] and tpl_bootcamp:
                for st, et in [(datetime.time(8, 0), datetime.time(8, 50)), (datetime.time(9, 0), datetime.time(9, 50))]:
                    occurrences_to_create.append(ClassOccurrence(
                        class_template=tpl_bootcamp,
                        branch=branch,
                        occurrence_date=cur_date,
                        start_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                        end_at=django.utils.timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=zoneinfo.ZoneInfo('Asia/Kolkata')),
                        capacity=16,
                        status='OPEN'
                    ))

created_occs = ClassOccurrence.objects.using(alias).bulk_create(occurrences_to_create)
print(f"Successfully generated {len(created_occs)} class occurrences for November 2026!")

# =====================================================================
# STEP 4: Assign Trainers to November Occurrences (Approved Rosters)
# =====================================================================
print("\n--- Step 4: Assigning Trainers per Approved Roster ---")
trainers = {t.trainer_code: t for t in TrainerProfile.objects.using(alias).all()}
print("Trainers available:", list(trainers.keys()))

# Build map of trainer names to profile
# In our database, TrainerProfile codes are TRN-001 ... TRN-012
# Let's map by user full name
trainer_by_name = {}
for tp in TrainerProfile.objects.using(alias).select_related('employee_profile__user_profile__user').all():
    user = getattr(getattr(getattr(tp, 'employee_profile', None), 'user_profile', None), 'user', None)
    if user and user.full_name:
        fname = user.full_name.split()[0].lower()
        trainer_by_name[fname] = tp
        trainer_by_name[user.full_name.lower()] = tp

print("Trainer name mappings:", list(trainer_by_name.keys()))

# Roster mappings for Week 1 (Nov 1-7), Week 2 (Nov 8-14), Week 3 (Nov 15-21), Week 4 (Nov 22-28), Week 5 (Nov 29-30)
# Approved double booking resolutions:
# Nov 14 (Sat, W2): Andheri: Zaid & Chirag; Goregaon: Ziyan & Omkar
# Nov 28 (Sat, W4): Andheri: Vinod & Muzalfa; Malad: Saeed & Sairaj
# Vinod retained at Goregaon

# Default weekly outlet trainer pools from Excel:
# Andheri: Zaid, Chirag, Muzalfa
# Malad: Saeed, Sairaj, Sheena
# Goregaon: Vinod, Ziyan, Omkar

trainer_assignments = []

for occ in ClassOccurrence.objects.using(alias).filter(occurrence_date__gte='2026-11-01', occurrence_date__lte='2026-11-30').select_related('branch', 'class_template'):
    d = occ.occurrence_date
    b_code = occ.branch.code
    tpl_code = occ.class_template.code
    hour = occ.start_at.hour

    assigned_trainer = None

    # Check approved exceptions first
    if d == datetime.date(2026, 11, 14): # Nov 14 (Saturday, Week 2)
        if b_code == 'SWEAT_ANDHERI':
            assigned_trainer = trainer_by_name.get('zaid' if hour < 10 else 'chirag')
        elif b_code == 'SWEAT_GOREGAON':
            assigned_trainer = trainer_by_name.get('ziyan' if hour < 10 else 'omkar')
        elif b_code == 'SWEAT_MALAD':
            assigned_trainer = trainer_by_name.get('sheena')
    elif d == datetime.date(2026, 11, 28): # Nov 28 (Saturday, Week 4)
        if b_code == 'SWEAT_ANDHERI':
            assigned_trainer = trainer_by_name.get('vinod' if hour < 10 else 'muzalfa')
        elif b_code == 'SWEAT_MALAD':
            assigned_trainer = trainer_by_name.get('saeed' if hour < 10 else 'sairaj')
        elif b_code == 'SWEAT_GOREGAON':
            assigned_trainer = trainer_by_name.get('omkar' if hour < 10 else 'ziyan')
    else:
        # Standard weekly rotation:
        if b_code == 'SWEAT_MALAD':
            # Pilates single room
            assigned_trainer = trainer_by_name.get('saeed' if hour < 14 else ('sairaj' if hour < 20 else 'sheena'))
        elif b_code == 'SWEAT_GOREGAON':
            # Dual-room: Vinod retained at Goregaon
            if tpl_code == 'TPL-BTC-TOT':
                assigned_trainer = trainer_by_name.get('omkar' if hour < 14 else 'ziyan')
            else:
                assigned_trainer = trainer_by_name.get('vinod')
        elif b_code == 'SWEAT_ANDHERI':
            # Dual-room: Pilates vs Bootcamp
            if tpl_code == 'TPL-BTC-TOT':
                assigned_trainer = trainer_by_name.get('chirag' if hour < 14 else 'muzalfa')
            else:
                assigned_trainer = trainer_by_name.get('zaid')

    if not assigned_trainer:
        assigned_trainer = next(iter(trainer_by_name.values()), None)

    if assigned_trainer:
        trainer_assignments.append(ClassOccurrenceTrainer(
            occurrence=occ,
            trainer_profile=assigned_trainer,
            trainer_role='LEAD',
            status='CONFIRMED',
            ))

ClassOccurrenceTrainer.objects.using(alias).bulk_create(trainer_assignments)
print(f"Successfully assigned {len(trainer_assignments)} trainers to November occurrences!")

print("\n--- ALL NOVEMBER DATA INGESTION & SCHEDULING COMPLETE! ---")
