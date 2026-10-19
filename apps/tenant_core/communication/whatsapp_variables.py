import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


EARLY_ARRIVAL_INSTRUCTION = "Please arrive 30 minutes prior to class for your fitness assessment and gear setup."

def fmt_ist_datetime(dt: Optional[datetime], tz_str: str = "Asia/Kolkata") -> str:
    """Format datetime -> 'Monday, 19 Oct at 10:00 AM' in IST."""
    if not dt:
        return ""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Cannot format naive datetime '{dt}' as IST: datetime has no timezone information.")
    import zoneinfo
    local_dt = dt.astimezone(zoneinfo.ZoneInfo(tz_str))
    day_num = str(local_dt.day)
    weekday = WEEKDAY_NAMES[local_dt.weekday()]
    month_abbr = MONTH_NAMES[local_dt.month][:3] if local_dt.month < len(MONTH_NAMES) else ""
    hour = local_dt.hour
    minute = local_dt.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{weekday}, {day_num} {month_abbr} at {hour12}:{minute:02d} {ampm}"

def fmt_ist_time(dt: Optional[datetime], tz_str: str = "Asia/Kolkata") -> str:
    """Format datetime -> '10:00 AM' in IST."""
    if not dt:
        return ""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Cannot format naive datetime '{dt}' as IST: datetime has no timezone information.")
    import zoneinfo
    local_dt = dt.astimezone(zoneinfo.ZoneInfo(tz_str))
    hour = local_dt.hour
    minute = local_dt.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {ampm}"

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _fmt_dt(dt: Optional[datetime], tz_str: str = "Asia/Kolkata") -> str:
    """Format datetime -> friendly local string e.g. 'Tomorrow at 6:30 PM'."""
    if not dt:
        return ""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Cannot format naive datetime '{dt}': datetime has no timezone information.")
    import zoneinfo
    local_dt = dt.astimezone(zoneinfo.ZoneInfo(tz_str))
    now_local = datetime.now(local_dt.tzinfo) if local_dt.tzinfo else datetime.now()
    delta_days = (local_dt.date() - now_local.date()).days
    hour = local_dt.hour
    minute = local_dt.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    time_str = f"{hour12}:{minute:02d} {ampm}"
    if delta_days == 0:
        return f"Today at {time_str}"
    if delta_days == 1:
        return f"Tomorrow at {time_str}"
    if delta_days == -1:
        return f"Yesterday at {time_str}"
    weekday = WEEKDAY_NAMES[local_dt.weekday()]
    month = MONTH_NAMES[local_dt.month]
    return f"{weekday}, {local_dt.day} {month} at {time_str}"


def _fmt_date(dt: Optional[datetime], tz_str: str = "Asia/Kolkata") -> str:
    if not dt:
        return ""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Cannot format naive datetime '{dt}': datetime has no timezone information.")
    import zoneinfo
    local_dt = dt.astimezone(zoneinfo.ZoneInfo(tz_str))
    return f"{local_dt.day} {MONTH_NAMES[local_dt.month]} {local_dt.year}"


def _fmt_time(dt: Optional[datetime], tz_str: str = "Asia/Kolkata") -> str:
    if not dt:
        return ""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"Cannot format naive datetime '{dt}': datetime has no timezone information.")
    import zoneinfo
    local_dt = dt.astimezone(zoneinfo.ZoneInfo(tz_str))
    hour = local_dt.hour
    minute = local_dt.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {ampm}"


class WhatsAppVariableResolver:
    """
    Resolves variable key names into concrete string values for
    Gupshup positional template parameters (position 0 -> param 1, etc.).
    Supported keys: member_name, lead_name, class_name, start_datetime,
                    start_date, start_time, trainer_name, branch_name, booking_number
    """

    def __init__(
        self,
        booking=None,
        trial=None,
        lead=None,
        occurrence=None,
        timezone_str: str = "Asia/Kolkata",
    ):
        self._booking = booking
        self._trial = trial
        self._lead = lead
        self._occ = occurrence
        self._tz = timezone_str
        self._cache: Dict[str, str] = {}

    def resolve(self, variable_keys: List[str]) -> List[str]:
        return [self._get(k) for k in variable_keys]

    def resolve_as_dict(self, variable_keys: List[str]) -> Dict[str, str]:
        return {k: self._get(k) for k in variable_keys}

    def _get(self, key: str) -> str:
        if key not in self._cache:
            try:
                self._cache[key] = str(self._resolve(key) or "")
            except Exception as exc:
                logger.warning("WhatsAppVariableResolver key=%s failed: %s", key, exc)
                self._cache[key] = ""
        return self._cache[key]

    def _resolve(self, key: str) -> str:
        occ = self._occurrence()
        mapping = {
            "member_name":    self._member_name,
            "lead_name":      self._lead_name,
            "class_name":     lambda: occ.class_template.name if occ and occ.class_template else "",
            "start_datetime": lambda: _fmt_dt(occ.start_at if occ else None, self._tz),
            "start_date":     lambda: _fmt_date(occ.start_at if occ else None, self._tz),
            "start_time":     lambda: _fmt_time(occ.start_at if occ else None, self._tz),
            "trainer_name":       self._trainer_name,
            "branch_name":        self._branch_name,
            "branch_address":     self._branch_address,
            "class_datetime_ist": self._class_datetime_ist,
            "booking_number":     lambda: str(self._booking.booking_number) if self._booking else "",
        }
        fn = mapping.get(key)
        if fn:
            return fn()
        logger.debug("WhatsAppVariableResolver: unknown key %s", key)
        return ""

    def _occurrence(self):
        if self._occ:
            return self._occ
        if self._booking:
            try:
                self._occ = self._booking.occurrence
                return self._occ
            except Exception:
                pass
        if self._trial:
            for attr in ("occurrence", "class_occurrence", "booking_occurrence"):
                try:
                    occ = getattr(self._trial, attr, None)
                    if occ:
                        self._occ = occ
                        return self._occ
                except Exception:
                    pass
        return None

    def _member_name(self) -> str:
        if self._booking:
            try:
                up = self._booking.user_profile
                name = f"{up.first_name_snapshot or ''} {up.last_name_snapshot or ''}".strip()
                if name:
                    return name
                user = up.user
                return f"{user.first_name} {user.last_name}".strip() or user.email
            except Exception:
                pass
        return self._lead_name()

    def _lead_name(self) -> str:
        lead = self._lead
        if not lead and self._trial:
            try:
                lead = self._trial.lead
            except Exception:
                pass
        if lead:
            name = f"{lead.first_name or ''} {lead.last_name or ''}".strip()
            return name or lead.email_normalized or lead.phone_normalized or "there"
        return "there"

    def _trainer_name(self) -> str:
        occ = self._occurrence()
        if not occ:
            return ""
        try:
            asgn = occ.trainer_assignments.filter(status="ACTIVE").first()
            if not asgn:
                asgn = occ.trainer_assignments.first()
            if asgn and asgn.trainer_profile:
                user = asgn.trainer_profile.employee_profile.user
                return f"{user.first_name} {user.last_name}".strip() or user.email
        except Exception:
            pass
        return ""

    def _branch_name(self) -> str:
        if self._booking:
            try:
                return self._booking.branch.name
            except Exception:
                pass
        occ = self._occurrence()
        if occ and occ.branch:
            try:
                return occ.branch.name
            except Exception:
                pass
        return ""

    def _branch_address(self) -> str:
        branch = None
        if self._trial and getattr(self._trial, 'branch', None):
            branch = self._trial.branch
        elif self._booking and getattr(self._booking, 'branch', None):
            branch = self._booking.branch
        else:
            occ = self._occurrence()
            if occ and getattr(occ, 'branch', None):
                branch = occ.branch
        if branch:
            return (
                getattr(branch, 'address', '')
                or f"{getattr(branch, 'address_line_1', '') or ''} {getattr(branch, 'address_line_2', '') or ''}".strip()
            ).strip()
        return ""

    def _class_datetime_ist(self) -> str:
        dt = None
        if self._trial and getattr(self._trial, 'scheduled_start', None):
            dt = self._trial.scheduled_start
        else:
            occ = self._occurrence()
            if occ and getattr(occ, 'start_at', None):
                dt = occ.start_at
        return fmt_ist_datetime(dt, self._tz) if dt else ""
