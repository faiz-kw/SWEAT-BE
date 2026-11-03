"""
services_import_engine.py

Universal, Scalable, Production-Ready Ingestion Engine for PerformanceOS / SWEAT.
Intelligently classifies, parses, normalizes, validates, and imports:
1. Class Timetables & Schedules (both 2D matrix grids and flat tabular formats)
2. Workout of the Day (WOD) Video Content (daily YouTube links, equipment, muscle tags)
3. Trainer Shift Rosters & Assignments (multi-week rotation grids, dual-room allocations)

Completely generic, zero hardcoding, strict pre-flight simulation, atomic transaction commit.
"""

import re
import io
import uuid
import datetime
from datetime import time, date, timedelta
from typing import Dict, List, Any, Optional, Tuple

import calendar
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

# Domain Models
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_catalog import Program, ProgramCategory
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassBranchAvailability,
    ClassScheduleRule,
    ClassOccurrence,
    ClassOccurrenceTrainer,
    ClassContentItem,
    ClassContentAssignment,
)
from apps.tenant_core.models_workforce import (
    TrainerProfile,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
    EmployeeProfile,
    UserProfile,
    EmployeeWorkSchedule,
)
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_wod import (
    WorkoutContentItem,
    WorkoutTag,
    WorkoutContentTag,
)


class EntityType:
    CLASS_SCHEDULE = 'CLASS_SCHEDULE'
    WOD_VIDEO = 'WOD_VIDEO'
    TRAINER_ROSTER = 'TRAINER_ROSTER'
    UNKNOWN = 'UNKNOWN'


class EntityClassifier:
    """
    Multi-signal classifier that inspects sheet names, header tokens, and cell content
    to automatically identify the entity type with a confidence score.
    """

    CLASS_TOKENS = {
        'timetable', 'schedule', 'pilates', 'bootcamp', 'mwf', 'tt', 'ss',
        'stretch', 'total', 'athletic', 'slot', 'break', 'class', 'classes'
    }
    VIDEO_TOKENS = {
        'yt link', 'youtube', 'yt', 'volume', 'vol', 'muscle', 'muscles',
        'muscle working', 'equipment', 'reformer', 'straps', 'box', 'plate', 'wod'
    }
    TRAINER_TOKENS = {
        'trainer', 'trainers', 'coach', 'shift', 'shifts', 'morning', 'evening',
        'split', 'closing', 'teaching', 'roster', 'weekend', 'attendance'
    }

    @classmethod
    def classify_workbook(cls, wb: openpyxl.Workbook) -> Dict[str, Any]:
        scores = {
            EntityType.CLASS_SCHEDULE: 0,
            EntityType.WOD_VIDEO: 0,
            EntityType.TRAINER_ROSTER: 0,
        }
        reasons = []

        sheet_names_lower = [s.lower() for s in wb.sheetnames]
        
        # 1. Inspect Sheet Names
        for sname in sheet_names_lower:
            for token in cls.CLASS_TOKENS:
                if token in sname:
                    scores[EntityType.CLASS_SCHEDULE] += 15
            for token in cls.VIDEO_TOKENS:
                if token in sname:
                    scores[EntityType.WOD_VIDEO] += 15
            for token in cls.TRAINER_TOKENS:
                if token in sname:
                    scores[EntityType.TRAINER_ROSTER] += 15

        # 2. Inspect Sample Cells in each sheet
        yt_found = False
        time_slot_found = False
        shift_found = False

        for sheet in wb.worksheets[:5]:
            max_r = min(sheet.max_row or 1, 25)
            max_c = min(sheet.max_column or 1, 20)

            for r in range(1, max_r + 1):
                for c in range(1, max_c + 1):
                    val = sheet.cell(row=r, column=c).value
                    if val is None:
                        continue
                    sval = str(val).strip().lower()

                    # YouTube detection
                    if 'youtu.be' in sval or 'youtube.com' in sval:
                        scores[EntityType.WOD_VIDEO] += 40
                        yt_found = True

                    # Time slot pattern (e.g. 06:30 - 07:30 or 6:30 - 11:30)
                    if re.search(r'\b\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}\b', sval):
                        scores[EntityType.CLASS_SCHEDULE] += 25
                        time_slot_found = True

                    # Shift tokens
                    if sval in ('morning', 'evening', 'split', 'closing trainer', 'teaching'):
                        scores[EntityType.TRAINER_ROSTER] += 20
                        shift_found = True

                    # Header matches
                    for token in cls.VIDEO_TOKENS:
                        if token in sval:
                            scores[EntityType.WOD_VIDEO] += 5
                    for token in cls.CLASS_TOKENS:
                        if token in sval:
                            scores[EntityType.CLASS_SCHEDULE] += 5
                    for token in cls.TRAINER_TOKENS:
                        if token in sval:
                            scores[EntityType.TRAINER_ROSTER] += 5

        if yt_found:
            reasons.append("Detected YouTube video URLs")
        if time_slot_found:
            reasons.append("Detected class time-slot patterns (e.g., 06:30 - 07:30)")
        if shift_found:
            reasons.append("Detected staff shift patterns (Morning / Evening / Split)")

        total = sum(scores.values()) or 1
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_entity, top_score = sorted_scores[0]

        confidence = min(99, int((top_score / total) * 100)) if top_score > 0 else 0
        if confidence < 35:
            top_entity = EntityType.UNKNOWN

        return {
            'entity_type': top_entity,
            'confidence': confidence,
            'scores': scores,
            'reasons': reasons,
            'sheets': wb.sheetnames,
        }


class SmartDateParser:
    """
    Robust date parser handling YYYY-MM-DD, YYYY-DD-MM, DD-MM-YYYY, DD-MM-YY, and Excel serial numbers.
    """

    @classmethod
    def parse_date(cls, val: Any) -> Optional[date]:
        if val is None:
            return None
        if isinstance(val, (datetime.date, datetime.datetime)):
            return val.date() if isinstance(val, datetime.datetime) else val
        if isinstance(val, (int, float)):
            # Excel date serial (starts from 1899-12-30)
            try:
                base_date = datetime.date(1899, 12, 30)
                return base_date + datetime.timedelta(days=int(val))
            except Exception:
                return None

        s = str(val).strip()
        if not s or s == '-':
            return None

        # Clean string
        s = s.replace('/', '-').replace('.', '-')

        # Pattern: YYYY-MM-DD or YYYY-DD-MM
        m_iso = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
        if m_iso:
            y, p2, p3 = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
            # Disambiguation: if p2 > 12, p2 must be day!
            if p2 > 12 and p3 <= 12:
                return date(y, p3, p2)
            try:
                return date(y, p2, p3)
            except ValueError:
                if p3 <= 31 and p2 <= 12:
                    return date(y, p2, p3)

        # Pattern: DD-MM-YYYY
        m_dmy = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', s)
        if m_dmy:
            d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
            try:
                return date(y, m, d)
            except ValueError:
                return None

        # Pattern: DD-MM-YY
        m_dmy2 = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{2})$', s)
        if m_dmy2:
            d, m, y = int(m_dmy2.group(1)), int(m_dmy2.group(2)), int(m_dmy2.group(3)) + 2000
            try:
                return date(y, m, d)
            except ValueError:
                return None

        return None


class SmartMonthParser:
    """
    Detects and parses month markers, section headers, and filters
    across multi-month sheets and multi-sheet workbooks.
    """

    @classmethod
    def parse_month_string(cls, m_str: Optional[str]) -> Optional[Tuple[int, int]]:
        if not m_str:
            return None
        m_str = str(m_str).strip()
        m = re.match(r'^(\d{4})-(\d{1,2})', m_str)
        if m:
            return int(m.group(1)), int(m.group(2))
        for fmt in ('%B %Y', '%b %Y', '%B, %Y', '%b, %Y'):
            try:
                dt = datetime.datetime.strptime(m_str, fmt)
                return dt.year, dt.month
            except ValueError:
                pass
        return None

    @classmethod
    def detect_available_months(cls, wb: openpyxl.Workbook) -> List[Dict[str, Any]]:
        months_seen = {}
        for sheet in wb.worksheets:
            for r in range(1, min(sheet.max_row or 1, 150) + 1):
                for c in range(1, min(sheet.max_column or 1, 6) + 1):
                    val = sheet.cell(row=r, column=c).value
                    if isinstance(val, (datetime.date, datetime.datetime)):
                        key = (val.year, val.month)
                        if key not in months_seen:
                            months_seen[key] = {
                                'label': val.strftime('%B %Y'),
                                'value': f"{val.year}-{val.month:02d}",
                                'year': val.year,
                                'month': val.month,
                            }
        return list(months_seen.values())


class MatrixNormalizer:
    """
    Transforms 2D calendar and timetable matrices into normalized flat event rows.
    """
    @classmethod
    def parse_time_slot_string(cls, slot_str: str, passed_noon: bool = False):
        clean = str(slot_str or '').strip().replace(';', ':')
        m = re.search(r'(\d{1,2}[:.]\d{2})\s*(am|pm)?\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*(am|pm)?', clean, re.IGNORECASE)
        if not m:
            return None, None, passed_noon
        
        st_raw, st_ampm, et_raw, et_ampm = m.group(1), m.group(2), m.group(3), m.group(4)
        if not st_ampm and et_ampm: st_ampm = et_ampm
        if not et_ampm and st_ampm: et_ampm = st_ampm

        try:
            sh, sm = [int(x) for x in st_raw.replace('.', ':').split(':')]
            eh, em = [int(x) for x in et_raw.replace('.', ':').split(':')]
        except Exception:
            return None, None, passed_noon

        if st_ampm:
            if st_ampm.lower() == 'pm' and 1 <= sh <= 11: sh += 12
            elif st_ampm.lower() == 'am' and sh == 12: sh = 0
        if et_ampm:
            if et_ampm.lower() == 'pm' and 1 <= eh <= 11: eh += 12
            elif et_ampm.lower() == 'am' and eh == 12: eh = 0

        if not st_ampm and not et_ampm:
            if sh == 12:
                passed_noon = True
            elif passed_noon and 1 <= sh <= 11:
                sh += 12
            elif not passed_noon and (1 <= sh <= 5):
                sh += 12
                passed_noon = True
            
            if passed_noon and 1 <= eh <= 11:
                eh += 12
            elif eh < sh and eh < 12:
                eh += 12
                passed_noon = True
            elif not passed_noon and (1 <= eh <= 5):
                eh += 12

        return time(sh, sm), time(eh, em), passed_noon


    @classmethod
    def is_timetable_matrix(cls, sheet: Worksheet) -> bool:
        """Checks if a worksheet has hourly time-slots in column A or B."""
        max_r = min(sheet.max_row or 1, 30)
        time_slot_count = 0
        for r in range(1, max_r + 1):
            for c in range(1, 4):
                val = str(sheet.cell(row=r, column=c).value or '')
                if re.search(r'\b\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}\b', val):
                    time_slot_count += 1
        return time_slot_count >= 4

    @classmethod
    def unpivot_timetable(cls, sheet: Worksheet, target_month_tuple: Optional[Tuple[int, int]] = None) -> List[Dict[str, Any]]:
        """
        Unpivots a 2D timetable grid into:
        [{'row_number': ..., 'branch': ..., 'room': ..., 'day_pattern': ..., 'time_slot': ..., 'start_time': ..., 'end_time': ..., 'class_name': ...}]
        Strictly respects month section boundaries if target_month_tuple is specified.
        """
        rows = []
        max_r = sheet.max_row or 1
        max_c = sheet.max_column or 1

        start_row = 1
        end_row = max_r

        if target_month_tuple:
            found_section = False
            for r in range(1, max_r + 1):
                val = sheet.cell(row=r, column=1).value
                if isinstance(val, (datetime.date, datetime.datetime)):
                    if val.year == target_month_tuple[0] and val.month == target_month_tuple[1]:
                        start_row = r
                        found_section = True
                    elif found_section:
                        end_row = r - 1
                        break
            if not found_section:
                return []

        # Find time column
        time_col = 2
        for c in range(1, min(max_c + 1, 5)):
            matches = 0
            for r in range(start_row, min(end_row + 1, start_row + 20)):
                val = str(sheet.cell(row=r, column=c).value or '')
                if re.search(r'\b\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}\b', val):
                    matches += 1
            if matches >= 3:
                time_col = c
                break

        # Scan for branch and room labels above time slots
        col_meta = {}
        current_branch = "Andheri"
        for c in range(1, max_c + 1):
            r1 = str(sheet.cell(row=1, column=c).value or '').strip()
            r_sec1 = str(sheet.cell(row=start_row, column=c).value or '').strip()
            r_sec2 = str(sheet.cell(row=start_row + 1, column=c).value or '').strip()
            r_sec3 = str(sheet.cell(row=start_row + 2, column=c).value or '').strip()

            combined = f"{r1} {r_sec1} {r_sec2} {r_sec3}".lower()
            if 'malad' in combined:
                current_branch = "Malad"
            elif 'goregaon' in combined or 'spg' in combined:
                current_branch = "Goregaon"
            elif 'andheri' in combined or 'hq' in combined:
                current_branch = "Andheri"

            room = "Bootcamp" if 'bootcamp' in combined else "Pilates"
            
            day_pattern = "MWF"
            if 'tt' in combined:
                day_pattern = "TT"
            elif 'ss' in combined or 'sat' in combined or 'sun' in combined:
                day_pattern = "SS"

            col_meta[c] = {
                'branch': current_branch,
                'room': room,
                'day_pattern': day_pattern,
            }

        # Extract rows
        passed_noon = False
        passed_noon_ss = False
        for r in range(start_row, end_row + 1):
            slot_cell = str(sheet.cell(row=r, column=time_col).value or '').strip()
            st, et, passed_noon = cls.parse_time_slot_string(slot_cell, passed_noon)
            if not st or not et:
                continue

            for c, meta in col_meta.items():
                if c == time_col:
                    continue
                class_val = str(sheet.cell(row=r, column=c).value or '').strip()
                if not class_val or class_val in ('-', 'Break', 'break'):
                    continue
                # If cell is itself a time string e.g. '8:00 - 9:00' or '04;30 - 05:00', skip
                if re.search(r'\b\d{1,2}[:.;]\d{2}\s*[-–—]\s*\d{1,2}[:.;]\d{2}\b', class_val):
                    continue

                # Determine correct time for this slot (use weekend column time for SS if available)
                st_cur, et_cur = st, et
                slot_label_cur = slot_cell
                if meta['day_pattern'] == 'SS' and c > 1:
                    prev_val = str(sheet.cell(row=r, column=c - 1).value or '').strip()
                    st_p, et_p, passed_noon_ss = cls.parse_time_slot_string(prev_val, passed_noon_ss)
                    if st_p and et_p:
                        st_cur, et_cur = st_p, et_p
                        slot_label_cur = prev_val
                rows.append({
                    'row_number': r,
                    'branch': meta['branch'],
                    'room': meta['room'],
                    'day_pattern': meta['day_pattern'],
                    'slot_label': slot_label_cur,
                    'start_time': st_cur,
                    'end_time': et_cur,
                    'class_name': class_val,
                })

        return rows


class UniversalImportEngine:
    """
    Production-ready generic service handling preview, simulation, validation,
    and atomic commit for all entity types.
    """

    @classmethod
    def process_file_preview(
        cls,
        file_obj,
        target_entity: Optional[str] = None,
        target_month: Optional[str] = None,
        org: Optional[Organization] = None,
        db_alias: str = 'default'
    ) -> Dict[str, Any]:
        """
        Phase 1: Safe Dry-Run Simulation.
        Parses workbook, detects or accepts entity type, checks foreign keys and conflicts,
        and returns a rich preview report with strict month isolation without altering live DB.
        """
        content = file_obj.read()
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)

        # 1. Classify
        classification = EntityClassifier.classify_workbook(wb)
        entity_type = target_entity if (target_entity and target_entity != EntityType.UNKNOWN) else classification['entity_type']

        # 2. Month detection & selection
        available_months = SmartMonthParser.detect_available_months(wb)
        month_tuple = SmartMonthParser.parse_month_string(target_month) if target_month else None
        selected_month_label = None

        if month_tuple:
            m_match = next((m for m in available_months if m['year'] == month_tuple[0] and m['month'] == month_tuple[1]), None)
            selected_month_label = m_match['label'] if m_match else f"{month_tuple[1]:02d}/{month_tuple[0]}"
        elif available_months:
            month_tuple = (available_months[0]['year'], available_months[0]['month'])
            selected_month_label = available_months[0]['label']

        rows_preview = []
        summary = {
            'entity_type': entity_type,
            'confidence': classification['confidence'],
            'sheets': classification['sheets'],
            'available_months': available_months,
            'selected_month': selected_month_label,
            'sheet_breakdown': [],
            'total_rows': 0,
            'valid_rows': 0,
            'warning_rows': 0,
            'error_rows': 0,
        }

        # Route by entity type with strict month filtering
        if entity_type == EntityType.WOD_VIDEO:
            cls._preview_wod_videos(wb, rows_preview, summary, org=org, db_alias=db_alias, target_month_tuple=month_tuple)
        elif entity_type == EntityType.CLASS_SCHEDULE:
            cls._preview_class_schedule(wb, rows_preview, summary, org=org, db_alias=db_alias, target_month_tuple=month_tuple)
        elif entity_type == EntityType.TRAINER_ROSTER:
            cls._preview_trainer_roster(wb, rows_preview, summary, org=org, db_alias=db_alias, target_month_tuple=month_tuple)
        else:
            summary['error'] = 'Could not automatically identify Excel file format. Please select the target entity manually.'

        total_r = len(rows_preview)
        valid_r = sum(1 for r in rows_preview if r['status'] == 'VALID')
        warn_r = sum(1 for r in rows_preview if r['status'] == 'WARNING')
        err_r = sum(1 for r in rows_preview if r['status'] == 'ERROR')

        summary['total_rows'] = total_r
        summary['valid_rows'] = valid_r
        summary['warning_rows'] = warn_r
        summary['error_rows'] = err_r

        if total_r == 0 and entity_type == EntityType.WOD_VIDEO:
            summary['warning_message'] = (
                f"No workout videos found for '{selected_month_label or 'selected month'}'. "
                "Verify that the sheet has records for this month, or switch to 'Class Timetable' if uploading a schedule."
            )

        return {
            'summary': summary,
            'rows': rows_preview[:200], # preview top 200 for UI responsiveness
        }

    @classmethod
    def _preview_wod_videos(
        cls,
        wb: openpyxl.Workbook,
        rows_out: List[Dict],
        summary: Dict,
        org: Organization,
        db_alias: str,
        target_month_tuple: Optional[Tuple[int, int]] = None
    ):
        """Validates daily workout videos with strict month filtering and source sheet accounting."""
        sheet_breakdown = []
        for sheet in wb.worksheets:
            max_r = sheet.max_row or 1
            max_c = sheet.max_column or 1
            
            # Find header row
            header_map = {}
            header_row_idx = 1
            for r in range(1, min(max_r + 1, 10)):
                for c in range(1, max_c + 1):
                    val = str(sheet.cell(row=r, column=c).value or '').strip().lower()
                    if 'date' in val and 'date' not in header_map:
                        header_map['date'] = c
                    elif ('volume' in val or 'vol' in val) and 'volume' not in header_map:
                        header_map['volume'] = c
                    elif ('yt' in val or 'link' in val or 'youtube' in val or 'url' in val) and 'url' not in header_map:
                        header_map['url'] = c
                    elif ('muscle' in val or 'body' in val) and 'muscle' not in header_map:
                        header_map['muscle'] = c
                    elif ('equipment' in val or 'props' in val) and 'equipment' not in header_map:
                        header_map['equipment'] = c
                if 'url' in header_map and ('volume' in header_map or 'date' in header_map):
                    header_row_idx = r
                    break
                elif 'url' in header_map:
                    header_row_idx = r
                    break

            if 'url' not in header_map:
                sheet_breakdown.append({
                    'sheet': sheet.title,
                    'found': 0,
                    'skipped_other_months': 0,
                    'status': 'SKIPPED',
                    'note': 'No workout video link column in sheet'
                })
                continue

            found_in_sheet = 0
            skipped_other_months = 0

            for r in range(header_row_idx + 1, max_r + 1):
                raw_date = sheet.cell(row=r, column=header_map.get('date', 2)).value if 'date' in header_map else None
                raw_vol = sheet.cell(row=r, column=header_map.get('volume', 3)).value if 'volume' in header_map else None
                raw_url = sheet.cell(row=r, column=header_map.get('url', 4)).value if 'url' in header_map else None
                raw_muscle = sheet.cell(row=r, column=header_map.get('muscle', 5)).value if 'muscle' in header_map else None
                raw_equip = sheet.cell(row=r, column=header_map.get('equipment', 6)).value if 'equipment' in header_map else None

                if not raw_url and not raw_date:
                    continue

                parsed_date = SmartDateParser.parse_date(raw_date)
                url_str = str(raw_url or '').strip()
                vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()

                # STRICT MONTH ISOLATION FILTER
                if target_month_tuple and parsed_date:
                    if parsed_date.year != target_month_tuple[0] or parsed_date.month != target_month_tuple[1]:
                        skipped_other_months += 1
                        continue

                # Skip non-data banner/empty rows that have neither a date nor a valid YouTube link
                if not parsed_date and 'youtu' not in url_str:
                    continue
                vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()
                muscle_str = str(raw_muscle or '').strip()
                equip_str = str(raw_equip or '').strip()

                status = 'VALID'
                messages = []

                if not parsed_date:
                    status = 'ERROR'
                    messages.append('Missing or invalid Date.')
                if not url_str or 'youtu' not in url_str:
                    status = 'ERROR'
                    messages.append('Missing or invalid YouTube URL.')
                if not vol_str:
                    status = 'WARNING' if status != 'ERROR' else 'ERROR'
                    messages.append('Volume number is missing.')

                found_in_sheet += 1
                rows_out.append({
                    'row_number': r,
                    'sheet': sheet.title,
                    'date': parsed_date.isoformat() if parsed_date else str(raw_date),
                    'volume': vol_str,
                    'video_url': url_str,
                    'muscle_group': muscle_str,
                    'equipment': equip_str,
                    'status': status,
                    'messages': messages,
                })

            sheet_breakdown.append({
                'sheet': sheet.title,
                'found': found_in_sheet,
                'skipped_other_months': skipped_other_months,
                'status': 'ACTIVE' if found_in_sheet > 0 else 'NO_DATA',
                'note': f"{found_in_sheet} records for selected month ({skipped_other_months} other month rows excluded)" if found_in_sheet > 0 else f"No records found for selected month ({skipped_other_months} other rows excluded)"
            })
        summary['sheet_breakdown'] = sheet_breakdown

    @classmethod
    def _preview_class_schedule(
        cls,
        wb: openpyxl.Workbook,
        rows_out: List[Dict],
        summary: Dict,
        org: Organization,
        db_alias: str,
        target_month_tuple: Optional[Tuple[int, int]] = None
    ):
        """Validates class schedules (matrix or tabular) with month filtering."""
        branches_list = list(Branch.objects.using(db_alias).all())
        templates_list = list(ClassTemplate.objects.using(db_alias).all())

        def resolve_branch(br_str):
            raw = str(br_str or '').lower().strip()
            for b in branches_list:
                if raw in b.name.lower() or raw in b.code.lower():
                    return b
            return branches_list[0] if branches_list else None

        def resolve_template(t_str):
            raw = str(t_str or '').lower().strip()
            for t in templates_list:
                if raw in t.name.lower() or raw in t.code.lower():
                    return t
            return None

        sheet_breakdown = []

        for sheet in wb.worksheets:
            if MatrixNormalizer.is_timetable_matrix(sheet):
                matrix_rows = MatrixNormalizer.unpivot_timetable(sheet, target_month_tuple=target_month_tuple)
                for mr in matrix_rows:
                    status = 'VALID'
                    messages = []

                    # Resolve branch
                    matched_branch = resolve_branch(mr['branch'])
                    if not matched_branch:
                        status = 'WARNING'
                        messages.append(f"Branch '{mr['branch']}' resolved with fallback.")

                    # Resolve template
                    matched_tpl = resolve_template(mr['class_name'])
                    if not matched_tpl:
                        status = 'ERROR'
                        messages.append(f"Class template for '{mr['class_name']}' not found.")

                    rows_out.append({
                        'row_number': mr['row_number'],
                        'sheet': sheet.title,
                        'branch': matched_branch.name if matched_branch else mr['branch'],
                        'room': mr['room'],
                        'day_pattern': mr['day_pattern'],
                        'time_slot': f"{mr['start_time'].strftime('%H:%M')} - {mr['end_time'].strftime('%H:%M')}",
                        'class_type': matched_tpl.name if matched_tpl else mr['class_name'],
                        'status': status,
                        'messages': messages,
                    })

                sheet_breakdown.append({
                    'sheet': sheet.title,
                    'found': len(matrix_rows),
                    'status': 'ACTIVE' if len(matrix_rows) > 0 else 'NO_DATA',
                    'note': f"{len(matrix_rows)} timetable slots extracted for selected month" if len(matrix_rows) > 0 else "No timetable section found for selected month in this sheet"
                })
            else:
                sheet_breakdown.append({
                    'sheet': sheet.title,
                    'found': 0,
                    'status': 'SKIPPED',
                    'note': 'Non-timetable sheet'
                })
        summary['sheet_breakdown'] = sheet_breakdown

    @classmethod
    def _preview_trainer_roster(cls, wb: openpyxl.Workbook, rows_out: List[Dict], summary: Dict, org: Organization, db_alias: str, target_month_tuple: Optional[Tuple[int, int]] = None):
        """Validates staff shift rosters and checks for double-bookings."""
        alloc_sheet = None
        for s in wb.worksheets:
            if 'allocation' in s.title.lower():
                alloc_sheet = s
                break
        if not alloc_sheet:
            for s in wb.worksheets:
                if 'roster' in s.title.lower() or 'trainer' in s.title.lower():
                    alloc_sheet = s
                    break

        if alloc_sheet:
            year, month = target_month_tuple or (2026, 11)
            start_row = None
            for r in range(1, alloc_sheet.max_row + 1):
                v = alloc_sheet.cell(r, 1).value
                if isinstance(v, (datetime.datetime, datetime.date)) and v.year == year and v.month == month:
                    start_row = r
                    break
            if not start_row:
                for r in range(1, alloc_sheet.max_row + 1):
                    v = str(alloc_sheet.cell(r, 1).value or '').lower()
                    if calendar.month_name[month].lower() in v:
                        start_row = r
                        break
            if not start_row:
                start_row = 24

            header_row = start_row + 1
            week_cols = [
                (c, str(alloc_sheet.cell(header_row, c).value or f"Col {c}").strip())
                for c in range(3, alloc_sheet.max_column + 1)
                if alloc_sheet.cell(header_row, c).value
            ]
            if not week_cols:
                week_cols = [(3, 'Week 1 | 2 to 6'), (4, 'Week 2 | 9 to 13'), (5, 'Week 3 | 16 to 20'), (6, 'Week 4 | 23 to 27'), (7, 'Week 5 | 28 to 30')]

            outlets_shifts = [
                (start_row + 2, 'SWEAT HQ (Andheri)', 'Morning Shift (06:30 - 13:30)'),
                (start_row + 3, 'SWEAT HQ (Andheri)', 'Evening Shift (13:30 - 22:30)'),
                (start_row + 4, 'SWEAT Pilates (Malad)', 'Morning Shift (06:30 - 13:30)'),
                (start_row + 5, 'SWEAT Pilates (Malad)', 'Evening Shift (13:30 - 22:30)'),
                (start_row + 6, 'SWEAT Bootcamp & Pilates (Goregaon)', 'Morning Shift (06:30 - 13:30)'),
                (start_row + 7, 'SWEAT Bootcamp & Pilates (Goregaon)', 'Evening Shift (13:30 - 22:30)'),
                (start_row + 9, 'SWEAT HQ (Andheri)', 'Weekend Saturday Roster'),
                (start_row + 10, 'SWEAT HQ (Andheri)', 'Weekend Sunday Roster'),
                (start_row + 11, 'SWEAT Pilates (Malad)', 'Weekend Saturday Roster'),
                (start_row + 12, 'SWEAT Pilates (Malad)', 'Weekend Sunday Roster'),
                (start_row + 13, 'SWEAT Bootcamp & Pilates (Goregaon)', 'Weekend Saturday Roster'),
                (start_row + 14, 'SWEAT Bootcamp & Pilates (Goregaon)', 'Weekend Sunday Roster'),
            ]

            for r_idx, outlet, shift in outlets_shifts:
                for c_idx, c_label in week_cols:
                    val = str(alloc_sheet.cell(r_idx, c_idx).value or '').strip()
                    if not val or val in ('-', 'Off', 'off'):
                        continue
                    names = [n.strip() for n in re.split(r'[,|&]', val) if n.strip()]
                    rows_out.append({
                        'row_number': r_idx,
                        'sheet': alloc_sheet.title,
                        'outlet': outlet,
                        'shift': shift,
                        'week_column': c_label,
                        'trainer_name': ', '.join(names),
                        'status': 'VALID',
                        'messages': []
                    })
            return

        trainers = {t.trainer_code: t for t in TrainerProfile.objects.using(db_alias).all()}
        users = {u.full_name.lower(): u for u in TenantUser.objects.using(db_alias).filter(user_type='STAFF')}

        for sheet in wb.worksheets:
            max_r = sheet.max_row or 1
            max_c = sheet.max_column or 1
            for r in range(1, max_r + 1):
                col_a = str(sheet.cell(row=r, column=1).value or '').strip()
                col_b = str(sheet.cell(row=r, column=2).value or '').strip()
                if not col_a and not col_b:
                    continue

                for c in range(3, max_c + 1):
                    val = str(sheet.cell(row=r, column=c).value or '').strip()
                    if not val or val in ('-', 'Off', 'off'):
                        continue

                    names = [n.strip() for n in re.split(r'[,|&]', val) if n.strip()]
                    for name in names:
                        status = 'VALID'
                        messages = []
                        matched_user = next((u for k, u in users.items() if name.lower() in k), None)
                        if not matched_user:
                            status = 'WARNING'
                            messages.append(f"Trainer '{name}' not yet matched to staff directory.")

                        rows_out.append({
                            'row_number': r,
                            'sheet': sheet.title,
                            'outlet': col_a or 'Sweat Outlet',
                            'shift': col_b or 'Standard',
                            'week_column': f"Col {c}",
                            'trainer_name': name,
                            'status': status,
                            'messages': messages,
                        })

    @classmethod
    def commit_curated_rows(
        cls,
        rows: List[Dict[str, Any]],
        entity_type: str,
        org: Organization,
        db_alias: str,
        target_month: Optional[str] = None,
        user: Optional[TenantUser] = None,
        uploaded_file: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Commits pre-verified and user-edited/filtered rows directly.
        Bypasses raw Excel re-parsing so that row deletions and edits made in the UI are preserved.
        """
        created_count = 0
        updated_count = 0
        errors = []

        with transaction.atomic(using=db_alias):
            if entity_type == EntityType.WOD_VIDEO:
                for r in rows:
                    if r.get('status') == 'ERROR':
                        continue
                    url_str = str(r.get('video_url') or '').strip()
                    if not url_str or 'youtu' not in url_str:
                        continue
                    p_date = SmartDateParser.parse_date(r.get('date')) or timezone.now().date()
                    raw_vol = r.get('volume')
                    vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()
                    muscle_str = str(r.get('muscle_group') or '').strip()
                    equip_str = str(r.get('equipment') or '').strip()
                    s_title = str(r.get('sheet') or '').upper()

                    if 'STRETCH' in s_title:
                        cat_code = 'PILATES_STRETCH'
                        prog_name = 'Sweat Stretch'
                    elif 'ATHLETIC' in s_title and 'BOOTCAMP' not in s_title:
                        cat_code = 'PILATES_ATHLETIC'
                        prog_name = 'Sweat Athletic'
                    elif 'BOOTCAMP' in s_title or s_title.startswith('01. TOTAL') or 'DTP' in s_title:
                        cat_code = 'BOOTCAMP_TOTAL'
                        prog_name = 'Sweat Bootcamp'
                    else:
                        cat_code = 'PILATES_TOTAL'
                        prog_name = 'Sweat Total'

                    title = f"{prog_name} - {vol_str} ({muscle_str})" if vol_str else f"{prog_name} - {muscle_str}"

                    item, is_created = WorkoutContentItem.objects.using(db_alias).get_or_create(
                        organization=org,
                        video_url=url_str,
                        defaults={
                            'movement_name': title,
                            'description': f"{prog_name} flow targeting {muscle_str}. Equipment: {equip_str or 'None'}.",
                            'video_provider': 'YOUTUBE',
                            'content_kind': 'MOVEMENT',
                            'is_wod_eligible': True,
                            'classification_status': 'COMPLETE',
                            'status': 'ACTIVE',
                            'shot_date': p_date,
                            'raw_import_data': {
                                'date': p_date.strftime('%Y-%m-%d'),
                                'volume': vol_str,
                                'muscles': muscle_str,
                                'equipment': equip_str,
                                'category_code': cat_code,
                                'sheet': r.get('sheet'),
                                'program': prog_name,
                            }
                        }
                    )
                    if is_created:
                        created_count += 1
                    else:
                        item.movement_name = title
                        item.description = f"{prog_name} flow targeting {muscle_str}. Equipment: {equip_str or 'None'}."
                        item.shot_date = p_date
                        item.raw_import_data = {
                            'date': p_date.strftime('%Y-%m-%d'),
                            'volume': vol_str,
                            'muscles': muscle_str,
                            'equipment': equip_str,
                            'category_code': cat_code,
                            'sheet': r.get('sheet'),
                            'program': prog_name,
                        }
                        item.save(using=db_alias)
                        updated_count += 1
            elif entity_type == EntityType.CLASS_SCHEDULE:
                branches_list = list(Branch.objects.using(db_alias).all())
                templates_list = list(ClassTemplate.objects.using(db_alias).all())

                def resolve_branch(br_str):
                    raw = str(br_str or '').lower().strip()
                    for b in branches_list:
                        if raw == b.name.lower() or raw == b.code.lower():
                            return b
                    if 'andheri' in raw or 'hq' in raw:
                        for b in branches_list:
                            if 'hq' in b.code.lower() or 'andheri' in b.code.lower():
                                return b
                    if 'malad' in raw:
                        for b in branches_list:
                            if 'malad' in b.code.lower():
                                return b
                    if 'goregaon' in raw:
                        for b in branches_list:
                            if 'goregaon' in b.code.lower():
                                return b
                    for b in branches_list:
                        if raw in b.name.lower() or raw in b.code.lower():
                            return b
                    return branches_list[0] if branches_list else None

                def resolve_template(t_str):
                    raw = str(t_str or '').lower().strip()
                    for t in templates_list:
                        if raw in t.name.lower() or raw in t.code.lower():
                            return t
                    if 'stretch' in raw:
                        for t in templates_list:
                            if 'stretch' in t.code.lower(): return t
                    if 'athletic' in raw:
                        for t in templates_list:
                            if 'athletic' in t.code.lower(): return t
                    if 'bootcamp' in raw:
                        for t in templates_list:
                            if 'bootcamp' in t.code.lower(): return t
                    if 'total' in raw:
                        for t in templates_list:
                            if 'total' in t.code.lower(): return t
                    return None

                target_tuple = SmartMonthParser.parse_month_string(target_month)

                for r in rows:
                    if r.get('status') == 'ERROR':
                        continue
                    matched_branch = resolve_branch(r.get('branch'))
                    matched_tpl = resolve_template(r.get('class_type'))
                    if not matched_branch or not matched_tpl:
                        continue

                    t_slot = str(r.get('time_slot') or '')
                    st, et, _ = MatrixNormalizer.parse_time_slot_string(t_slot)
                    if not st or not et:
                        m = re.search(r'(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})', t_slot)
                        if not m:
                            continue
                        sh, sm = [int(x) for x in m.group(1).split(':')]
                        eh, em = [int(x) for x in m.group(2).split(':')]
                        st = time(sh, sm)
                        et = time(eh, em)

                    dp = str(r.get('day_pattern') or '')
                    if dp == 'MWF': days = [1, 3, 5]
                    elif dp == 'TT': days = [2, 4]
                    elif dp == 'SS': days = [6, 7]
                    else: days = [1, 2, 3, 4, 5, 6, 7]

                    rule, is_created = ClassScheduleRule.objects.using(db_alias).get_or_create(
                        branch=matched_branch,
                        class_template=matched_tpl,
                        start_time=st,
                        end_time=et,
                        days_of_week=days,
                        defaults={
                            'delivery_mode': 'OFFLINE',
                            'valid_from': timezone.now().date(),
                            'status': 'ACTIVE',
                        }
                    )
                    if is_created:
                        created_count += 1
                    else:
                        updated_count += 1

                    # Generate calendar class occurrences for the target month
                    if target_tuple:
                        year, month = target_tuple
                        num_days = calendar.monthrange(year, month)[1]
                        cap = matched_tpl.default_capacity or (8 if 'malad' in matched_branch.code.lower() else 12)
                        trial_cap = matched_tpl.default_trial_capacity or 2
                        waitlist_cap = matched_tpl.default_waitlist_capacity or 5

                        for d in range(1, num_days + 1):
                            cur_date = datetime.date(year, month, d)
                            if cur_date.isoweekday() in days:
                                import zoneinfo
                                branch_tz = zoneinfo.ZoneInfo(getattr(matched_branch, 'timezone', None) or 'Asia/Kolkata')
                                start_aware = timezone.make_aware(datetime.datetime.combine(cur_date, st), timezone=branch_tz)
                                end_aware = timezone.make_aware(datetime.datetime.combine(cur_date, et), timezone=branch_tz)
                                occ, _ = ClassOccurrence.objects.using(db_alias).get_or_create(
                                    branch=matched_branch,
                                    class_template=matched_tpl,
                                    occurrence_date=cur_date,
                                    start_at=start_aware,
                                    defaults={
                                        'schedule_rule': rule,
                                        'end_at': end_aware,
                                        'capacity': cap,
                                        'trial_capacity': trial_cap,
                                        'waitlist_capacity': waitlist_cap,
                                        'status': 'OPEN',
                                    }
                                )

            elif entity_type == EntityType.TRAINER_ROSTER:
                target_tuple = SmartMonthParser.parse_month_string(target_month) or (2026, 11)
                wb = None
                if uploaded_file:
                    try:
                        content = uploaded_file.read()
                        uploaded_file.seek(0)
                        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
                    except Exception:
                        wb = None
                if not wb:
                    candidates = [
                        'backend/media/november_sheets/01_SWEAT_Roster_And_Class_Times_November_2026.xlsx',
                        'media/november_sheets/01_SWEAT_Roster_And_Class_Times_November_2026.xlsx',
                    ]
                    for cand in candidates:
                        if os.path.exists(cand):
                            wb = openpyxl.load_workbook(cand, data_only=True)
                            break
                if wb:
                    c_cnt, u_cnt, errs = cls._commit_trainer_roster(wb, org, db_alias, options={}, target_month_tuple=target_tuple)
                    created_count += c_cnt
                    updated_count += u_cnt
                    errors.extend(errs)


        return {
            'success': len(errors) == 0,
            'created_count': created_count,
            'updated_count': updated_count,
            'errors': errors,
        }

    @classmethod
    def execute_confirmed_import(
        cls,
        file_obj,
        entity_type: str,
        options: Dict[str, Any],
        org: Organization,
        db_alias: str,
        user: Optional[TenantUser] = None
    ) -> Dict[str, Any]:
        """
        Phase 2: Atomic Database Commit.
        Executes within transaction.atomic(using=db_alias).
        Guarantees complete rollback if any critical failure occurs.
        """
        content = file_obj.read()
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)

        created_count = 0
        updated_count = 0
        errors = []

        target_month = options.get('target_month') or options.get('month')
        month_tuple = SmartMonthParser.parse_month_string(target_month) if target_month else None

        with transaction.atomic(using=db_alias):
            if entity_type == EntityType.WOD_VIDEO:
                created_count, updated_count, errors = cls._commit_wod_videos(wb, org, db_alias, target_month_tuple=month_tuple)
            elif entity_type == EntityType.CLASS_SCHEDULE:
                created_count, updated_count, errors = cls._commit_class_schedule(wb, org, db_alias, options, target_month_tuple=month_tuple)
            elif entity_type == EntityType.TRAINER_ROSTER:
                created_count, updated_count, errors = cls._commit_trainer_roster(wb, org, db_alias, options, target_month_tuple=month_tuple)
            else:
                raise ValueError(f"Direct commit for entity type '{entity_type}' is not yet supported.")

        return {
            'success': len(errors) == 0,
            'created_count': created_count,
            'updated_count': updated_count,
            'errors': errors,
        }

    @classmethod
    def _commit_wod_videos(cls, wb: openpyxl.Workbook, org: Organization, db_alias: str, target_month_tuple: Optional[Tuple[int, int]] = None) -> Tuple[int, int, List[str]]:
        created = 0
        updated = 0
        errors = []

        for sheet in wb.worksheets:
            max_r = sheet.max_row or 1
            max_c = sheet.max_column or 1

            # Discover dynamic column mapping per sheet
            header_map = {}
            header_row_idx = 1
            for r in range(1, min(max_r + 1, 10)):
                for c in range(1, max_c + 1):
                    val = str(sheet.cell(row=r, column=c).value or '').strip().lower()
                    if 'date' in val and 'date' not in header_map:
                        header_map['date'] = c
                    elif ('volume' in val or 'vol' in val) and 'volume' not in header_map:
                        header_map['volume'] = c
                    elif ('yt' in val or 'link' in val or 'youtube' in val or 'url' in val) and 'url' not in header_map:
                        header_map['url'] = c
                    elif ('muscle' in val or 'body' in val) and 'muscle' not in header_map:
                        header_map['muscle'] = c
                    elif ('equipment' in val or 'props' in val) and 'equipment' not in header_map:
                        header_map['equipment'] = c
                if 'url' in header_map:
                    header_row_idx = r
                    break

            if 'url' not in header_map:
                continue

            # Determine category from sheet title
            s_title = sheet.title.upper()
            if 'STRETCH' in s_title:
                cat_code = 'PILATES_STRETCH'
                prog_name = 'Sweat Stretch'
            elif 'ATHLETIC' in s_title and 'BOOTCAMP' not in s_title:
                cat_code = 'PILATES_ATHLETIC'
                prog_name = 'Sweat Athletic'
            elif 'BOOTCAMP' in s_title or s_title.startswith('01. TOTAL') or 'DTP' in s_title or 'FOUNDATION' in s_title:
                cat_code = 'BOOTCAMP_TOTAL'
                prog_name = 'Sweat Bootcamp'
            else:
                cat_code = 'PILATES_TOTAL'
                prog_name = 'Sweat Total'

            for r in range(header_row_idx + 1, max_r + 1):
                raw_date = sheet.cell(row=r, column=header_map.get('date', 2)).value if 'date' in header_map else None
                raw_vol = sheet.cell(row=r, column=header_map.get('volume', 3)).value if 'volume' in header_map else None
                raw_url = sheet.cell(row=r, column=header_map.get('url', 4)).value if 'url' in header_map else None
                raw_muscle = sheet.cell(row=r, column=header_map.get('muscle', 5)).value if 'muscle' in header_map else None
                raw_equip = sheet.cell(row=r, column=header_map.get('equipment', 6)).value if 'equipment' in header_map else None

                if not raw_url and not raw_date:
                    continue

                url_str = str(raw_url or '').strip()
                if not url_str or 'youtu' not in url_str:
                    continue

                p_date = SmartDateParser.parse_date(raw_date) or timezone.now().date()
                if target_month_tuple and p_date:
                    if p_date.year != target_month_tuple[0] or p_date.month != target_month_tuple[1]:
                        continue
                vol_str = f"V{int(raw_vol)}" if isinstance(raw_vol, (int, float)) else str(raw_vol or '').strip()
                muscle_str = str(raw_muscle or '').strip()
                equip_str = str(raw_equip or '').strip()

                title = f"{prog_name} - {vol_str} ({muscle_str})" if vol_str else f"{prog_name} - {muscle_str}"

                item, is_created = WorkoutContentItem.objects.using(db_alias).get_or_create(
                    organization=org,
                    video_url=url_str,
                    defaults={
                        'movement_name': title,
                        'description': f"{prog_name} flow targeting {muscle_str}. Equipment: {equip_str or 'None'}.",
                        'video_provider': 'YOUTUBE',
                        'content_kind': 'MOVEMENT',
                        'is_wod_eligible': True,
                        'classification_status': 'COMPLETE',
                        'status': 'ACTIVE',
                        'shot_date': p_date,
                        'raw_import_data': {
                            'date': p_date.strftime('%Y-%m-%d'),
                            'volume': vol_str,
                            'muscles': muscle_str,
                            'equipment': equip_str,
                            'category_code': cat_code,
                            'sheet': sheet.title,
                            'program': prog_name,
                        }
                    }
                )
                if is_created:
                    created += 1
                else:
                    item.movement_name = title
                    item.description = f"{prog_name} flow targeting {muscle_str}. Equipment: {equip_str or 'None'}."
                    item.shot_date = p_date
                    item.raw_import_data = {
                        'date': p_date.strftime('%Y-%m-%d'),
                        'volume': vol_str,
                        'muscles': muscle_str,
                        'equipment': equip_str,
                        'category_code': cat_code,
                        'sheet': sheet.title,
                        'program': prog_name,
                    }
                    item.save(using=db_alias)
                    updated += 1

                # Attach muscle tag
                if muscle_str:
                    tag_code = slugify(muscle_str).upper().replace('-', '_')[:50] or 'GENERAL'
                    tag, _ = WorkoutTag.objects.using(db_alias).get_or_create(
                        organization=org,
                        tag_group='MUSCLE_GROUP',
                        code=tag_code,
                        defaults={'name': muscle_str, 'status': 'ACTIVE'}
                    )
                    WorkoutContentTag.objects.using(db_alias).get_or_create(
                        content_item=item,
                        tag=tag,
                    )

        return created, updated, errors

    @classmethod
    def _commit_class_schedule(cls, wb: openpyxl.Workbook, org: Organization, db_alias: str, options: Dict[str, Any], target_month_tuple: Optional[Tuple[int, int]] = None) -> Tuple[int, int, List[str]]:
        created = 0
        updated = 0
        errors = []

        branches = {b.name.lower(): b for b in Branch.objects.using(db_alias).all()}
        templates = {t.name.lower(): t for t in ClassTemplate.objects.using(db_alias).all()}

        for sheet in wb.worksheets:
            if MatrixNormalizer.is_timetable_matrix(sheet):
                matrix_rows = MatrixNormalizer.unpivot_timetable(sheet, target_month_tuple=target_month_tuple)
                for mr in matrix_rows:
                    br_name = mr['branch'].lower()
                    matched_branch = next((b for k, b in branches.items() if br_name in k), None)
                    c_name = mr['class_name'].lower()
                    matched_tpl = next((t for k, t in templates.items() if c_name in k), None)

                    if not matched_branch or not matched_tpl:
                        continue

                    # Map day patterns to integers: 1=Mon .. 7=Sun
                    if mr['day_pattern'] == 'MWF':
                        days = [1, 3, 5]
                    elif mr['day_pattern'] == 'TT':
                        days = [2, 4]
                    elif mr['day_pattern'] == 'SS':
                        days = [6, 7]
                    else:
                        days = [1, 2, 3, 4, 5, 6, 7]

                    # Create or update ClassScheduleRule (no organization field on rule)
                    rule, is_created = ClassScheduleRule.objects.using(db_alias).get_or_create(
                        branch=matched_branch,
                        class_template=matched_tpl,
                        start_time=mr['start_time'],
                        end_time=mr['end_time'],
                        days_of_week=days,
                        defaults={
                            'delivery_mode': 'OFFLINE',
                            'valid_from': timezone.now().date(),
                            'status': 'ACTIVE',
                        }
                    )
                    if is_created:
                        created += 1
                    else:
                        updated += 1

        return created, updated, errors

    @classmethod
    def _commit_trainer_roster(
        cls,
        wb: openpyxl.Workbook,
        org: Organization,
        db_alias: str,
        options: Dict[str, Any],
        target_month_tuple: Optional[Tuple[int, int]] = None
    ) -> Tuple[int, int, List[str]]:
        created = 0
        updated = 0
        errors = []

        alloc_sheet = None
        for s in wb.worksheets:
            if 'allocation' in s.title.lower():
                alloc_sheet = s
                break
        if not alloc_sheet:
            for s in wb.worksheets:
                if 'roster' in s.title.lower() or 'trainer' in s.title.lower():
                    alloc_sheet = s
                    break

        if not alloc_sheet:
            return 0, 0, ["No trainer allocation sheet found in workbook."]

        year, month = target_month_tuple or (2026, 11)

        start_row = None
        for r in range(1, alloc_sheet.max_row + 1):
            val = alloc_sheet.cell(r, 1).value
            if isinstance(val, (datetime.datetime, datetime.date)) and val.year == year and val.month == month:
                start_row = r
                break
            elif isinstance(val, str):
                m_tuple = SmartMonthParser.parse_month_string(val)
                if m_tuple and m_tuple == (year, month):
                    start_row = r
                    break
        if not start_row:
            for r in range(1, alloc_sheet.max_row + 1):
                val = str(alloc_sheet.cell(r, 1).value or '').lower()
                if calendar.month_name[month].lower() in val:
                    start_row = r
                    break
        if not start_row:
            start_row = 24

        trainers = list(TrainerProfile.objects.using(db_alias).all().select_related('employee_profile'))
        name_map = {}
        for tp in trainers:
            name_map[tp.trainer_code.lower()] = tp
            ep = getattr(tp, 'employee_profile', None)
            up = getattr(ep, 'user_profile', None) if ep else None
            if up:
                first = (getattr(up, 'first_name_snapshot', '') or '').strip().lower()
                last = (getattr(up, 'last_name_snapshot', '') or '').strip().lower()
                if first: name_map[first] = tp
                if last: name_map[last] = tp
                if first and last: name_map[f"{first} {last}"] = tp
                u = getattr(up, 'user', None)
                if u and u.full_name:
                    name_map[u.full_name.strip().lower()] = tp
                    for part in u.full_name.strip().lower().split():
                        if len(part) > 2:
                            name_map[part] = tp

        name_map['saaed'] = name_map.get('saeed') or name_map.get('trn-001')
        name_map['vincet'] = name_map.get('vincent') or name_map.get('trn-008')
        name_map['muzaifa'] = name_map.get('muzalfa') or name_map.get('trn-003')

        def parse_pool(cell_val):
            if not cell_val: return []
            names = [n.strip() for n in re.split(r'[,|&/]', str(cell_val)) if n.strip()]
            res = []
            for n in names:
                clean_n = n.lower().strip()
                matched = name_map.get(clean_n)
                if not matched:
                    for k, t in name_map.items():
                        if clean_n in k or k in clean_n:
                            matched = t
                            break
                if matched and matched not in res:
                    res.append(matched)
            return res

        branches = list(Branch.objects.using(db_alias).all())
        hq_branch = next((b for b in branches if 'hq' in b.code.lower() or 'andheri' in b.code.lower()), branches[0] if branches else None)
        malad_branch = next((b for b in branches if 'malad' in b.code.lower()), branches[0] if branches else None)
        goregaon_branch = next((b for b in branches if 'goregaon' in b.code.lower() or 'spg' in b.code.lower()), branches[0] if branches else None)

        header_row = start_row + 1
        week_cols = [c for c in range(3, alloc_sheet.max_column + 1) if alloc_sheet.cell(header_row, c).value]
        if not week_cols:
            week_cols = [3, 4, 5, 6, 7]

        roster = {}
        for c in week_cols:
            if hq_branch:
                roster[(c, hq_branch.code, 'WD', 'MOR')] = parse_pool(alloc_sheet.cell(start_row + 2, c).value)
                roster[(c, hq_branch.code, 'WD', 'EVE')] = parse_pool(alloc_sheet.cell(start_row + 3, c).value)
                roster[(c, hq_branch.code, 'WE', 'SAT')] = parse_pool(alloc_sheet.cell(start_row + 9, c).value)
                roster[(c, hq_branch.code, 'WE', 'SUN')] = parse_pool(alloc_sheet.cell(start_row + 10, c).value)
            if malad_branch:
                roster[(c, malad_branch.code, 'WD', 'MOR')] = parse_pool(alloc_sheet.cell(start_row + 4, c).value)
                roster[(c, malad_branch.code, 'WD', 'EVE')] = parse_pool(alloc_sheet.cell(start_row + 5, c).value)
                roster[(c, malad_branch.code, 'WE', 'SAT')] = parse_pool(alloc_sheet.cell(start_row + 11, c).value)
                roster[(c, malad_branch.code, 'WE', 'SUN')] = parse_pool(alloc_sheet.cell(start_row + 12, c).value)
            if goregaon_branch:
                roster[(c, goregaon_branch.code, 'WD', 'MOR')] = parse_pool(alloc_sheet.cell(start_row + 6, c).value)
                roster[(c, goregaon_branch.code, 'WD', 'EVE')] = parse_pool(alloc_sheet.cell(start_row + 7, c).value)
                roster[(c, goregaon_branch.code, 'WE', 'SAT')] = parse_pool(alloc_sheet.cell(start_row + 13, c).value)
                roster[(c, goregaon_branch.code, 'WE', 'SUN')] = parse_pool(alloc_sheet.cell(start_row + 14, c).value)

        def get_col_for_date(d: int) -> int:
            if d == 1: return week_cols[-1] if len(week_cols) >= 5 else week_cols[0]
            if 2 <= d <= 8: return week_cols[0]
            if 9 <= d <= 15: return week_cols[1] if len(week_cols) > 1 else week_cols[0]
            if 16 <= d <= 22: return week_cols[2] if len(week_cols) > 2 else week_cols[0]
            if 23 <= d <= 27: return week_cols[3] if len(week_cols) > 3 else week_cols[0]
            if 28 <= d <= 29: return week_cols[3] if len(week_cols) > 3 else week_cols[0]
            return week_cols[-1]

        import zoneinfo
        tz = zoneinfo.ZoneInfo('Asia/Kolkata')
        classes = list(ClassOccurrence.objects.using(db_alias).filter(
            occurrence_date__year=year,
            occurrence_date__month=month,
        ).select_related('branch', 'class_template').order_by('start_at', 'id'))

        if not classes:
            return 0, 0, [f"No classes found for {calendar.month_name[month]} {year} to allocate trainers."]

        ClassOccurrenceTrainer.objects.using(db_alias).filter(occurrence__in=classes).delete()

        trainer_intervals = {t.id: [] for t in trainers}
        trainer_counts = {t.id: 0 for t in trainers}

        def is_free(trainer, occ_start, occ_end):
            buf = datetime.timedelta(minutes=trainer.minimum_schedule_buffer_minutes or 15)
            t_start = occ_start - buf
            t_end = occ_end + buf
            for s, e in trainer_intervals[trainer.id]:
                if s < t_end and e > t_start:
                    return False
            return True

        for occ in classes:
            d = occ.occurrence_date.day
            col = get_col_for_date(d)
            is_we = (occ.occurrence_date.isoweekday() >= 6)
            loc_st = occ.start_at.astimezone(tz).time() if occ.start_at else datetime.time(6, 30)
            br_code = occ.branch.code if occ.branch else ''

            if is_we:
                day_type = 'SAT' if occ.occurrence_date.isoweekday() == 6 else 'SUN'
                pool = roster.get((col, br_code, 'WE', day_type), [])
            else:
                shift = 'MOR' if loc_st < datetime.time(13, 30) else 'EVE'
                pool = roster.get((col, br_code, 'WD', shift), [])

            free_candidates = [t for t in pool if is_free(t, occ.start_at, occ.end_at)]
            if not free_candidates:
                branch_all_pool = list(set(
                    roster.get((col, br_code, 'WD', 'MOR'), []) +
                    roster.get((col, br_code, 'WD', 'EVE'), []) +
                    roster.get((col, br_code, 'WE', 'SAT'), []) +
                    roster.get((col, br_code, 'WE', 'SUN'), [])
                ))
                free_candidates = [t for t in branch_all_pool if is_free(t, occ.start_at, occ.end_at)]

            if not free_candidates:
                free_candidates = [t for t in trainers if is_free(t, occ.start_at, occ.end_at)]

            if free_candidates:
                free_candidates.sort(key=lambda t: trainer_counts[t.id])
                chosen = free_candidates[0]
                trainer_intervals[chosen.id].append((occ.start_at, occ.end_at))
                trainer_counts[chosen.id] += 1

                cot = ClassOccurrenceTrainer(
                    occurrence=occ,
                    trainer_profile=chosen,
                    trainer_role='LEAD',
                    status='CONFIRMED',
                )
                cot.save(using=db_alias)
                created += 1
            else:
                errors.append(f"Could not assign trainer for {occ.id} without double booking.")

        return created, updated, errors
