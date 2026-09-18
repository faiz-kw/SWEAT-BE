"""
apps/tenant_core/services_schedule.py — Branch Schedule & Operating Hours Service.

Sprint 13 Business Logic Implementation:
1. branches.timezone is the authoritative timezone.
2. BranchOperatingException row overrides weekly working hours for any given date.
3. is_24_hours controls 24-hour operation.
4. Overnight schedules (e.g. 22:00 -> 06:00) are resolved at application layer.
5. Booking/attendance enforcement integration is DEFERRED to Sprint 15.
"""

from datetime import date, time, datetime, timedelta
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
from django.utils import timezone

from .models_govern import BranchWorkingHours, BranchOperatingException
from .models_org import Branch


class BranchScheduleService:
    @staticmethod
    def get_branch_timezone(branch: Branch) -> ZoneInfo:
        """Rule 1: branches.timezone is the authoritative timezone."""
        tz_str = getattr(branch, 'timezone', None) or 'UTC'
        try:
            return ZoneInfo(tz_str)
        except Exception:
            return ZoneInfo('UTC')

    @classmethod
    def get_effective_schedule_for_date(cls, branch: Branch, target_date: date, db_alias: Optional[str] = None) -> Dict[str, Any]:
        """
        Determines the effective schedule for a branch on a given date.
        Rule 2: exception row overrides weekly working hours.
        Rule 3: is_24_hours controls 24-hour operation.
        """
        db_alias = db_alias or (getattr(branch, '_state', None) and getattr(branch._state, 'db', None)) or 'default'

        # Check for operating exception first (Rule 2)
        exception = BranchOperatingException.objects.using(db_alias).filter(
            branch=branch,
            exception_date=target_date,
        ).first()

        if exception:
            if exception.is_closed:
                return {
                    'date': target_date,
                    'is_open': False,
                    'is_24_hours': False,
                    'open_time': None,
                    'close_time': None,
                    'source': 'EXCEPTION',
                    'reason': exception.reason,
                    'is_overnight': False,
                }
            is_overnight = (
                exception.open_time is not None and
                exception.close_time is not None and
                exception.close_time < exception.open_time
            )
            return {
                'date': target_date,
                'is_open': True,
                'is_24_hours': False,
                'open_time': exception.open_time,
                'close_time': exception.close_time,
                'source': 'EXCEPTION',
                'reason': exception.reason,
                'is_overnight': is_overnight,
            }

        # No exception: check weekly working hours
        # Python isoweekday: Monday=1, Sunday=7 (matches canonical day_of_week)
        day_of_week = target_date.isoweekday()
        working_hours = BranchWorkingHours.objects.using(db_alias).filter(
            branch=branch,
            day_of_week=day_of_week,
        ).first()

        if not working_hours or not working_hours.is_open:
            return {
                'date': target_date,
                'is_open': False,
                'is_24_hours': False,
                'open_time': None,
                'close_time': None,
                'source': 'WEEKLY_SCHEDULE' if working_hours else 'DEFAULT_CLOSED',
                'reason': None,
                'is_overnight': False,
            }

        if working_hours.is_24_hours:
            return {
                'date': target_date,
                'is_open': True,
                'is_24_hours': True,
                'open_time': time(0, 0),
                'close_time': time(23, 59, 59),
                'source': 'WEEKLY_SCHEDULE',
                'reason': None,
                'is_overnight': False,
            }

        is_overnight = (
            working_hours.open_time is not None and
            working_hours.close_time is not None and
            working_hours.close_time < working_hours.open_time
        )
        return {
            'date': target_date,
            'is_open': True,
            'is_24_hours': False,
            'open_time': working_hours.open_time,
            'close_time': working_hours.close_time,
            'source': 'WEEKLY_SCHEDULE',
            'reason': None,
            'is_overnight': is_overnight,
        }

    @classmethod
    def is_branch_open_at(cls, branch: Branch, target_dt: datetime) -> bool:
        """
        Evaluates whether a branch is open at a specific localized datetime.
        Handles Rule 1 (branch timezone) and Rule 4 (overnight schedule resolution).
        """
        branch_tz = cls.get_branch_timezone(branch)
        if timezone.is_aware(target_dt):
            local_dt = target_dt.astimezone(branch_tz)
        else:
            local_dt = target_dt.replace(tzinfo=branch_tz)

        local_date = local_dt.date()
        local_time = local_dt.time()

        # Check today's schedule
        today_sched = cls.get_effective_schedule_for_date(branch, local_date)
        if today_sched['is_open']:
            if today_sched['is_24_hours']:
                return True
            op = today_sched['open_time']
            cl = today_sched['close_time']
            if not today_sched['is_overnight']:
                if op <= local_time <= cl:
                    return True
            else:
                # Overnight: open today from op until midnight
                if local_time >= op:
                    return True

        # Check yesterday's schedule for overnight spillover into early morning
        yesterday_date = local_date - timedelta(days=1)
        yesterday_sched = cls.get_effective_schedule_for_date(branch, yesterday_date)
        if yesterday_sched['is_open'] and yesterday_sched['is_overnight']:
            # Spills over until cl
            if local_time <= yesterday_sched['close_time']:
                return True

        return False
