"""
Security & Data Masking Utilities for Role-Based Profile Visibility.
Admin & Org Admin: Complete, unmasked profile visibility.
Trainers & Sales: Masked mobile numbers across member profiles, bookings, and rosters.
"""

import re
from typing import Set, Optional

def get_user_role_codes(request, user=None) -> Set[str]:
    if not user:
        if not request or not hasattr(request, 'user') or not getattr(request.user, 'is_authenticated', False):
            return set()
        user = request.user
    role_codes = getattr(user, '_role_codes', None)
    if role_codes is not None:
        return set(role_codes)
    try:
        from .models_rbac import RoleAssignment
        alias = getattr(request, '_tenant_db_alias', None) or (getattr(user, '_state', None) and getattr(user._state, 'db', None)) or 'default'
        role_codes = set(
            RoleAssignment.objects.using(alias)
            .filter(user=user, is_active=True)
            .values_list('role__code', flat=True)
        )
    except Exception:
        role_codes = set()
    return role_codes

def mask_phone_if_restricted(request, raw_phone: Optional[str]) -> str:
    """
    Masks mobile number if user role is restricted (Trainer or Sales).
    Returns unmasked phone for ORG_ADMIN, SUPER_ADMIN, ADMIN, BRANCH_MANAGER.
    Format for restricted roles: +91-XXXXX-1234
    """
    if not raw_phone:
        return ''
    raw_str = str(raw_phone).strip()
    if not raw_str:
        return ''
    if not request or not hasattr(request, 'user') or not getattr(request.user, 'is_authenticated', False):
        return raw_str
    user = request.user
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return raw_str

    role_codes = get_user_role_codes(request, user)
    # Org Admin, Super Admin, Admin, and Branch Manager have complete profile visibility (NOTHING MASKED)
    unmasked_roles = {'ORG_ADMIN', 'SUPER_ADMIN', 'ADMIN', 'BRANCH_MANAGER', 'PLATFORM_SUPERADMIN'}
    if role_codes & unmasked_roles:
        return raw_str

    # For Trainers, Sales Reps, and other non-admin staff, mask phone number
    clean_digits = re.sub(r'[^\d]', '', raw_str)
    if len(clean_digits) >= 4:
        return f"+91-XXXXX-{clean_digits[-4:]}"
    return "+91-XXXXX-XXXX"
