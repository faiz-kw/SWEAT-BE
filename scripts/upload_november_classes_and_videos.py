"""
upload_november_classes_and_videos.py

Uploads class occurrences, maps curriculum YouTube videos, and allocates trainers
for the month of November 2026.
Supports multi-tenant operation via --tenant-slug.
Guarantees duplicate protection: skips any existing classes, videos, or trainer assignments without overwriting.
Auto-creates new trainers if they do not already exist in the system.
"""

import os
import sys
import glob
import re
import argparse
import datetime
import openpyxl
import django
from django.utils import timezone
from django.db import transaction

# Setup Django environment
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
    ClassCategory, ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer,
    ClassContentItem, ClassContentAssignment
)
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_workforce import (
    UserProfile, EmployeeProfile, TrainerProfile, TrainerSpecialty, TrainerSpecialtyAssignment
)
from apps.tenant_core.models_wod import WorkoutContentItem


def resolve_tenant_connection(tenant_slug: str):
    """
    Connects to the specified tenant's database dynamically using platform routing.
    """
    tenant = Tenant.objects.filter(slug=tenant_slug).first()
    if not tenant:
        raise ValueError(f"Tenant with slug '{tenant_slug}' not found in master database.")

    ds = TenantDataSource.objects.filter(tenant=tenant).first()
    alias = f"tenant_{tenant.id.hex}"
    db_name = getattr(ds, 'database_name', '') or getattr(ds, 'db_name', '') or f"tenant_{tenant_slug}_uat"

    _register_tenant_connection(alias, db_name=db_name, data_source=ds, tenant_id=tenant.id)
    set_tenant_db_alias(alias)

    org = Organization.objects.using(alias).filter(status='ACTIVE').first()
    print(f"[Tenant] Connected to alias '{alias}' (DB: '{db_name}'). Organization: '{org.name if org else 'N/A'}'")
    return alias, org, tenant


def find_excel_file(specified_path, candidate_patterns):
    """
    Finds an Excel file from specified path, or scans candidate directories/patterns using os.walk.
    """
    if specified_path and os.path.isfile(specified_path):
        return specified_path

    import fnmatch
    base_dirs = [
        os.path.join(BACKEND_DIR, 'media', 'november_sheets'),
        os.path.join(BACKEND_DIR, 'scripts', 'data'),
        os.path.join(BACKEND_DIR, 'media'),
        os.getcwd(),
        os.path.join(BACKEND_DIR, 'scripts'),
        os.path.join(BACKEND_DIR, '..', 'scratch'),
        r'C:\Users\khanf\.gemini\antigravity\brain',
    ]

    matches = []
    for bdir in base_dirs:
        if not os.path.isdir(bdir):
            continue
        for root, dirs, files in os.walk(bdir):
            for f in files:
                for pat in candidate_patterns:
                    if fnmatch.fnmatch(f, pat):
                        full_p = os.path.join(root, f)
                        matches.append(full_p)

    if matches:
        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    return None


def extract_november_videos(wb_path, sheet_name, category_code, program_name):
    """
    Extracts November 2026 YouTube videos from a workbook sheet.
    Returns dict: { 'YYYY-MM-DD': { 'volume': str, 'url': str, 'muscles': str, 'equipment': str } }
    """
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"Sheet '{sheet_name}' not found in workbook {wb_path}. Available: {wb.sheetnames}")

    sheet = wb[sheet_name]
    video_map = {}
    max_r = sheet.max_row or 1

    for r in range(2, max_r + 1):
        raw_date = sheet.cell(r, 2).value
        dt = None
        if hasattr(raw_date, 'year') and raw_date.year == 2026 and raw_date.month == 11:
            dt = raw_date.date() if hasattr(raw_date, 'date') else raw_date
        elif isinstance(raw_date, str) and raw_date.startswith('2026-11'):
            try:
                dt = datetime.datetime.strptime(raw_date[:10], '%Y-%m-%d').date()
            except ValueError:
                continue

        if not dt or dt.year != 2026 or dt.month != 11:
            continue

        date_str = dt.strftime('%Y-%m-%d')
        raw_vol = sheet.cell(r, 3).value
        vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()

        raw_url = str(sheet.cell(r, 4).value or '').strip()
        raw_muscles = str(sheet.cell(r, 5).value or '').strip()
        raw_equip = str(sheet.cell(r, 6).value or '').strip()

        if raw_url and 'youtu' in raw_url:
            video_map[date_str] = {
                'volume': vol_str,
                'url': raw_url,
                'muscles': raw_muscles,
                'equipment': raw_equip,
                'category_code': category_code,
                'program_name': program_name,
                'shot_date': dt,
            }

    return video_map


def extract_november_roster(wb_path):
    """
    Parses the November 2026 weekly trainer allocation roster from '02. Trainer allocation'.
    Returns dict: { (branch_code, shift_or_day, week_name): [trainer_name, ...] }
    """
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    sheet = wb['02. Trainer allocation']

    def parse_names(s):
        if not s or s == '-':
            return []
        parts = re.split(r'[,|&/]', str(s))
        res = []
        for p in parts:
            clean = p.strip()
            if clean.lower() == 'saaed':
                clean = 'Saeed'
            if clean and clean not in ['-', 'F-AT', 'S-AT']:
                res.append(clean)
        return res

    roster = {}
    weeks = ['Week 1', 'Week 2', 'Week 3', 'Week 4', 'Week 5']

    # Weekdays: rows 26 to 31
    weekday_rows = [
        (26, ('SWEAT_ANDHERI' or branches.get('SWEAT_HQ'), 'Mor')),
        (27, ('SWEAT_ANDHERI', 'Eve')),
        (28, ('SWEAT_MALAD', 'Mor')),
        (29, ('SWEAT_MALAD', 'Eve')),
        (30, ('SWEAT_GOREGAON', 'Mor')),
        (31, ('SWEAT_GOREGAON', 'Eve')),
    ]
    for r, (outlet, shift) in weekday_rows:
        for col_idx, w_name in enumerate(weeks, start=3):
            val = sheet.cell(r, col_idx).value
            roster[(outlet, shift, w_name)] = parse_names(val)

    # Weekends: rows 33 to 38
    weekend_rows = [
        (33, ('SWEAT_ANDHERI', 'Sat')),
        (34, ('SWEAT_ANDHERI', 'Sun')),
        (35, ('SWEAT_MALAD', 'Sat')),
        (36, ('SWEAT_MALAD', 'Sun')),
        (37, ('SWEAT_GOREGAON', 'Sat')),
        (38, ('SWEAT_GOREGAON', 'Sun')),
    ]
    for r, (outlet, day_name) in weekend_rows:
        for col_idx, w_name in enumerate(weeks, start=3):
            val = sheet.cell(r, col_idx).value
            roster[(outlet, day_name, w_name)] = parse_names(val)

    return roster


def get_roster_week_col(dt):
    """
    Maps a November 2026 date to the roster's Week 1-5 column.
    """
    day = dt.day
    is_weekend = dt.weekday() >= 5
    if not is_weekend:
        if 2 <= day <= 6:
            return 'Week 1'
        elif 9 <= day <= 13:
            return 'Week 2'
        elif 16 <= day <= 20:
            return 'Week 3'
        elif 23 <= day <= 27:
            return 'Week 4'
        elif day == 30:
            return 'Week 5'
    else:
        if day == 1:
            return 'Week 5'  # Column 7 'Sat & Sun | 31 & 01'
        elif day in [7, 8]:
            return 'Week 1'
        elif day in [14, 15]:
            return 'Week 2'
        elif day in [21, 22]:
            return 'Week 3'
        elif day in [28, 29]:
            return 'Week 4'
    return 'Week 1'


def get_or_create_trainer(alias, org, name_str, home_branch, trainer_by_name, dry_run=False):
    """
    Resolves an existing TrainerProfile, or creates a new trainer profile if missing.
    """
    name_clean = name_str.strip()
    key = name_clean.lower()
    
    # Common alias aliases
    if key == 'vincent' and 'vincet' in trainer_by_name:
        return trainer_by_name['vincet']
    if key == 'vincet' and 'vincent' in trainer_by_name:
        return trainer_by_name['vincent']

    if key in trainer_by_name:
        return trainer_by_name[key]

    # Split first / last name
    parts = name_clean.split()
    first_name = parts[0]
    last_name = " ".join(parts[1:]) if len(parts) > 1 else "Trainer"

    print(f"[Trainer] Trainer '{name_clean}' not found. Creating new trainer profile...")

    if dry_run:
        mock_tp = TrainerProfile(trainer_code=f"TRN-NEW-{first_name.upper()}")
        trainer_by_name[key] = mock_tp
        trainer_by_name[first_name.lower()] = mock_tp
        return mock_tp

    # Determine next TRN-xxx and EMP-xxx code
    all_codes = list(TrainerProfile.objects.using(alias).values_list('trainer_code', flat=True))
    max_num = 0
    for c in all_codes:
        if c and c.startswith('TRN-'):
            try:
                max_num = max(max_num, int(c.split('-')[1]))
            except ValueError:
                pass
    new_trn_code = f"TRN-{max_num + 1:03d}"
    new_emp_code = f"EMP-{max_num + 1:03d}"
    email = f"{first_name.lower()}.{last_name.lower().replace(' ', '')}@sweatfitness.local"

    # 1. TenantUser
    user = TenantUser.objects.using(alias).filter(email__iexact=email).first()
    if not user:
        user = TenantUser.objects.using(alias).create(
            organization=org,
            email=email,
            first_name=first_name,
            last_name=last_name,
            display_name=name_clean,
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=home_branch,
        )
        user.set_password('Sweat@2026!')
        user.save(using=alias)

    # 2. UserProfile
    user_prof, _ = UserProfile.objects.using(alias).get_or_create(
        user=user,
        defaults={
            'first_name_snapshot': first_name,
            'last_name_snapshot': last_name,
            'preferred_branch': home_branch,
        }
    )

    # 3. EmployeeProfile
    emp_prof, _ = EmployeeProfile.objects.using(alias).get_or_create(
        user_profile=user_prof,
        defaults={
            'organization': org,
            'employee_code': new_emp_code,
            'designation': 'Fitness Trainer',
            'employment_status': 'ACTIVE',
        }
    )

    # 4. TrainerProfile
    tp, _ = TrainerProfile.objects.using(alias).get_or_create(
        employee_profile=emp_prof,
        defaults={
            'trainer_code': new_trn_code,
            'bio': f'Certified Fitness Trainer ({name_clean})',
            'trainer_status': 'ACTIVE',
            'can_teach_all_specialties': True,
            'minimum_schedule_buffer_minutes': 15,
        }
    )

    # 5. UserBranch
    UserBranch.objects.using(alias).get_or_create(
        user=user,
        branch=home_branch,
        defaults={
            'relationship_type': 'EMPLOYMENT',
            'is_primary': True,
            'status': 'ACTIVE',
            'is_active': True,
        }
    )

    # 6. RoleAssignment (TRAINER)
    trainer_role = Role.objects.using(alias).filter(code='TRAINER').first()
    if trainer_role:
        RoleAssignment.objects.using(alias).get_or_create(
            user=user,
            role=trainer_role,
            defaults={
                'organization': org,
                'status': 'ACTIVE',
                'is_active': True,
            }
        )

    trainer_by_name[key] = tp
    trainer_by_name[first_name.lower()] = tp
    trainer_by_name[name_clean.lower()] = tp
    print(f"[Trainer] Created TrainerProfile: {tp.trainer_code} for {name_clean} (Branch: {home_branch.code})")
    return tp


def sync_content_items(alias, org, all_videos, dry_run=False):
    """
    Ensures each video exists in ClassContentItem and WorkoutContentItem without duplicating.
    Returns dict: { (category_code, date_str): ClassContentItem }
    """
    content_item_by_cat_date = {}

    for (cat_code, date_str), vinfo in all_videos.items():
        title = f"{vinfo['program_name']} - {vinfo['volume']} ({vinfo['muscles']})" if vinfo['volume'] else f"{vinfo['program_name']} - {vinfo['muscles']}"
        desc = f"Targeting {vinfo['muscles']}. Equipment: {vinfo['equipment'] or 'None'}."

        # 1. ClassContentItem
        existing_ci = ClassContentItem.objects.using(alias).filter(
            organization=org,
            external_url=vinfo['url']
        ).first()

        if not existing_ci:
            if not dry_run:
                existing_ci = ClassContentItem.objects.using(alias).create(
                    organization=org,
                    title=title,
                    description=desc,
                    content_type='VIDEO',
                    external_url=vinfo['url'],
                    status='ACTIVE'
                )
            else:
                existing_ci = ClassContentItem(
                    organization=org,
                    title=title,
                    description=desc,
                    content_type='VIDEO',
                    external_url=vinfo['url'],
                    status='ACTIVE'
                )
        content_item_by_cat_date[(cat_code, date_str)] = existing_ci

        # 2. WorkoutContentItem (for WOD player date-aware lookups)
        existing_wod = WorkoutContentItem.objects.using(alias).filter(
            raw_import_data__date=date_str,
            raw_import_data__category_code=cat_code
        ).first()

        if not existing_wod and not dry_run:
            WorkoutContentItem.objects.using(alias).create(
                organization=org,
                movement_name=title,
                description=desc,
                video_url=vinfo['url'],
                video_provider='YOUTUBE',
                content_kind='MOVEMENT',
                is_wod_eligible=True,
                classification_status='COMPLETE',
                status='ACTIVE',
                shot_date=vinfo['shot_date'],
                raw_import_data={
                    'date': date_str,
                    'volume': vinfo['volume'],
                    'muscles': vinfo['muscles'],
                    'equipment': vinfo['equipment'],
                    'category_code': cat_code,
                    'program': vinfo['program_name'],
                }
            )

    return content_item_by_cat_date


def generate_november_classes(alias, org, content_item_map, roster, dry_run=False):
    """
    Generates class occurrences, links videos, and allocates trainers strictly for November 2026.
    Protects all existing records (skips without overwriting or deleting).
    """
    # Load existing branches
    branches = {b.code: b for b in Branch.objects.using(alias).all()}
    b_andheri = branches.get('SWEAT_ANDHERI') or branches.get('SWEAT_HQ')
    b_malad = branches.get('SWEAT_MALAD')
    b_goregaon = branches.get('SWEAT_GOREGAON')

    if not b_andheri or not b_malad or not b_goregaon:
        raise ValueError(f"Missing required branches: Andheri={b_andheri}, Malad={b_malad}, Goregaon={b_goregaon}")

    # Load existing class templates
    templates = {t.code: t for t in ClassTemplate.objects.using(alias).select_related('category').all()}
    tpl_total = templates.get('TPL-PIL-TOT')
    tpl_stretch = templates.get('TPL-PIL-STR')
    tpl_athletic = templates.get('TPL-PIL-ATH')
    tpl_bootcamp = templates.get('TPL-BTC-TOT')

    if not tpl_total or not tpl_stretch or not tpl_athletic or not tpl_bootcamp:
        raise ValueError("Missing required templates (TPL-PIL-TOT, TPL-PIL-STR, TPL-PIL-ATH, TPL-BTC-TOT).")

    # Load existing trainers and index by name variants
    trainer_by_name = {}
    for tp in TrainerProfile.objects.using(alias).select_related('employee_profile__user_profile__user').all():
        user = getattr(getattr(getattr(tp, 'employee_profile', None), 'user_profile', None), 'user', None)
        if user:
            if user.full_name:
                trainer_by_name[user.full_name.lower().strip()] = tp
                trainer_by_name[user.full_name.split()[0].lower().strip()] = tp
            if user.first_name:
                trainer_by_name[user.first_name.lower().strip()] = tp
            if user.display_name:
                trainer_by_name[user.display_name.lower().strip()] = tp
        if tp.trainer_code:
            trainer_by_name[tp.trainer_code.lower().strip()] = tp

    # Schedule Slots from 03. Class Time sheet (November 2026):
    weekday_pilates_mwf = [
        (datetime.time(6, 30), datetime.time(7, 20), tpl_total),
        (datetime.time(7, 30), datetime.time(8, 20), tpl_stretch),
        (datetime.time(8, 30), datetime.time(9, 20), tpl_total),
        (datetime.time(9, 30), datetime.time(10, 20), tpl_stretch),
        (datetime.time(11, 0), datetime.time(11, 50), tpl_total),
        (datetime.time(12, 0), datetime.time(12, 50), tpl_stretch),
        (datetime.time(13, 30), datetime.time(14, 20), tpl_total),
        (datetime.time(14, 30), datetime.time(15, 20), tpl_stretch),
        (datetime.time(15, 30), datetime.time(16, 20), tpl_total),
        (datetime.time(17, 0), datetime.time(17, 50), tpl_total),
        (datetime.time(18, 0), datetime.time(18, 50), tpl_stretch),
        (datetime.time(19, 0), datetime.time(19, 50), tpl_total),
        (datetime.time(20, 0), datetime.time(20, 50), tpl_stretch),
        (datetime.time(21, 30), datetime.time(22, 20), tpl_stretch),
    ]

    weekday_pilates_tt = [
        (datetime.time(6, 30), datetime.time(7, 20), tpl_stretch),
        (datetime.time(7, 30), datetime.time(8, 20), tpl_total),
        (datetime.time(8, 30), datetime.time(9, 20), tpl_stretch),
        (datetime.time(9, 30), datetime.time(10, 20), tpl_total),
        (datetime.time(11, 0), datetime.time(11, 50), tpl_stretch),
        (datetime.time(12, 0), datetime.time(12, 50), tpl_total),
        (datetime.time(13, 30), datetime.time(14, 20), tpl_stretch),
        (datetime.time(14, 30), datetime.time(15, 20), tpl_total),
        (datetime.time(15, 30), datetime.time(16, 20), tpl_stretch),
        (datetime.time(17, 0), datetime.time(17, 50), tpl_stretch),
        (datetime.time(18, 0), datetime.time(18, 50), tpl_total),
        (datetime.time(19, 0), datetime.time(19, 50), tpl_stretch),
        (datetime.time(20, 0), datetime.time(20, 50), tpl_total),
        (datetime.time(21, 30), datetime.time(22, 20), tpl_total),
    ]

    weekend_pilates_andheri_malad = [
        (datetime.time(8, 0), datetime.time(8, 50), tpl_stretch),
        (datetime.time(9, 0), datetime.time(9, 50), tpl_total),
        (datetime.time(10, 0), datetime.time(10, 50), tpl_stretch),
        (datetime.time(11, 30), datetime.time(12, 20), tpl_total),
        (datetime.time(12, 30), datetime.time(13, 20), tpl_stretch),
        (datetime.time(13, 30), datetime.time(14, 20), tpl_total),
        (datetime.time(15, 0), datetime.time(15, 50), tpl_stretch),
        (datetime.time(16, 0), datetime.time(16, 50), tpl_total),
    ]

    goregaon_weekday_bootcamp = [
        (datetime.time(6, 30), datetime.time(7, 20), tpl_bootcamp),
        (datetime.time(7, 30), datetime.time(8, 20), tpl_bootcamp),
        (datetime.time(8, 30), datetime.time(9, 20), tpl_bootcamp),
        (datetime.time(9, 30), datetime.time(10, 20), tpl_bootcamp),
        (datetime.time(11, 0), datetime.time(11, 50), tpl_bootcamp),
        (datetime.time(17, 0), datetime.time(17, 50), tpl_bootcamp),
        (datetime.time(18, 0), datetime.time(18, 50), tpl_bootcamp),
        (datetime.time(19, 0), datetime.time(19, 50), tpl_bootcamp),
        (datetime.time(20, 0), datetime.time(20, 50), tpl_bootcamp),
        (datetime.time(21, 30), datetime.time(22, 20), tpl_bootcamp),
    ]

    goregaon_weekend_pilates = [
        (datetime.time(7, 30), datetime.time(8, 20), tpl_total),
        (datetime.time(8, 30), datetime.time(9, 20), tpl_stretch),
        (datetime.time(11, 0), datetime.time(11, 50), tpl_total),
        (datetime.time(13, 30), datetime.time(14, 20), tpl_stretch),
        (datetime.time(14, 30), datetime.time(15, 20), tpl_athletic),
        (datetime.time(15, 30), datetime.time(16, 20), tpl_stretch),
    ]

    goregaon_weekend_bootcamp = [
        (datetime.time(7, 30), datetime.time(8, 20), tpl_bootcamp),
        (datetime.time(8, 30), datetime.time(9, 20), tpl_bootcamp),
        (datetime.time(11, 0), datetime.time(11, 50), tpl_bootcamp),
        (datetime.time(14, 30), datetime.time(15, 20), tpl_bootcamp),
        (datetime.time(15, 30), datetime.time(16, 20), tpl_bootcamp),
    ]

    created_count = 0
    skipped_count = 0
    assigned_video_count = 0
    assigned_trainer_count = 0
    skipped_trainer_count = 0

    print("\n--- Processing November 2026 Class Schedule (Nov 1 to Nov 30) ---")

    # Track allocated trainer IDs per time-slot to prevent double-booking across branches
    busy_trainers_by_slot = set()

    for day in range(1, 31):
        cur_date = datetime.date(2026, 11, day)
        date_str = cur_date.strftime('%Y-%m-%d')
        weekday = cur_date.weekday()
        is_weekend = weekday >= 5
        week_name = get_roster_week_col(cur_date)

        day_slots = []

        # 1. Sweat Andheri
        if not is_weekend:
            slots = weekday_pilates_mwf if weekday in [0, 2, 4] else weekday_pilates_tt
            for idx, (st, et, tpl) in enumerate(slots):
                day_slots.append((b_andheri, st, et, tpl, 12, idx))
        else:
            for idx, (st, et, tpl) in enumerate(weekend_pilates_andheri_malad):
                day_slots.append((b_andheri, st, et, tpl, 12, idx))

        # 2. Sweat Malad
        if not is_weekend:
            slots = weekday_pilates_mwf if weekday in [0, 2, 4] else weekday_pilates_tt
            for idx, (st, et, tpl) in enumerate(slots):
                day_slots.append((b_malad, st, et, tpl, 8, idx))
        else:
            for idx, (st, et, tpl) in enumerate(weekend_pilates_andheri_malad):
                day_slots.append((b_malad, st, et, tpl, 8, idx))

        # 3. Sweat Goregaon (Pilates + Bootcamp)
        if not is_weekend:
            p_slots = weekday_pilates_mwf if weekday in [0, 2, 4] else weekday_pilates_tt
            for idx, (st, et, tpl) in enumerate(p_slots):
                day_slots.append((b_goregaon, st, et, tpl, 12, idx))
            for idx, (st, et, tpl) in enumerate(goregaon_weekday_bootcamp):
                day_slots.append((b_goregaon, st, et, tpl, 16, idx))
        else:
            for idx, (st, et, tpl) in enumerate(goregaon_weekend_pilates):
                day_slots.append((b_goregaon, st, et, tpl, 12, idx))
            for idx, (st, et, tpl) in enumerate(goregaon_weekend_bootcamp):
                day_slots.append((b_goregaon, st, et, tpl, 16, idx))

        # Process all candidate slots for this day
        import zoneinfo
        # Process all candidate slots for this day
        for branch, st, et, tpl, cap, slot_idx in day_slots:
            branch_tz = zoneinfo.ZoneInfo(getattr(branch, 'timezone', None) or 'Asia/Kolkata')
            start_aware = timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=branch_tz)
            end_aware = timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=branch_tz)

            # Duplicate protection: check if class occurrence already exists
            existing_occ = ClassOccurrence.objects.using(alias).filter(
                branch=branch,
                occurrence_date=cur_date,
                start_at=start_aware,
                class_template=tpl
            ).first()

            if existing_occ:
                skipped_count += 1
                occ = existing_occ
            else:
                if not dry_run:
                    occ = ClassOccurrence.objects.using(alias).create(
                        class_template=tpl,
                        branch=branch,
                        occurrence_date=cur_date,
                        start_at=start_aware,
                        end_at=end_aware,
                        capacity=cap,
                        trial_capacity=min(2, cap),
                        waitlist_capacity=5,
                        status='OPEN'
                    )
                else:
                    occ = ClassOccurrence(
                        class_template=tpl,
                        branch=branch,
                        occurrence_date=cur_date,
                        start_at=start_aware,
                        end_at=end_aware,
                        capacity=cap,
                        status='OPEN'
                    )
                created_count += 1

            # -------------------------------------------------------------
            # YouTube Video mapping
            # -------------------------------------------------------------
            cat_code = tpl.category.code if tpl.category else None
            ci = content_item_map.get((cat_code, date_str))

            if occ and ci and not dry_run:
                has_assignment = ClassContentAssignment.objects.using(alias).filter(
                    occurrence=occ,
                    content_item=ci
                ).exists()

                if not has_assignment:
                    ClassContentAssignment.objects.using(alias).create(
                        occurrence=occ,
                        content_item=ci,
                        assignment_method='DATE_MAPPED',
                        status='ACTIVE'
                    )
                    assigned_video_count += 1
            elif dry_run and ci:
                assigned_video_count += 1

            # -------------------------------------------------------------
            # Trainer Allocation
            # -------------------------------------------------------------
            if not is_weekend:
                shift_key = 'Mor' if st.hour < 14 else 'Eve'
                pool = roster.get((branch.code, shift_key, week_name), [])
            else:
                day_key = 'Sat' if weekday == 5 else 'Sun'
                pool = roster.get((branch.code, day_key, week_name), [])

            assigned_trainer_name = None
            if pool:
                if branch.code == 'SWEAT_GOREGAON':
                    # Goregaon parallel sessions:
                    if tpl.code == 'TPL-BTC-TOT' and len(pool) > 1:
                        assigned_trainer_name = pool[1]
                    else:
                        assigned_trainer_name = pool[0]
                else:
                    # Alternating slots between shift trainers
                    assigned_trainer_name = pool[slot_idx % len(pool)]

            # Double-booking collision resolution
            target_tp = None
            if assigned_trainer_name:
                cand_tp = get_or_create_trainer(alias, org, assigned_trainer_name, branch, trainer_by_name, dry_run=dry_run)
                slot_booking_key = (cur_date, st, cand_tp.id if cand_tp.id else cand_tp.trainer_code)

                if slot_booking_key in busy_trainers_by_slot and len(pool) > 1:
                    # Switch to alternate trainer in pool
                    for alt_name in pool:
                        if alt_name != assigned_trainer_name:
                            alt_tp = get_or_create_trainer(alias, org, alt_name, branch, trainer_by_name, dry_run=dry_run)
                            alt_key = (cur_date, st, alt_tp.id if alt_tp.id else alt_tp.trainer_code)
                            if alt_key not in busy_trainers_by_slot:
                                target_tp = alt_tp
                                busy_trainers_by_slot.add(alt_key)
                                break
                    if not target_tp:
                        target_tp = cand_tp
                else:
                    target_tp = cand_tp
                    busy_trainers_by_slot.add(slot_booking_key)

            if occ and target_tp:
                if not dry_run:
                    # Check if trainer assignment already exists (duplicate protection)
                    has_cot = ClassOccurrenceTrainer.objects.using(alias).filter(
                        occurrence=occ
                    ).exists()

                    if not has_cot:
                        try:
                            ClassOccurrenceTrainer.objects.using(alias).create(
                                occurrence=occ,
                                trainer_profile=target_tp,
                                trainer_role='LEAD',
                                status='CONFIRMED',
                            )
                            assigned_trainer_count += 1
                        except Exception as e:
                            # Try alternate non-conflicting trainer from roster pool
                            allocated = False
                            for alt_name in pool:
                                alt_tp = get_or_create_trainer(alias, org, alt_name, branch, trainer_by_name, dry_run=dry_run)
                                if alt_tp != target_tp:
                                    try:
                                        ClassOccurrenceTrainer.objects.using(alias).create(
                                            occurrence=occ,
                                            trainer_profile=alt_tp,
                                            trainer_role='LEAD',
                                            status='CONFIRMED',
                                        )
                                        assigned_trainer_count += 1
                                        allocated = True
                                        break
                                    except Exception:
                                        pass
                            if not allocated:
                                skipped_trainer_count += 1
                    else:
                        skipped_trainer_count += 1
                else:
                    assigned_trainer_count += 1

    print(f"\n[Summary] Class Occurrences -> Created: {created_count}, Skipped (Already existed): {skipped_count}")
    print(f"[Summary] Video Assignments -> Mapped: {assigned_video_count}")
    print(f"[Summary] Trainer Allotment -> Assigned: {assigned_trainer_count}, Skipped (Already assigned): {skipped_trainer_count}")
    return created_count, skipped_count, assigned_video_count, assigned_trainer_count


def main():
    parser = argparse.ArgumentParser(description="Upload November 2026 Classes, Videos, and Trainer Allotments for a Tenant")
    parser.add_argument('--tenant-slug', type=str, default='sweat', help="Slug of the tenant (e.g. 'sweat')")
    parser.add_argument('--dry-run', action='store_true', help="Perform dry run without committing database changes")
    parser.add_argument('--class-time-file', type=str, default=None, help="Path to Excel file containing '02. Trainer allocation' and '03. Class Time'")
    parser.add_argument('--pilates-file', type=str, default=None, help="Path to Excel file containing Pilates workout videos")
    parser.add_argument('--bootcamp-file', type=str, default=None, help="Path to Excel file containing Bootcamp workout videos")
    args = parser.parse_args()

    print(f"=== NOVEMBER 2026 CLASS, VIDEO & TRAINER ALLOTMENT UPLOADER ===")
    print(f"Tenant slug: {args.tenant_slug}")
    print(f"Dry run mode: {args.dry_run}")

    # 1. Resolve tenant connection
    alias, org, tenant = resolve_tenant_connection(args.tenant_slug)

    # 2. Locate Excel files
    f_time = find_excel_file(args.class_time_file, ['*media*476a5bc2*.xlsx', '*media*44d38651*.xlsx', '*01_SWEAT_Roster*.xlsx', '*roster*.xlsx', '*class*.xlsx'])
    f_pil = find_excel_file(args.pilates_file, ['*media*f755e6e8*.xlsx', '*media*bebd723a*.xlsx', '*02_SWEAT_Pilates*.xlsx', '*pilates*.xlsx'])
    f_btc = find_excel_file(args.bootcamp_file, ['*media*d8db2667*.xlsx', '*media*e83556d4*.xlsx', '*03_SWEAT_Bootcamp*.xlsx', '*bootcamp*.xlsx'])

    print(f"\nUsing Excel Files:")
    print(f" - Roster & Class Time Sheet: {f_time}")
    print(f" - Pilates Videos:            {f_pil}")
    print(f" - Bootcamp Videos:           {f_btc}")

    if not f_time or not f_pil or not f_btc:
        print("[ERROR] Could not find one or more required Excel files. Please pass --class-time-file, --pilates-file, --bootcamp-file explicitly.")
        sys.exit(1)

    # 3. Extract November video mappings
    print("\n--- Extracting November YouTube Video Mappings ---")
    all_videos = {}

    v_total = extract_november_videos(f_pil, '01. Sweat Total', 'PILATES_TOTAL', 'Sweat Total')
    v_stretch = extract_november_videos(f_pil, '02. Sweat Stretch ', 'PILATES_STRETCH', 'Sweat Stretch')
    v_athletic = extract_november_videos(f_pil, '03. Sweat Athletic', 'PILATES_ATHLETIC', 'Sweat Athletic')
    v_bootcamp = extract_november_videos(f_btc, '01. Total', 'BOOTCAMP_TOTAL', 'Sweat Bootcamp')

    print(f"Extracted Videos: Total={len(v_total)}, Stretch={len(v_stretch)}, Athletic={len(v_athletic)}, Bootcamp={len(v_bootcamp)}")

    for d, info in v_total.items():
        all_videos[('PILATES_TOTAL', d)] = info
    for d, info in v_stretch.items():
        all_videos[('PILATES_STRETCH', d)] = info
    for d, info in v_athletic.items():
        all_videos[('PILATES_ATHLETIC', d)] = info
    for d, info in v_bootcamp.items():
        all_videos[('BOOTCAMP_TOTAL', d)] = info

    # 4. Extract November Trainer Roster
    print("\n--- Extracting November Trainer Roster ---")
    roster = extract_november_roster(f_time)
    print(f"Parsed Roster: {len(roster)} branch-shift weekly rotation slots.")

    # 5. Sync Content Items into Database
    print("\n--- Synchronizing Class Content Items ---")
    content_item_map = sync_content_items(alias, org, all_videos, dry_run=args.dry_run)

    # 6. Generate November Classes, Video Mappings & Trainer Allotments
    if not args.dry_run:
        with transaction.atomic(using=alias):
            generate_november_classes(alias, org, content_item_map, roster, dry_run=False)
            print("\n[SUCCESS] November 2026 Classes, Videos, and Trainer Allotments uploaded successfully!")
    else:
        generate_november_classes(alias, org, content_item_map, roster, dry_run=True)
        print("\n[DRY-RUN COMPLETE] No changes were written to the database.")


if __name__ == '__main__':
    main()
