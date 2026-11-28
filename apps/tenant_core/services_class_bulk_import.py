"""
Class Schedule Bulk Import — ID-first (name-fallback) flat Excel importer.

A clean, deterministic importer for class occurrences. One row = one class session.
Resolution order for every reference is ID FIRST, then Name as a fallback — so a wrong
or duplicate name never creates the wrong class/branch/trainer.

Template columns (header row, any order, case-insensitive):
  Date | Branch Name | Branch ID | Class Name | Class ID | Trainer Name | Trainer ID | Start Time | End Time | Capacity

- Branch ID  = Branch UUID
- Class ID   = ClassTemplate UUID
- Trainer ID = TrainerProfile trainer_code (e.g. TR-0064) or TrainerProfile UUID
- Times in IST, 24h (06:30) or 12h with AM/PM; Date as DD-MM-YYYY / YYYY-MM-DD / Excel date.
"""

import io
import re
import datetime
from datetime import timedelta, timezone as dtz

import openpyxl

from .models_org import Branch
from .models_classes import ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer
from .models_workforce import TrainerProfile

UTC = dtz.utc
IST_OFFSET = timedelta(hours=5, minutes=30)  # stored times are true UTC; sheet times are IST

# header substring -> canonical field. Longer/ID keys are checked first so "branch id"
# wins over "branch".
_HEADER_ORDER = [
    ('branch id', 'branch_id'), ('branch code', 'branch_id'),
    ('class id', 'class_id'), ('class code', 'class_id'), ('template id', 'class_id'),
    ('trainer id', 'trainer_id'), ('trainer code', 'trainer_id'), ('coach id', 'trainer_id'),
    ('branch', 'branch_name'),
    ('class', 'class_name'), ('program', 'class_name'),
    ('trainer', 'trainer_name'), ('coach', 'trainer_name'),
    ('date', 'date'),
    ('start', 'start'), ('end', 'end'),
    ('capacity', 'capacity'), ('cap', 'capacity'),
]

_TIME_RE = re.compile(r'^\s*(\d{1,2})[:.\-](\d{2})\s*(am|pm)?\s*$', re.IGNORECASE)


def _trainer_name(tp):
    try:
        u = tp.employee_profile.user_profile.user
        return ((getattr(u, 'full_name', None) or f"{u.first_name} {u.last_name}").strip())
    except Exception:
        return ''


def _parse_date(val):
    if val is None or val == '':
        return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        return datetime.date(val.year, val.month, val.day)
    s = str(val).strip()
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%d-%m-%y', '%d %b %Y', '%d %B %Y', '%m/%d/%Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(val):
    """Return minutes-from-midnight (IST) or None. Accepts 24h or 12h with AM/PM; bare
    12h (no AM/PM) is taken literally in 24h."""
    if val is None or val == '':
        return None
    if isinstance(val, datetime.time):
        return val.hour * 60 + val.minute
    if isinstance(val, datetime.datetime):
        return val.hour * 60 + val.minute
    m = _TIME_RE.match(str(val))
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or '').lower()
    if ap == 'pm' and h != 12:
        h += 12
    elif ap == 'am' and h == 12:
        h = 0
    if h > 23 or mn > 59:
        return None
    return h * 60 + mn


class ClassScheduleBulkImportService:

    @classmethod
    def read_rows(cls, file_bytes):
        """Parse the first sheet into a list of raw row dicts. Raises ValueError on a
        sheet with no recognisable header."""
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.worksheets[0]
        max_r, max_c = ws.max_row or 1, ws.max_column or 1

        # find header row (within first 8 rows) that has at least a date/branch/class signal
        header_row = None
        col_map = {}
        for r in range(1, min(max_r, 8) + 1):
            m = {}
            for c in range(1, max_c + 1):
                raw = ws.cell(r, c).value
                if raw is None:
                    continue
                txt = str(raw).strip().lower()
                for key, field in _HEADER_ORDER:
                    if key in txt and field not in m:
                        m[field] = c
                        break
            if ('date' in m) and ('branch_name' in m or 'branch_id' in m) and ('class_name' in m or 'class_id' in m):
                header_row, col_map = r, m
                break
        if header_row is None:
            raise ValueError("No header row found. Expected columns like Date, Branch ID/Name, Class ID/Name, Start Time, End Time.")

        def g(r, field):
            c = col_map.get(field)
            if not c:
                return ''
            v = ws.cell(r, c).value
            return '' if v is None else (v if isinstance(v, (datetime.date, datetime.time, int, float)) else str(v).strip())

        rows = []
        for r in range(header_row + 1, max_r + 1):
            row = {f: g(r, f) for f in ['date', 'branch_name', 'branch_id', 'class_name',
                                        'class_id', 'trainer_name', 'trainer_id', 'start', 'end', 'capacity']}
            if not any(str(v).strip() for v in row.values()):
                continue  # blank line
            row['_row'] = r
            rows.append(row)
        return rows

    @classmethod
    def resolve_and_validate(cls, rows, db_alias):
        branches = {str(b.id): b for b in Branch.objects.using(db_alias).all()}
        branch_by_name = {}
        for b in branches.values():
            branch_by_name.setdefault((b.name or '').strip().lower(), b)
        templates = {str(t.id): t for t in ClassTemplate.objects.using(db_alias).filter(status='ACTIVE')}
        tmpl_by_name = {}
        for t in templates.values():
            tmpl_by_name.setdefault((t.name or '').strip().lower(), t)
        tr_by_id, tr_by_code, tr_by_name = {}, {}, {}
        for tp in TrainerProfile.objects.using(db_alias).filter(trainer_status='ACTIVE'):
            tr_by_id[str(tp.id)] = tp
            if getattr(tp, 'trainer_code', None):
                tr_by_code[tp.trainer_code.strip().lower()] = tp
            nm = _trainer_name(tp)
            if nm:
                tr_by_name.setdefault(nm.split()[0].lower(), tp)

        out = []
        for r in rows:
            errs = []
            # branch: ID first, name fallback
            br = branches.get(str(r.get('branch_id')).strip()) if r.get('branch_id') else None
            if not br and r.get('branch_name'):
                br = branch_by_name.get(str(r['branch_name']).strip().lower())
            if not br:
                errs.append('branch not found')
            # class: ID first, name fallback
            tmpl = templates.get(str(r.get('class_id')).strip()) if r.get('class_id') else None
            if not tmpl and r.get('class_name'):
                tmpl = tmpl_by_name.get(str(r['class_name']).strip().lower())
            if not tmpl:
                errs.append('class not found')
            # trainer: optional; ID(code/uuid) first, name fallback
            tp = None
            if r.get('trainer_id'):
                k = str(r['trainer_id']).strip().lower()
                tp = tr_by_id.get(k) or tr_by_code.get(k)
            if not tp and r.get('trainer_name'):
                tp = tr_by_name.get(str(r['trainer_name']).strip().split()[0].lower() if str(r['trainer_name']).strip() else '')
            trainer_requested = bool(str(r.get('trainer_id') or '').strip() or str(r.get('trainer_name') or '').strip())
            # date + times
            dt = _parse_date(r.get('date'))
            if not dt:
                errs.append('invalid date')
            st = _parse_time(r.get('start'))
            et = _parse_time(r.get('end'))
            if st is None:
                errs.append('invalid start time')
            if et is None:
                errs.append('invalid end time')
            if st is not None and et is not None and et <= st:
                errs.append('end not after start')
            # capacity
            cap = None
            try:
                cap = int(float(r['capacity'])) if str(r.get('capacity')).strip() else (tmpl.default_capacity if tmpl else None)
            except (ValueError, TypeError):
                cap = tmpl.default_capacity if tmpl else None
            start_utc = end_utc = None
            if dt and st is not None and et is not None:
                start_utc = datetime.datetime(dt.year, dt.month, dt.day, st // 60, st % 60, tzinfo=UTC) - IST_OFFSET
                end_utc = datetime.datetime(dt.year, dt.month, dt.day, et // 60, et % 60, tzinfo=UTC) - IST_OFFSET
            out.append({
                'row': r.get('_row'),
                'ok': not errs,
                'errors': errs,
                'branch': br, 'template': tmpl, 'trainer': tp,
                'date': dt, 'start_utc': start_utc, 'end_utc': end_utc, 'capacity': cap,
                'trainer_requested': trainer_requested,
                'trainer_unresolved': trainer_requested and tp is None,
                # display
                'branch_name': br.name if br else str(r.get('branch_name') or r.get('branch_id') or ''),
                'class_name': tmpl.name if tmpl else str(r.get('class_name') or r.get('class_id') or ''),
                'trainer_name': _trainer_name(tp) if tp else (str(r.get('trainer_name') or '') if trainer_requested else ''),
                'start_disp': f"{st // 60:02d}:{st % 60:02d}" if st is not None else str(r.get('start') or ''),
                'end_disp': f"{et // 60:02d}:{et % 60:02d}" if et is not None else str(r.get('end') or ''),
                'date_disp': dt.strftime('%d-%m-%Y') if dt else str(r.get('date') or ''),
            })
        return out

    @classmethod
    def commit(cls, resolved, db_alias, actor=None):
        res = {'created': 0, 'duplicates': 0, 'errors': 0,
               'trainer_assigned': 0, 'trainer_conflict': 0, 'trainer_unresolved': 0}
        for r in resolved:
            if not r['ok']:
                res['errors'] += 1
                continue
            if ClassOccurrence.objects.using(db_alias).filter(
                    branch=r['branch'], class_template=r['template'], start_at=r['start_utc']).exists():
                res['duplicates'] += 1
                continue
            occ = ClassOccurrence.objects.using(db_alias).create(
                class_template=r['template'], branch=r['branch'], schedule_rule=None,
                occurrence_date=r['date'], start_at=r['start_utc'], end_at=r['end_utc'],
                delivery_mode='OFFLINE',
                capacity=int(r['capacity']) if r['capacity'] else r['template'].default_capacity,
                status='SCHEDULED', is_manual=True,
            )
            res['created'] += 1
            if r['trainer']:
                try:
                    ClassOccurrenceTrainer.objects.using(db_alias).create(
                        occurrence=occ, trainer_profile=r['trainer'], trainer_role='LEAD', status='ASSIGNED')
                    res['trainer_assigned'] += 1
                except Exception:
                    res['trainer_conflict'] += 1  # overlapping assignment, etc.
            elif r['trainer_unresolved']:
                res['trainer_unresolved'] += 1
        return res
