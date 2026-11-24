"""
backend/apps/tenant_core/services_telephony_export.py — Dynamic Excel (.xlsx) Export Service for Sales Telephony & Call Logs.

Generates beautifully styled, multi-tab or focused Excel workbooks (.xlsx) using openpyxl:
- Sheet 1: Call Logs (Full detailed call session audit with resolved lead names, durations, dispositions, audio links)
- Sheet 2: Sales Rep Leaderboard (Reps ranked by dials, connect rate %, meaningful calls, talk time, trials booked)
- Sheet 3: Call Dispositions (Aggregated breakdown by outcome and percentage)
- Sheet 4: Hourly Connect Heatmap (09:00 - 21:00 calling windows & answer rate analysis)
- Sheet 5: Executive KPI Summary (Overall calling metrics & filter context)
"""

import io
import math
import zoneinfo
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from django.conf import settings
from django.utils import timezone as dj_timezone
from django.db.models import Q, Count, Sum

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from apps.tenant_core.models_crm import CallSession, Lead, TenantUser, UserProfile, Organization, Branch
from apps.tenant_core.security_utils import mask_phone_if_restricted

try:
    IST = zoneinfo.ZoneInfo('Asia/Kolkata')
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))


def _clean_phone_key(phone: str) -> str:
    """Extract trailing 10 digits from phone string for reliable lookups."""
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _build_phone_name_cache(phone_numbers: List[str], db_alias: str) -> Dict[str, str]:
    """
    Pre-fetch lead and member names in bulk to eliminate N+1 queries during Excel export.
    Returns a dictionary mapping last-10-digits -> Full Name.
    """
    cache: Dict[str, str] = {}
    clean_keys = set()
    for p in phone_numbers:
        k = _clean_phone_key(p)
        if k:
            clean_keys.add(k)

    if not clean_keys:
        return cache

    # 1. Match against Leads
    # Query leads with phone ending in or matching any key
    lead_matches = Lead.objects.using(db_alias).filter(
        Q(phone_normalized__isnull=False) & ~Q(phone_normalized='')
    ).values('phone_normalized', 'first_name', 'last_name')

    for lm in lead_matches:
        pn = lm.get('phone_normalized') or ''
        k = _clean_phone_key(pn)
        if k in clean_keys and k not in cache:
            name = f"{lm.get('first_name', '')} {lm.get('last_name', '')}".strip()
            if name:
                cache[k] = name

    # 2. Match against Tenant Users / Members
    remaining_keys = clean_keys - set(cache.keys())
    if remaining_keys:
        user_matches = TenantUser.objects.using(db_alias).filter(
            Q(phone__isnull=False) & ~Q(phone='')
        ).values('phone', 'first_name', 'last_name')

        for um in user_matches:
            pn = um.get('phone') or ''
            k = _clean_phone_key(pn)
            if k in remaining_keys and k not in cache:
                name = f"{um.get('first_name', '')} {um.get('last_name', '')}".strip()
                if name:
                    cache[k] = name

    return cache


def export_telephony_to_excel_buffer(
    qs,
    db_alias: str,
    request=None,
    org=None,
    timeframe: str = 'today',
    filter_summary: Optional[Dict[str, Any]] = None,
    export_type: str = 'all',
) -> bytes:
    """
    Generates an in-memory .xlsx workbook for the given CallSession queryset.
    Returns binary Excel (.xlsx) file bytes.
    """
    wb = openpyxl.Workbook()
    # Remove default sheet
    default_sheet = wb.active

    # Shared Palette & Styles
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_bold = Font(name="Calibri", size=10, bold=True)
    font_data = Font(name="Calibri", size=10)
    font_link = Font(name="Calibri", size=10, color="004085", underline="single")
    font_code = Font(name="Consolas", size=9)

    fill_navy = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")       # Call Logs Header
    fill_forest = PatternFill(start_color="0D5C3A", end_color="0D5C3A", fill_type="solid")     # Rep Leaderboard Header
    fill_purple = PatternFill(start_color="5A3D8A", end_color="5A3D8A", fill_type="solid")     # Dispositions Header
    fill_slate = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")      # Hourly Stats Header
    fill_blue = PatternFill(start_color="1A5276", end_color="1A5276", fill_type="solid")       # Summary Header
    fill_stripe = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")     # Alternating Rows

    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )

    align_left = Alignment(horizontal="left", vertical="center")
    align_center = Alignment(horizontal="center", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")
    align_wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)

    labels_map = dict(CallSession.DISPOSITION_CHOICES)

    # Pre-evaluate queryset with optimal select_related
    if hasattr(qs, 'query') and (getattr(qs.query, 'low_mark', None) is not None or getattr(qs.query, 'high_mark', None) is not None):
        call_sessions = list(qs.select_related('lead', 'sales_rep', 'branch'))
    elif hasattr(qs, 'order_by'):
        call_sessions = list(qs.order_by('-created_at').select_related('lead', 'sales_rep', 'branch'))
    else:
        call_sessions = list(qs)

    # Build Phone -> Name cache for any missing lead records
    unresolved_phones = [
        cs.user_phone_number for cs in call_sessions
        if not cs.lead and cs.user_phone_number
    ]
    phone_name_cache = _build_phone_name_cache(unresolved_phones, db_alias)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 1: Call Logs (Detailed Session Records)
    # ══════════════════════════════════════════════════════════════════════════
    ws_logs = wb.create_sheet(title="Call Logs")
    headers_logs = [
        "Call ID",
        "Timestamp (IST)",
        "Lead / Contact Name",
        "Phone Number",
        "Sales Representative",
        "Rep Email",
        "Studio / Branch",
        "Direction",
        "Call Status",
        "Duration (Seconds)",
        "Talk Time (Formatted)",
        "Meaningful (≥30s)",
        "Call Disposition",
        "Disposition Code",
        "Disposition Notes",
        "Call Mode",
        "Audio Recording Link"
    ]
    ws_logs.append(headers_logs)
    ws_logs.row_dimensions[1].height = 28

    for col_idx, cell in enumerate(ws_logs[1], start=1):
        cell.font = font_header
        cell.fill = fill_navy
        cell.alignment = align_center

    for r_idx, cs in enumerate(call_sessions, start=2):
        # Format Timestamp
        if cs.created_at:
            time_str = cs.created_at.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')
        else:
            time_str = ""

        # Format Duration
        dur = int(cs.duration_seconds or 0)
        dur_fmt = f"{dur // 60}m {dur % 60}s"

        # Resolve Lead Name
        if cs.lead:
            lead_name = f"{cs.lead.first_name} {cs.lead.last_name}".strip()
            if not lead_name:
                lead_name = cs.lead.phone_normalized or "Lead"
        else:
            k = _clean_phone_key(cs.user_phone_number)
            lead_name = phone_name_cache.get(k) or "Direct Contact"

        # Resolve Phone Number
        raw_phone = cs.user_phone_number or (cs.lead.phone_normalized if cs.lead else '')
        masked_phone = mask_phone_if_restricted(request, raw_phone) if raw_phone else ''

        # Resolve Sales Rep
        if cs.sales_rep:
            rep_name = f"{cs.sales_rep.first_name} {cs.sales_rep.last_name}".strip()
            rep_email = cs.sales_rep.email or ""
        else:
            rep_name = "Automated / Pool"
            rep_email = ""

        # Branch
        branch_name = cs.branch.name if cs.branch else "All Branches"

        # Disposition
        disp_label = labels_map.get(cs.disposition, cs.disposition) if cs.disposition else "Pending"
        disp_code = cs.disposition or "PENDING"

        # Mode
        call_mode_label = "Human Manual" if cs.call_mode == 'HUMAN_MANUAL' else "AI Automated"

        # Recording
        rec_url = cs.recording_url or ""

        row_data = [
            str(cs.id),
            time_str,
            lead_name,
            masked_phone,
            rep_name,
            rep_email,
            branch_name,
            cs.direction or "OUTBOUND",
            cs.status or "INITIATED",
            dur,
            dur_fmt,
            "YES" if cs.is_meaningful else "NO",
            disp_label,
            disp_code,
            cs.disposition_notes or "",
            call_mode_label,
            "▶ Listen Audio" if rec_url else "No Recording"
        ]
        ws_logs.append(row_data)
        ws_logs.row_dimensions[r_idx].height = 20

        is_even = (r_idx % 2 == 0)
        row_fill = fill_stripe if is_even else None

        for c_idx in range(1, len(row_data) + 1):
            cell = ws_logs.cell(row=r_idx, column=c_idx)
            cell.font = font_data
            cell.border = thin_border
            if row_fill:
                cell.fill = row_fill

            # Specific column alignments & formats
            if c_idx == 1: # Call ID
                cell.font = font_code
                cell.alignment = align_center
            elif c_idx == 2: # Timestamp
                cell.alignment = align_center
            elif c_idx in (4, 8, 9, 12, 16): # Phone, Direction, Status, Meaningful, Mode
                cell.alignment = align_center
            elif c_idx == 10: # Duration Seconds (Numeric)
                cell.alignment = align_right
                cell.number_format = '#,##0'
            elif c_idx == 11: # Talk Time
                cell.alignment = align_right
            elif c_idx == 15: # Notes
                cell.alignment = align_wrap
            elif c_idx == 17: # Audio Recording Link
                cell.alignment = align_center
                if rec_url and rec_url.startswith("http"):
                    try:
                        cell.hyperlink = rec_url
                        cell.font = font_link
                    except Exception:
                        pass

    # Auto-fit column widths for Sheet 1
    for col in ws_logs.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val = str(cell.value or '')
            if '\n' in val:
                val = val.split('\n')[0]
            if len(val) > max_len:
                max_len = len(val)
        ws_logs.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    # If user requested Call Logs only, we can skip other sheets or add a quick filter sheet
    if export_type == 'call_logs':
        # Add summary sheet at the end
        pass

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 2: Sales Rep Leaderboard (Dialer Performance)
    # ══════════════════════════════════════════════════════════════════════════
    ws_leaderboard = wb.create_sheet(title="Rep Leaderboard")
    headers_lead = [
        "Rank",
        "Sales Representative",
        "Email",
        "Total Dials",
        "Connected Calls",
        "Connect Rate (%)",
        "Meaningful Calls (≥30s)",
        "Meaningful Rate (%)",
        "Total Talk Time (Mins)",
        "Direct Trials Booked"
    ]
    ws_leaderboard.append(headers_lead)
    ws_leaderboard.row_dimensions[1].height = 28
    for col_idx, cell in enumerate(ws_leaderboard[1], start=1):
        cell.font = font_header
        cell.fill = fill_forest
        cell.alignment = align_center

    # Compute Rep Stats from call_sessions
    rep_map: Dict[str, Dict[str, Any]] = {}
    for cs in call_sessions:
        if cs.sales_rep:
            r_id = str(cs.sales_rep_id)
            r_name = f"{cs.sales_rep.first_name} {cs.sales_rep.last_name}".strip()
            r_email = cs.sales_rep.email or ""
        else:
            r_id = "pool"
            r_name = "Automated / Pool"
            r_email = "system@sweatfit.com"

        if r_id not in rep_map:
            rep_map[r_id] = {
                'id': r_id,
                'name': r_name,
                'email': r_email,
                'dials': 0,
                'connected': 0,
                'meaningful': 0,
                'duration_sec': 0.0,
                'trials_booked': 0,
            }

        st = rep_map[r_id]
        st['dials'] += 1
        if cs.status in ('COMPLETED', 'CONNECTED'):
            st['connected'] += 1
        if cs.is_meaningful:
            st['meaningful'] += 1
        st['duration_sec'] += float(cs.duration_seconds or 0.0)
        if cs.disposition == 'TRIAL_BOOKED':
            st['trials_booked'] += 1

    sorted_reps = sorted(
        rep_map.values(),
        key=lambda x: (x['trials_booked'], x['connected'], x['dials']),
        reverse=True
    )

    for rank, rep in enumerate(sorted_reps, start=1):
        dials = rep['dials']
        conn = rep['connected']
        mean = rep['meaningful']
        conn_pct = round((conn / dials * 100), 1) if dials > 0 else 0.0
        mean_pct = round((mean / conn * 100), 1) if conn > 0 else 0.0
        talk_mins = round(rep['duration_sec'] / 60.0, 1)

        row_lead = [
            rank,
            rep['name'],
            rep['email'],
            dials,
            conn,
            f"{conn_pct}%",
            mean,
            f"{mean_pct}%",
            talk_mins,
            rep['trials_booked']
        ]
        ws_leaderboard.append(row_lead)
        r_lead_idx = rank + 1
        ws_leaderboard.row_dimensions[r_lead_idx].height = 20

        is_even = (r_lead_idx % 2 == 0)
        row_fill = fill_stripe if is_even else None

        for c_idx in range(1, len(row_lead) + 1):
            cell = ws_leaderboard.cell(row=r_lead_idx, column=c_idx)
            cell.font = font_data
            cell.border = thin_border
            if row_fill:
                cell.fill = row_fill

            if c_idx == 1:
                cell.alignment = align_center
                cell.font = font_bold
            elif c_idx in (2, 3):
                cell.alignment = align_left
            elif c_idx in (4, 5, 7, 10):
                cell.alignment = align_right
                cell.number_format = '#,##0'
            elif c_idx in (6, 8):
                cell.alignment = align_right
            elif c_idx == 9:
                cell.alignment = align_right
                cell.number_format = '#,##0.0'

    # Auto-fit Sheet 2
    for col in ws_leaderboard.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws_leaderboard.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 35)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 3: Call Dispositions Breakdown
    # ══════════════════════════════════════════════════════════════════════════
    ws_disp = wb.create_sheet(title="Call Dispositions")
    headers_disp = [
        "Disposition Code",
        "Outcome Label",
        "Total Calls Logged",
        "Percentage of Total (%)"
    ]
    ws_disp.append(headers_disp)
    ws_disp.row_dimensions[1].height = 28
    for col_idx, cell in enumerate(ws_disp[1], start=1):
        cell.font = font_header
        cell.fill = fill_purple
        cell.alignment = align_center

    disp_counts: Dict[str, int] = {}
    total_calls_count = len(call_sessions)
    for cs in call_sessions:
        code = cs.disposition or 'PENDING'
        disp_counts[code] = disp_counts.get(code, 0) + 1

    sorted_disps = sorted(disp_counts.items(), key=lambda x: x[1], reverse=True)
    for idx, (code, count) in enumerate(sorted_disps, start=2):
        lbl = labels_map.get(code, code) if code != 'PENDING' else 'Pending Outcome'
        pct = round((count / total_calls_count * 100), 1) if total_calls_count > 0 else 0.0

        row_d = [code, lbl, count, f"{pct}%"]
        ws_disp.append(row_d)
        ws_disp.row_dimensions[idx].height = 20

        is_even = (idx % 2 == 0)
        row_fill = fill_stripe if is_even else None

        for c_idx in range(1, len(row_d) + 1):
            cell = ws_disp.cell(row=idx, column=c_idx)
            cell.font = font_data
            cell.border = thin_border
            if row_fill:
                cell.fill = row_fill

            if c_idx == 1:
                cell.font = font_code
                cell.alignment = align_left
            elif c_idx == 2:
                cell.alignment = align_left
            elif c_idx == 3:
                cell.alignment = align_right
                cell.number_format = '#,##0'
            elif c_idx == 4:
                cell.alignment = align_right

    for col in ws_disp.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws_disp.column_dimensions[col_letter].width = min(max(max_len + 3, 14), 40)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 4: Hourly Connect Rate Analysis (09:00 - 21:00)
    # ══════════════════════════════════════════════════════════════════════════
    ws_hourly = wb.create_sheet(title="Hourly Answer Rates")
    headers_hourly = [
        "Calling Window (IST)",
        "Total Dials",
        "Connected Calls",
        "Connect Rate (%)",
        "Analysis & Peak Indicator"
    ]
    ws_hourly.append(headers_hourly)
    ws_hourly.row_dimensions[1].height = 28
    for col_idx, cell in enumerate(ws_hourly[1], start=1):
        cell.font = font_header
        cell.fill = fill_slate
        cell.alignment = align_center

    hourly_stats: Dict[int, Dict[str, int]] = {h: {'dials': 0, 'connected': 0} for h in range(9, 21)}
    for cs in call_sessions:
        if cs.created_at:
            h = cs.created_at.astimezone(IST).hour
            if 9 <= h < 21:
                hourly_stats[h]['dials'] += 1
                if cs.status in ('COMPLETED', 'CONNECTED'):
                    hourly_stats[h]['connected'] += 1

    peak_h = -1
    max_rate = -1.0
    for h in range(9, 21):
        d = hourly_stats[h]['dials']
        c = hourly_stats[h]['connected']
        rate = (c / d * 100) if d > 0 else 0.0
        if d >= 1 and rate > max_rate:
            max_rate = rate
            peak_h = h

    for r_h_idx, h in enumerate(range(9, 21), start=2):
        d = hourly_stats[h]['dials']
        c = hourly_stats[h]['connected']
        rate = round((c / d * 100), 1) if d > 0 else 0.0
        slot_label = f"{h:02d}:00 - {h+1:02d}:00"
        is_peak = (h == peak_h and max_rate > 0)
        indicator = "★ Golden Calling Hour (Highest Pick-up)" if is_peak else ("Moderate Pick-up" if rate >= 25 else "Standard")

        row_h = [slot_label, d, c, f"{rate}%", indicator]
        ws_hourly.append(row_h)
        ws_hourly.row_dimensions[r_h_idx].height = 20

        is_even = (r_h_idx % 2 == 0)
        row_fill = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid") if is_peak else (fill_stripe if is_even else None)

        for c_idx in range(1, len(row_h) + 1):
            cell = ws_hourly.cell(row=r_h_idx, column=c_idx)
            cell.font = font_bold if is_peak else font_data
            cell.border = thin_border
            if row_fill:
                cell.fill = row_fill

            if c_idx == 1:
                cell.alignment = align_center
            elif c_idx in (2, 3):
                cell.alignment = align_right
                cell.number_format = '#,##0'
            elif c_idx == 4:
                cell.alignment = align_right
            elif c_idx == 5:
                cell.alignment = align_left

    for col in ws_hourly.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws_hourly.column_dimensions[col_letter].width = min(max(max_len + 3, 14), 45)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEET 5: Executive KPI Summary
    # ══════════════════════════════════════════════════════════════════════════
    ws_summary = wb.create_sheet(title="Executive Summary")
    ws_summary.column_dimensions['A'].width = 30
    ws_summary.column_dimensions['B'].width = 35

    # Overall KPIs
    total_dials = len(call_sessions)
    connected_calls = sum(1 for cs in call_sessions if cs.status in ('COMPLETED', 'CONNECTED'))
    outbound_calls = sum(1 for cs in call_sessions if cs.direction == 'OUTBOUND')
    inbound_calls = sum(1 for cs in call_sessions if cs.direction == 'INBOUND')
    meaningful_calls = sum(1 for cs in call_sessions if cs.is_meaningful)
    total_sec = sum(float(cs.duration_seconds or 0) for cs in call_sessions)
    total_talk_mins = round(total_sec / 60.0, 1)
    trials_booked = sum(1 for cs in call_sessions if cs.disposition == 'TRIAL_BOOKED')
    connect_rate = round((connected_calls / total_dials * 100), 1) if total_dials > 0 else 0.0
    meaningful_rate = round((meaningful_calls / connected_calls * 100), 1) if connected_calls > 0 else 0.0

    kpi_rows = [
        ("EXECUTIVE TELEPHONY KPI SUMMARY", ""),
        ("Generated Date (IST)", datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')),
        ("Report Timeframe Filter", timeframe.replace('_', ' ').title()),
        ("Organization", org.name if org else "SWEAT Fitness"),
        ("", ""),
        ("CORE CALLING METRICS", ""),
        ("Total Dials Placed", total_dials),
        ("Outbound Calls", outbound_calls),
        ("Inbound Calls", inbound_calls),
        ("Connected / Answered Calls", connected_calls),
        ("Connect Rate (%)", f"{connect_rate}%"),
        ("Meaningful Conversations (≥30s)", meaningful_calls),
        ("Meaningful Rate of Connects (%)", f"{meaningful_rate}%"),
        ("Total Talk Time (Minutes)", f"{total_talk_mins} mins"),
        ("Direct Trials Booked / Scheduled", trials_booked),
        ("Peak Golden Calling Hour", f"{peak_h:02d}:00 - {peak_h+1:02d}:00 ({max_rate}% connect)" if peak_h > 0 else "N/A"),
        ("", ""),
        ("ACTIVE EXPORT FILTERS APPLIED", ""),
    ]

    if filter_summary:
        for k, v in filter_summary.items():
            if v:
                kpi_rows.append((k, str(v)))

    for s_idx, (k, v) in enumerate(kpi_rows, start=1):
        ws_summary.append([k, v])
        ws_summary.row_dimensions[s_idx].height = 22
        cell_k = ws_summary.cell(row=s_idx, column=1)
        cell_v = ws_summary.cell(row=s_idx, column=2)

        if "EXECUTIVE" in k or "CORE CALLING" in k or "ACTIVE EXPORT" in k:
            cell_k.font = font_header
            cell_k.fill = fill_blue
            cell_v.fill = fill_blue
            cell_k.alignment = align_left
        elif k == "":
            pass
        else:
            cell_k.font = font_bold
            cell_k.alignment = align_left
            cell_k.border = thin_border
            cell_v.font = font_data
            cell_v.alignment = align_left
            cell_v.border = thin_border

    # Remove the placeholder default sheet if present
    if default_sheet in wb.worksheets and len(wb.worksheets) > 1:
        wb.remove(default_sheet)

    # Reorder sheets so Call Logs is first or Summary is first based on export type
    if export_type == 'dialer_summary':
        # Put Executive Summary and Rep Leaderboard first
        wb._sheets = [ws_summary, ws_leaderboard, ws_disp, ws_hourly, ws_logs]
    else:
        # Call Logs first
        wb._sheets = [ws_logs, ws_leaderboard, ws_disp, ws_hourly, ws_summary]

    # Save to memory buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()
