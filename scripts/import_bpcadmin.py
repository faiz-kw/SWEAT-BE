"""
High-performance streaming ETL script to import bpcadmin.sql (MySQL dump)
into SWEAT-BE's dedicated tenant PostgreSQL database (tenant_sweat_demo).
"""

import os
import re
import sys
import html
import uuid
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, time as dt_time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.db import connections, transaction
from django.utils import timezone
from django.contrib.auth.hashers import make_password

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_iam import AuthenticationIdentity
from apps.master.services_auth_directory import sync_tenant_user_identity
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models import (
    Organization, Location, Branch, BranchSettings,
    Department, TenantUser, UserBranch, UserDepartment,
    Role, RoleAssignment, BranchModule, ModuleCatalog,
    UserProfile, EmployeeProfile, TrainerProfile, SalesProfile,
    ProgramCategory, ProgramType, Program, ProgramBranchAvailability,
    Package, PackageVersion, PackagePrice, PackageBranchAvailability,
    PackageEntitlementDefinition,
    DiscountCampaign, DiscountCode,
    LeadSource, Lead, LeadNote, SalesFollowupTask,
    IntakeForm, IntakeQuestion, IntakeSubmission, IntakeAnswer,
    Order, OrderItem, PaymentTransaction,
    Membership, MembershipContractSnapshot, MembershipEntitlement,
    ClassCategory, ClassTemplate, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    Booking, AttendanceRecord,
)
from apps.tenant_core.models_govern import BranchWorkingHours

SQL_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bpcadmin.sql",
)

IST = timezone.get_fixed_timezone(330)  # UTC+05:30
TODAY = date(2026, 10, 3)
NOW = timezone.now()


# ============================================================================
# 1. FAST BINARY OFFSET INDEXER & SQL TUPLE PARSER
# ============================================================================

def index_sql_tables(sql_path: str) -> dict:
    """Scan bpcadmin.sql in 16MB binary chunks to locate exact byte ranges of each table's data dump."""
    t0 = time.time()
    pattern = re.compile(
        rb"-- Dumping data for table `([^`]+)`|-- Table structure for table `([^`]+)`"
    )
    events = []
    offset = 0
    chunk_size = 16 * 1024 * 1024
    overlap = 256
    prev = b""

    with open(sql_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf = prev + chunk
            base_offset = offset - len(prev)
            for m in pattern.finditer(buf):
                pos = base_offset + m.start()
                if m.group(1):
                    events.append(("DUMP", m.group(1).decode("utf-8", errors="replace"), pos))
                else:
                    events.append(("STRUCT", m.group(2).decode("utf-8", errors="replace"), pos))
            prev = chunk[-overlap:]
            offset += len(chunk)

    offsets = {}
    for i, (etype, tname, pos) in enumerate(events):
        if etype == "DUMP":
            end_pos = events[i + 1][2] if i + 1 < len(events) else offset
            if tname not in offsets:
                offsets[tname] = (pos, end_pos)
            else:
                offsets[tname] = (offsets[tname][0], end_pos)

    print(f"[Indexer] Indexed {sql_path} in {time.time() - t0:.2f}s ({len(offsets)} tables with data)")
    return offsets


def parse_sql_tuple(line: str):
    """
    Parse a single MySQL VALUES tuple line `(val1, val2, ...),` or `(val1, val2, ...);`
    into a list of Python values.
    """
    s = line.strip()
    if not s.startswith("("):
        return None
    if s.endswith("),") or s.endswith(");"):
        s = s[1:-2]
    elif s.endswith(")"):
        s = s[1:-1]
    else:
        s = s[1:]

    res = []
    n = len(s)
    i = 0
    while i < n:
        while i < n and s[i] in (" ", "\t", "\r", "\n"):
            i += 1
        if i >= n:
            break

        if s[i] == "'":
            # Quoted string
            i += 1
            buf = []
            while i < n:
                ch = s[i]
                if ch == "\\":
                    i += 1
                    if i < n:
                        esc = s[i]
                        if esc == "n":
                            buf.append("\n")
                        elif esc == "r":
                            buf.append("\r")
                        elif esc == "t":
                            buf.append("\t")
                        elif esc == "0":
                            buf.append("")
                        else:
                            buf.append(esc)
                        i += 1
                elif ch == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    buf.append(ch)
                    i += 1
            res.append("".join(buf))
            while i < n and s[i] != ",":
                i += 1
            if i < n and s[i] == ",":
                i += 1
        else:
            # Unquoted token (NULL, int, float)
            j = i
            while j < n and s[j] != ",":
                j += 1
            token = s[i:j].strip()
            if token.upper() == "NULL" or token == "":
                res.append(None)
            else:
                res.append(token)
            i = j + 1

    return res


def iter_table_rows(sql_path: str, offsets: dict, table_name: str):
    """Yield parsed rows for a specific table using its byte offset range."""
    if table_name not in offsets:
        return
    start_pos, end_pos = offsets[table_name]
    with open(sql_path, "rb") as f:
        f.seek(start_pos)
        raw_bytes = f.read(end_pos - start_pos)
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    buf = ""
    for line in lines:
        stripped = line.strip()
        if not buf:
            if not stripped.startswith("("):
                continue
            if stripped.endswith("),") or stripped.endswith(");"):
                row = parse_sql_tuple(stripped)
                if row is not None:
                    yield row
            else:
                buf = stripped
        else:
            buf += "\n" + stripped
            if stripped.endswith("),") or stripped.endswith(");"):
                row = parse_sql_tuple(buf)
                if row is not None:
                    yield row
                buf = ""


# ============================================================================
# 2. PARSING HELPERS
# ============================================================================

def safe_int(val, default=0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(str(val).strip()))
    except Exception:
        return default


def safe_dec(val, default="0.00") -> Decimal:
    if val is None or val == "":
        return Decimal(default)
    try:
        cleaned = re.sub(r"[^\d.\-]", "", str(val))
        if not cleaned or cleaned in (".", "-", "-."):
            return Decimal(default)
        d = Decimal(cleaned)
        return d.quantize(Decimal("0.01"))
    except Exception:
        return Decimal(default)


def safe_date(val, default=None):
    if not val or not isinstance(val, str):
        return default
    v = val.strip()[:10]
    if v.startswith("0000") or len(v) < 8:
        return default
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(v, fmt).date()
            if 1920 <= d.year <= 2035:
                return d
        except ValueError:
            continue
    return default


def safe_datetime(val, default=None):
    if not val or not isinstance(val, str):
        return default
    v = val.strip()
    if v.startswith("0000") or len(v) < 10:
        return default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v[:19], fmt)
            if 1990 <= dt.year <= 2035:
                return timezone.make_aware(dt, IST)
        except ValueError:
            continue
    return default


def safe_time(val, default=dt_time(7, 0)):
    if not val or not isinstance(val, str):
        return default
    v = val.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(v[:8], fmt).time()
        except ValueError:
            continue
    return default


def clean_phone(val) -> str:
    if not val:
        return ""
    digits = re.sub(r"[^\d+]", "", str(val).strip())
    return digits[:28]


def split_name(full_name: str, default_first="Member"):
    if not full_name:
        return default_first, ""
    cleaned = " ".join(str(full_name).strip().split())
    if not cleaned:
        return default_first, ""
    parts = cleaned.split(" ", 1)
    first = parts[0][:95]
    last = parts[1][:95] if len(parts) > 1 else ""
    return first, last


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(str(raw))
    text = re.sub(r"<[^>]+>", " ", unescaped)
    return " ".join(text.split())


# ============================================================================
# 3. MAIN ETL PIPELINE
# ============================================================================

def run_import():
    t_start = time.time()
    print("=" * 72)
    print("STARTING BPCADMIN.SQL -> SWEAT-BE TENANT DATABASE MIGRATION")
    print("=" * 72)

    tenant = Tenant.objects.using("default").get(slug="sweat-demo")
    ds = TenantDataSource.objects.using("default").filter(tenant=tenant).first()
    alias = build_tenant_db_alias(tenant.id)
    _register_tenant_connection(alias, ds, tenant_id=tenant.id)
    set_tenant_db_alias(alias)

    org = Organization.objects.using(alias).first()
    print(f"[Tenant] Connected to '{tenant.slug}' (alias={alias}, org={org.id})")

    offsets = index_sql_tables(SQL_FILE_PATH)

    # ------------------------------------------------------------------------
    # STEP 0: WIPE EXISTING DEMO DATA (PRESERVING RBAC & SYSTEM LOGINS)
    # ------------------------------------------------------------------------
    print("\n[Step 0] Cleaning existing demo data in tenant_sweat_demo...")
    preserved_emails = {
        "admin@sweat-demo.example",
        "sales@vibecopilot.ai",
        "trainer@vibecopilot.ai",
    }
    preserved_snapshots = []
    for u in TenantUser.objects.using(alias).filter(email__in=preserved_emails):
        r_codes = list(
            RoleAssignment.objects.using(alias)
            .filter(user=u)
            .values_list("role__code", flat=True)
        )
        preserved_snapshots.append((u, r_codes))
    preserved_user_ids = [u.id for u, _ in preserved_snapshots]

    kept_tables = {
        "django_migrations",
        "organizations",
        "organization_settings",
        "module_catalog",
        "submodule_catalog",
        "permissions",
        "roles",
        "role_permission_sets",
        "role_module_access",
        "role_submodule_access",
        "role_permission_set_items",
        "crm_stage_sla_policies",
        "crm_trial_reminder_policies",
        "crm_agent_assignment_configs",
        "crm_stage_automation_rules",
        "crm_attention_policies",
        "processing_purposes",
        "reason_codes",
    }

    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"
        )
        all_pg_tables = [r[0] for r in cursor.fetchall()]
        # Clear any FK from kept_tables to users (e.g. roles.created_by_id, role_permission_sets.created_by_id)
        cursor.execute("UPDATE roles SET created_by_id = NULL, department_id = NULL;")
        cursor.execute(
            "UPDATE role_permission_sets SET created_by_id = NULL, branch_id = NULL, location_id = NULL, company_entity_id = NULL;"
        )
        tables_to_truncate = [t for t in all_pg_tables if t not in kept_tables]
        if tables_to_truncate:
            quoted = ", ".join(f'"{t}"' for t in tables_to_truncate)
            cursor.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE;")

    # Clean AuthenticationIdentity in default DB for non-preserved users of this tenant
    AuthenticationIdentity.objects.using("default").filter(
        tenant_id=tenant.id
    ).exclude(subject_id__in=preserved_user_ids).delete()
    print(f"[Step 0] Truncated {len(tables_to_truncate)} domain tables cleanly.")

    # ------------------------------------------------------------------------
    # STEP 1: ORGANIZATION, LOCATIONS & BRANCHES (`branch` table)
    # ------------------------------------------------------------------------
    print("\n[Step 1] Importing Locations & Branches...")
    org.name = "SWEAT Fitness & Wellness"
    org.legal_name = "SWEAT Fitness & Wellness Pvt Ltd"
    org.email = "info@sweatfitwellness.com"
    org.phone = "9920012533"
    org.status = "ACTIVE"
    org.save(using=alias)

    location_map = {}  # area_name -> Location
    branch_map = {}    # legacy branch uid (int) -> Branch
    all_modules = list(ModuleCatalog.objects.using(alias).all())

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "branch"):
        # 0:uid, 1:branch_name, 2:franchise_share_percent, 3:contact, 4:location,
        # 5:email, 6:description, 7:lat_long, 8:maps_link, 9:manager_id, 10:block,
        # 11:createdon, 12:blockedon, 13:updatedon, 14:slug
        b_uid = safe_int(row[0])
        b_name = (row[1] or f"Branch {b_uid}").strip()
        b_contact = clean_phone(row[3]) or "9920012533"
        b_loc_name = (row[4] or "Mumbai").strip()
        b_email = (row[5] or "info@sweatfitwellness.com").strip().lower()
        b_desc = (row[6] or "").strip()
        b_lat_long = (row[7] or "").strip()
        b_block = safe_int(row[10], 0)
        b_slug = (row[14] or f"branch-{b_uid}").strip()

        if b_loc_name not in location_map:
            loc_code = re.sub(r"[^A-Z0-9]+", "_", b_loc_name.upper()).strip("_") or f"LOC_{b_uid}"
            loc = Location.objects.using(alias).create(
                organization=org,
                code=loc_code,
                name=f"{b_loc_name} West" if b_loc_name in ("Goregaon", "Andheri", "Malad") else b_loc_name,
                city="Mumbai" if b_loc_name != "Online" else "Online",
                area=b_loc_name,
                state="Maharashtra",
                country="IN",
                status="ACTIVE",
                activated_at=NOW,
            )
            location_map[b_loc_name] = loc
        else:
            loc = location_map[b_loc_name]

        lat, lng = None, None
        if "," in b_lat_long:
            parts = [p.strip() for p in b_lat_long.split(",")]
            try:
                lat = Decimal(parts[0]).quantize(Decimal("0.0000001"))
                lng = Decimal(parts[1]).quantize(Decimal("0.0000001"))
            except Exception:
                lat, lng = None, None

        # Ensure one and only one branch per location area with normalized location name
        norm_branch_names = {
            "Goregaon": ("Sweat Goregaon West", "SWEAT_GOREGAON_WEST"),
            "Andheri": ("Sweat Andheri West", "SWEAT_ANDHERI_WEST"),
            "Malad": ("Sweat Malad West", "SWEAT_MALAD_WEST"),
            "Online": ("Sweat Online", "SWEAT_ONLINE"),
        }
        b_display_name, b_code = norm_branch_names.get(
            b_loc_name,
            (b_name, (re.sub(r"[^A-Z0-9]+", "_", b_slug.upper()).strip("_")[:45] or f"BR_{b_uid}") + f"_{b_uid}"),
        )
        existing_loc_branch = Branch.objects.using(alias).filter(organization=org, location=loc).first()
        if existing_loc_branch:
            existing_loc_branch.name = b_display_name
            existing_loc_branch.code = b_code
            existing_loc_branch.capacity = max(existing_loc_branch.capacity or 15, 30 if b_loc_name == "Goregaon" else 15)
            existing_loc_branch.save(using=alias)
            branch_map[b_uid] = existing_loc_branch
            continue
        branch = Branch.objects.using(alias).create(
            organization=org,
            location=loc,
            code=b_code,
            name=b_display_name,
            address=f"{b_display_name}, {loc.name}, Mumbai",
            address_line_1=b_display_name,
            address_line_2=f"{loc.name}, Mumbai",
            latitude=lat,
            longitude=lng,
            geofence_radius_meters=250,
            geofence_enforcement="FLAG_AUDIT",
            timezone="Asia/Kolkata",
            phone=b_contact,
            email=b_email,
            capacity=30 if ("Bootcamp" in b_name or b_loc_name == "Goregaon") else 15,
            business_open_time="06:00",
            business_close_time="22:00",
            is_passport_eligible=True,
            status="ACTIVE" if b_block == 0 else "INACTIVE",
            activated_at=safe_datetime(row[11], NOW),
        )
        branch_map[b_uid] = branch

        BranchSettings.objects.using(alias).create(
            branch=branch,
            business_open_time="06:00",
            business_close_time="22:00",
            max_booking_capacity=branch.capacity,
            contact_email=b_email,
            contact_phone=b_contact,
            whatsapp_number=b_contact,
        )

        # Working hours Mon(1) - Sun(7)
        wh_list = []
        for dow in range(1, 8):
            wh_list.append(
                BranchWorkingHours(
                    branch=branch,
                    day_of_week=dow,
                    is_open=True,
                    open_time=dt_time(6, 0),
                    close_time=dt_time(22, 0),
                )
            )
        BranchWorkingHours.objects.using(alias).bulk_create(wh_list)

        # Enable all modules for this branch
        bm_list = []
        for mod in all_modules:
            mcode = mod.module_code or mod.code
            if mcode:
                bm_list.append(
                    BranchModule(
                        branch=branch,
                        module=mod,
                        module_code=mcode[:50],
                        status="ENABLED",
                        is_enabled=True,
                    )
                )
        BranchModule.objects.using(alias).bulk_create(bm_list, ignore_conflicts=True)

    default_branch = branch_map.get(3) or branch_map.get(1) or list(branch_map.values())[0]
    print(f"[Step 1] Imported {len(branch_map)} branches: {[b.name for b in branch_map.values()]}")

    # Re-create preserved users with their exact UUIDs and attach to default_branch
    roles_by_code = {r.code: r for r in Role.objects.using(alias).all()}
    seen_emails = set()
    seen_usernames = set()
    admin_user = None

    for pu, r_codes in preserved_snapshots:
        seen_emails.add(pu.email.lower())
        if pu.username:
            seen_usernames.add(pu.username.lower())
        pu.home_branch = default_branch
        pu.deactivated_by = None
        pu.profile_file_id = None
        TenantUser.objects.using(alias).bulk_create([pu])
        if pu.email.lower() == "admin@sweat-demo.example":
            admin_user = pu
        for rc in r_codes:
            if rc in roles_by_code:
                ro = roles_by_code[rc]
                RoleAssignment.objects.using(alias).bulk_create([
                    RoleAssignment(
                        id=uuid.uuid4(),
                        organization=org,
                        user=pu,
                        role=ro,
                        scope_type="ORGANIZATION" if ro.scope == "ORG" else "BRANCH",
                        branch=None if ro.scope == "ORG" else default_branch,
                        status="ACTIVE",
                        is_active=True,
                        assigned_at=NOW,
                    )
                ])
        uprof = UserProfile.objects.using(alias).create(
            user=pu,
            first_name_snapshot=pu.first_name,
            last_name_snapshot=pu.last_name,
            preferred_branch=default_branch,
            member_status="ACTIVE",
        )
        emp = EmployeeProfile.objects.using(alias).create(
            user_profile=uprof,
            organization=org,
            employee_code=f"SYS-{str(pu.id)[:6].upper()}",
            joining_date=date(2024, 1, 1),
            employment_type="FULL_TIME",
            designation="System Staff",
            employment_status="ACTIVE",
        )
        if "trainer" in pu.email.lower():
            TrainerProfile.objects.using(alias).create(
                employee_profile=emp,
                trainer_code=f"TR-SYS-{str(pu.id)[:4].upper()}",
                bio="System Trainer",
                trainer_status="ACTIVE",
                can_teach_all_specialties=True,
            )
        elif "sales" in pu.email.lower():
            SalesProfile.objects.using(alias).create(
                employee_profile=emp,
                sales_code=f"SL-SYS-{str(pu.id)[:4].upper()}",
                sales_type="INSIDE_SALES",
                lead_assignment_enabled=True,
                sales_status="ACTIVE",
            )

    # ------------------------------------------------------------------------
    # STEP 2: STAFF, ADMINS, TRAINERS & SALES AGENTS (`agents` table)
    # ------------------------------------------------------------------------
    print("\n[Step 2] Importing Staff, Trainers, Sales Agents & Admins from `agents`...")
    dept_mgmt = Department.objects.using(alias).create(
        organization=org, name="Management & Admin", code="MANAGEMENT", status="ACTIVE"
    )
    dept_train = Department.objects.using(alias).create(
        organization=org, name="Fitness & Coaching", code="TRAINING", status="ACTIVE"
    )
    dept_sales = Department.objects.using(alias).create(
        organization=org, name="Sales & CRM", code="SALES", status="ACTIVE"
    )

    # Ensure canonical roles exist
    for rcode, rname, rscope in [
        ("ORG_ADMIN", "Organization Administrator", "ORG"),
        ("BRANCH_MANAGER", "Branch Manager", "BRANCH"),
        ("TRAINER", "Trainer", "BRANCH"),
        ("SALES_REP", "Sales & CRM Representative", "BRANCH"),
    ]:
        if rcode not in roles_by_code:
            roles_by_code[rcode] = Role.objects.using(alias).create(
                organization=org, code=rcode, name=rname, scope=rscope, status="ACTIVE"
            )

    default_staff_hash = make_password("SweatStaff@123!")
    agent_user_map = {}       # agent_uid -> TenantUser
    agent_trainer_map = {}    # agent_uid -> TrainerProfile
    agent_branch_map = {}     # agent_uid -> Branch

    staff_users_to_create = []
    staff_profiles_to_create = []
    emp_profiles_to_create = []
    trainer_profiles_to_create = []
    sales_profiles_to_create = []
    role_assignments_to_create = []
    user_branches_to_create = []
    user_depts_to_create = []
    active_staff_for_auth_sync = []

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "agents"):
        # 0:uid, 1:name, 2:email, 3:number, 4:commission, 5:pass, 6:role, 7:block,
        # 8:blockedon, 9:updatedon, 10:createdon, 11:lastlogin, 12:calling_active,
        # 13:exo_id, 14:role_id, 15:email_daily_sales_report, 16:branch_id
        a_uid = safe_int(row[0])
        a_name = (row[1] or f"Staff {a_uid}").strip()
        raw_email = (row[2] or "").strip().lower()
        a_phone = clean_phone(row[3])
        raw_pass = (row[5] or "").strip()
        raw_role = (row[6] or "").strip().lower()
        a_block = safe_int(row[7], 0)
        a_created = safe_datetime(row[10], NOW)
        a_lastlogin = safe_datetime(row[11], None)
        a_role_id = safe_int(row[14], 0)
        a_branch_id = safe_int(row[16], 0)

        first_name, last_name = split_name(a_name, default_first="Staff")
        if not raw_email or "@" not in raw_email or raw_email in seen_emails:
            email = f"staff_{a_uid}@sweatfitwellness.local"
        else:
            email = raw_email
        seen_emails.add(email)

        # Resolve role & department
        if a_role_id == 14 or "admin" in raw_role:
            role_obj = roles_by_code["ORG_ADMIN"]
            dept_obj = dept_mgmt
            designation = "Administrator"
        elif a_role_id == 17 or "manager" in raw_role:
            role_obj = roles_by_code["BRANCH_MANAGER"]
            dept_obj = dept_mgmt
            designation = "Branch Manager"
        elif a_role_id == 15 or "trainer" in raw_role or "coach" in raw_role or "instructor" in raw_role:
            role_obj = roles_by_code["TRAINER"]
            dept_obj = dept_train
            designation = "Fitness Trainer"
        else:
            role_obj = roles_by_code["SALES_REP"]
            dept_obj = dept_sales
            designation = "Sales Executive"

        home_br = branch_map.get(a_branch_id) or default_branch
        agent_branch_map[a_uid] = home_br
        is_active_staff = (a_block == 0)

        # Only hash individual password for active staff (fast: only 27 active rows)
        if role_obj.code == "ORG_ADMIN":
            pw_hash = make_password("SweatAdmin@123!")
        elif is_active_staff and raw_pass and not raw_pass.startswith("$2"):
            pw_hash = make_password(raw_pass)
        else:
            pw_hash = default_staff_hash

        u_id = uuid.uuid4()
        tu = TenantUser(
            id=u_id,
            organization=org,
            username=None,
            email=email,
            phone=a_phone,
            first_name=first_name,
            last_name=last_name,
            display_name=a_name[:145],
            password_hash=pw_hash,
            user_type="STAFF",
            status="ACTIVE" if is_active_staff else "INACTIVE",
            is_login_allowed=is_active_staff,
            last_login_at=a_lastlogin,
            activated_at=a_created,
            home_branch=home_br,
            created_at=a_created,
            updated_at=NOW,
        )
        staff_users_to_create.append(tu)
        agent_user_map[a_uid] = tu
        if is_active_staff:
            active_staff_for_auth_sync.append(tu)
            if not admin_user and role_obj.code == "ORG_ADMIN":
                admin_user = tu

        up_id = uuid.uuid4()
        uprof = UserProfile(
            id=up_id,
            user=tu,
            first_name_snapshot=first_name,
            last_name_snapshot=last_name,
            preferred_branch=home_br,
            joining_date=a_created.date(),
            member_status="ACTIVE" if is_active_staff else "INACTIVE",
            legacy_reference=f"agent:{a_uid}",
            created_at=a_created,
        )
        staff_profiles_to_create.append(uprof)

        ep_id = uuid.uuid4()
        emp = EmployeeProfile(
            id=ep_id,
            user_profile=uprof,
            organization=org,
            employee_code=f"EMP-{a_uid:04d}",
            joining_date=a_created.date(),
            hire_date=a_created.date(),
            employment_type="FULL_TIME",
            designation=designation,
            employment_status="ACTIVE" if is_active_staff else "INACTIVE",
            created_at=a_created,
        )
        emp_profiles_to_create.append(emp)

        # Create TrainerProfile for all staff so any historical slot referencing agentid resolves cleanly,
        # but mark trainer_status='ACTIVE' only for active Trainers/Founders
        is_trainer_active = is_active_staff and (role_obj.code == "TRAINER" or a_uid in (6, 398))
        tp = TrainerProfile(
            id=uuid.uuid4(),
            employee_profile=emp,
            trainer_code=f"TR-{a_uid:04d}",
            bio=f"{a_name} — {designation} at {home_br.name}",
            experience_years=Decimal("3.00"),
            trainer_status="ACTIVE" if is_trainer_active else "INACTIVE",
            can_teach_all_specialties=True,
            created_at=a_created,
        )
        trainer_profiles_to_create.append(tp)
        agent_trainer_map[a_uid] = tp

        if role_obj.code in ("SALES_REP", "BRANCH_MANAGER", "ORG_ADMIN"):
            sp = SalesProfile(
                id=uuid.uuid4(),
                employee_profile=emp,
                sales_code=f"SL-{a_uid:04d}",
                sales_type="INSIDE_SALES",
                target_enabled=True,
                lead_assignment_enabled=is_active_staff,
                sales_status="ACTIVE" if is_active_staff else "INACTIVE",
                created_at=a_created,
            )
            sales_profiles_to_create.append(sp)

        ra_scope = "ORGANIZATION" if role_obj.scope == "ORG" or a_branch_id == 0 else "BRANCH"
        role_assignments_to_create.append(
            RoleAssignment(
                id=uuid.uuid4(),
                organization=org,
                user=tu,
                role=role_obj,
                scope_type=ra_scope,
                branch=None if ra_scope == "ORGANIZATION" else home_br,
                department=dept_obj,
                status="ACTIVE" if is_active_staff else "INACTIVE",
                is_active=is_active_staff,
                assigned_at=a_created,
            )
        )
        user_branches_to_create.append(
            UserBranch(
                id=uuid.uuid4(),
                user=tu,
                branch=home_br,
                relationship_type="PRIMARY",
                scope_type="HOME",
                is_primary=True,
                status="ACTIVE" if is_active_staff else "INACTIVE",
                is_active=is_active_staff,
                assigned_at=a_created,
            )
        )
        user_depts_to_create.append(
            UserDepartment(
                id=uuid.uuid4(),
                user=tu,
                department=dept_obj,
                is_primary=True,
                status="ACTIVE" if is_active_staff else "INACTIVE",
                assigned_at=a_created,
            )
        )

    TenantUser.objects.using(alias).bulk_create(staff_users_to_create, batch_size=1000)
    UserProfile.objects.using(alias).bulk_create(staff_profiles_to_create, batch_size=1000)
    EmployeeProfile.objects.using(alias).bulk_create(emp_profiles_to_create, batch_size=1000)
    TrainerProfile.objects.using(alias).bulk_create(trainer_profiles_to_create, batch_size=1000)
    SalesProfile.objects.using(alias).bulk_create(sales_profiles_to_create, batch_size=1000)
    RoleAssignment.objects.using(alias).bulk_create(role_assignments_to_create, batch_size=1000)
    UserBranch.objects.using(alias).bulk_create(user_branches_to_create, batch_size=1000)
    UserDepartment.objects.using(alias).bulk_create(user_depts_to_create, batch_size=1000)

    for tu in active_staff_for_auth_sync:
        try:
            sync_tenant_user_identity(tu, tenant_id=tenant.id, db=alias)
        except Exception as e:
            print(f"  [Warn] Auth identity sync skipped for {tu.email}: {e}")

    if not admin_user:
        admin_user = staff_users_to_create[0]
    print(
        f"[Step 2] Imported {len(staff_users_to_create)} staff accounts "
        f"({len(active_staff_for_auth_sync)} active synced to login directory)."
    )

    # ------------------------------------------------------------------------
    # STEP 3: CATALOG (`types`, `plans`, `sessions`, `coupons`)
    # ------------------------------------------------------------------------
    print("\n[Step 3] Importing Catalog (Categories, Programs, Packages, Versions, Prices, Coupons)...")
    type_cat_map = {}   # type_uid -> ProgramCategory
    type_pt_map = {}    # type_uid -> ProgramType

    for idx, row in enumerate(iter_table_rows(SQL_FILE_PATH, offsets, "types"), 1):
        # 0:uid, 1:typename, 2:typeimg, 3:typedesc, 4:block, ..., 10:slug
        t_uid = safe_int(row[0])
        t_name = (row[1] or f"Category {t_uid}").strip()
        t_desc = clean_html(row[3])
        t_block = safe_int(row[4], 0)
        t_code = re.sub(r"[^A-Z0-9]+", "_", t_name.upper()).strip("_")[:80] + f"_{t_uid}"

        pcat = ProgramCategory.objects.using(alias).create(
            organization=org,
            code=t_code,
            name=t_name,
            description=t_desc,
            display_order=idx,
            status="ACTIVE" if t_block == 0 else "INACTIVE",
        )
        ptype = ProgramType.objects.using(alias).create(
            organization=org,
            code=t_code,
            name=t_name,
            description=t_desc,
            display_order=idx,
            status="ACTIVE" if t_block == 0 else "INACTIVE",
        )
        type_cat_map[t_uid] = pcat
        type_pt_map[t_uid] = ptype

    default_pcat = list(type_cat_map.values())[0]
    default_ptype = list(type_pt_map.values())[0]

    plan_map = {}        # plan_uid -> Program
    plan_branch_map = {} # plan_uid -> Branch
    plan_name_lookup = {} # lower_name -> Program

    for idx, row in enumerate(iter_table_rows(SQL_FILE_PATH, offsets, "plans"), 1):
        # 0:uid, 1:typeid, 2:branch_id, 3:planname, 4:slug, ..., 12:istrial, 15:description, 23:block, 24:createdon
        p_uid = safe_int(row[0])
        p_typeid = safe_int(row[1])
        p_branch_id = safe_int(row[2])
        raw_pname = (row[3] or f"Program {p_uid}").strip()
        p_desc = clean_html(row[15])
        p_block = safe_int(row[23], 0)

        br = branch_map.get(p_branch_id) or default_branch
        plan_branch_map[p_uid] = br

        # Disambiguate branch-specific Sweat Pilates programs while keeping clean display names
        if raw_pname.lower() == "sweat pilates" and p_branch_id in branch_map:
            loc_label = branch_map[p_branch_id].location.area
            pname = f"Sweat Pilates ({loc_label})"
        else:
            pname = raw_pname

        p_code = re.sub(r"[^A-Z0-9]+", "_", pname.upper()).strip("_")[:80] + f"_{p_uid}"
        pcat = type_cat_map.get(p_typeid) or default_pcat
        ptype = type_pt_map.get(p_typeid) or default_ptype

        if "1:1" in pname or "personal" in pname.lower() or "face to face" in pname.lower():
            deliv = "INDIVIDUAL_SERVICE"
        else:
            deliv = "GROUP_CLASS"

        prog = Program.objects.using(alias).create(
            organization=org,
            category=pcat,
            program_type=ptype,
            legacy_program_type=ptype.code[:40],
            code=p_code,
            name=pname,
            description=p_desc,
            delivery_mode=deliv,
            display_order=idx,
            trial_allowed=True,
            status="ACTIVE",
        )
        plan_map[p_uid] = prog
        plan_name_lookup[raw_pname.lower()] = prog
        plan_name_lookup[str(p_uid)] = prog

        ProgramBranchAvailability.objects.using(alias).get_or_create(
            program=prog,
            branch=br,
            defaults={"is_active": True, "effective_from": NOW},
        )

    # Helper to get or create fallback Program for legacy planids (e.g. 1, 5, 7, 8, 15, 16, 17, 19)
    legacy_plan_meta = {
        1: ("Bootcamp Classic Plan", 10, "GROUP_CLASS"),
        5: ("Sweat Annual Transformation Plan", 2, "GROUP_CLASS"),
        7: ("Founder 1:1 Elite Training Plan A", 3, "INDIVIDUAL_SERVICE"),
        8: ("Founder 1:1 Elite Training Plan B", 3, "INDIVIDUAL_SERVICE"),
        15: ("Bootcamp Starter 8-Session Plan", 10, "GROUP_CLASS"),
        16: ("1:1 Team Personal Training Plan", 7, "INDIVIDUAL_SERVICE"),
        17: ("Bootcamp 1:1 Personal Training", 7, "INDIVIDUAL_SERVICE"),
        19: ("Sweat Online Volume Program", 2, "GROUP_CLASS"),
    }

    def get_or_create_program(plan_uid: int) -> Program:
        if plan_uid in plan_map:
            return plan_map[plan_uid]
        pname, t_uid, deliv = legacy_plan_meta.get(
            plan_uid, (f"Fitness Program #{plan_uid}", 2, "GROUP_CLASS")
        )
        pcat = type_cat_map.get(t_uid) or default_pcat
        ptype = type_pt_map.get(t_uid) or default_ptype
        prog = Program.objects.using(alias).create(
            organization=org,
            category=pcat,
            program_type=ptype,
            legacy_program_type="MEMBERSHIP",
            code=f"LEGACY_PLAN_{plan_uid}",
            name=pname,
            delivery_mode=deliv,
            display_order=99 + plan_uid,
            trial_allowed=True,
            status="ACTIVE",
        )
        plan_map[plan_uid] = prog
        plan_branch_map[plan_uid] = default_branch
        ProgramBranchAvailability.objects.using(alias).get_or_create(
            program=prog,
            branch=default_branch,
            defaults={"is_active": True, "effective_from": NOW},
        )
        return prog

    # Import `sessions` -> Package + PackageVersion + PackagePrice + PackageBranchAvailability + PackageEntitlementDefinition
    session_pkg_map = {}  # session_uid -> (Package, PackageVersion, PackagePrice, PackageEntitlementDefinition)
    for row in iter_table_rows(SQL_FILE_PATH, offsets, "sessions"):
        # 0:uid, 1:planid, 2:name, 3:daystime, 4:notouser, 5:cost, 6:discount, 7:coupons,
        # 8:max_sessions, 9:istrial, 10:incl_tax, 11:taxname, 12:not_toweb, 13:not_toapp,
        # 14:block, 15:createdon, 16:updatedon, 17:blockedon, 18:max_passport_sessions,
        # 19:passport_session_cost, 20:only_to_trial, 21:taxpercent
        s_uid = safe_int(row[0])
        s_planid = safe_int(row[1])
        raw_sname = (row[2] or f"Package {s_uid}").strip()
        s_days = max(1, safe_int(row[3], 30))
        s_cost = max(Decimal("0.00"), safe_dec(row[5], "0.00"))
        s_mrp = max(Decimal("0.00"), safe_dec(row[6], "0.00"))
        s_max_sess = safe_int(row[8], 0)
        s_istrial = bool(safe_int(row[9], 0)) or ("trial" in raw_sname.lower())
        s_incl_tax = bool(safe_int(row[10], 0))
        s_not_web = bool(safe_int(row[12], 0))
        s_not_app = bool(safe_int(row[13], 0))
        s_block = safe_int(row[14], 0)
        s_created = safe_datetime(row[15], NOW)
        s_passport_sess = safe_int(row[18], 0)
        s_only_trial = bool(safe_int(row[20], 0))
        s_tax_pct = max(Decimal("0.000"), safe_dec(row[21], "5.00"))

        prog = get_or_create_program(s_planid)
        br = plan_branch_map.get(s_planid) or default_branch

        if raw_sname.isdigit():
            pkg_display_name = f"{prog.name} - {raw_sname} Sessions"
            if s_max_sess <= 0:
                s_max_sess = int(raw_sname)
        else:
            pkg_display_name = f"{prog.name} - {raw_sname}"
            if s_max_sess <= 0:
                m_num = re.search(r"(\d+)\s*session", raw_sname.lower())
                if m_num:
                    s_max_sess = int(m_num.group(1))
                elif s_istrial:
                    s_max_sess = 1
                else:
                    s_max_sess = max(1, s_days)

        pkg_code = re.sub(r"[^A-Z0-9]+", "_", f"PKG_{s_planid}_{raw_sname}".upper()).strip("_")[:80] + f"_{s_uid}"
        pkg = Package.objects.using(alias).create(
            organization=org,
            program=prog,
            code=pkg_code,
            name=pkg_display_name[:195],
            status="ACTIVE",
            created_at=s_created,
        )

        pver = PackageVersion(
            id=uuid.uuid4(),
            package=pkg,
            version_number=1,
            name_snapshot=pkg_display_name[:195],
            description_snapshot=f"{pkg_display_name} ({s_days} days validity, {s_max_sess} sessions)",
            duration_value=s_days,
            duration_unit="DAY",
            total_days=s_days,
            validity_days=s_days,
            is_trial_package=s_istrial,
            is_trial=s_istrial,
            only_for_trial=s_only_trial,
            show_on_web=not s_not_web,
            show_on_app=not s_not_app,
            effective_from=s_created,
            status="ACTIVE",
            published_at=s_created,
            created_by_user=admin_user,
            created_at=s_created,
        )
        PackageVersion.objects.using(alias).bulk_create([pver])

        pprice = PackagePrice.objects.using(alias).create(
            package_version=pver,
            branch=br,
            currency="INR",
            base_price=s_cost,
            display_price=s_mrp if s_mrp > s_cost else s_cost,
            prices_include_tax=s_incl_tax,
            tax_percent=s_tax_pct,
            effective_from=s_created,
            status="ACTIVE",
            created_by_user=admin_user,
            created_at=s_created,
        )

        PackageBranchAvailability.objects.using(alias).create(
            package=pkg,
            branch=br,
            status="ENABLED" if s_block == 0 else "DISABLED",
            available_from=s_created,
        )

        ent_def = PackageEntitlementDefinition.objects.using(alias).create(
            package_version=pver,
            entitlement_type="CLASS_SESSION",
            allocated_units=Decimal(str(max(1, s_max_sess))),
            is_unlimited=False,
            validity_days=s_days,
            configuration={"max_passport_sessions": s_passport_sess},
            status="ACTIVE",
            created_at=s_created,
        )
        session_pkg_map[s_uid] = (pkg, pver, pprice, ent_def)

    default_pkg_tuple = session_pkg_map.get(31) or list(session_pkg_map.values())[0]

    # Import `coupons` -> DiscountCampaign + DiscountCode
    coupon_count = 0
    for row in iter_table_rows(SQL_FILE_PATH, offsets, "coupons"):
        # 0:uid, 1:label, 2:couponcode, 3:promotext, 4:flatorpercent, 5:value,
        # 6:uselimit, 7:amountlimit, 8:mincartvalue, 9:percentupto, ..., 14:block, 17:createdon
        c_uid = safe_int(row[0])
        c_label = (row[1] or f"Campaign {c_uid}").strip()
        c_code = (row[2] or f"CODE{c_uid}").strip().upper()
        c_promo = (row[3] or "").strip()
        c_type = "PERCENTAGE" if "percent" in str(row[4] or "").lower() else "FIXED"
        c_val = max(Decimal("0.00"), safe_dec(row[5], "10.00"))
        c_uselimit = safe_int(row[6], 100)
        c_mincart = safe_dec(row[8], "0.00")
        c_block = safe_int(row[14], 0)
        c_created = safe_datetime(row[17], NOW)

        camp = DiscountCampaign.objects.using(alias).create(
            organization=org,
            name=c_label[:195],
            description=c_promo,
            discount_type=c_type,
            discount_value=c_val,
            minimum_order_amount=c_mincart,
            usage_limit=c_uselimit if c_uselimit > 0 else None,
            valid_from=c_created,
            status="ACTIVE" if c_block == 0 else "EXPIRED",
        )
        DiscountCode.objects.using(alias).create(
            campaign=camp,
            code=f"{c_code}" if c_block == 0 else f"{c_code}_{c_uid}",
            status="ACTIVE" if c_block == 0 else "INACTIVE",
        )
        coupon_count += 1

    print(
        f"[Step 3] Imported {len(type_cat_map)} categories, {len(plan_map)} programs, "
        f"{len(session_pkg_map)} packages/versions/prices, {coupon_count} coupons."
    )

    # ------------------------------------------------------------------------
    # STEP 4: MEMBERS & PAR-Q (`users` & `par_q_forms`)
    # ------------------------------------------------------------------------
    print("\n[Step 4] Importing Members (`users` table - 67,763 rows)...")
    t_mem = time.time()
    default_member_hash = make_password("SweatMember@123!")
    user_map = {}  # legacy user uid -> (user_id, profile_id)

    member_users_batch = []
    member_profiles_batch = []
    total_members = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "users"):
        # 0:uid, 1:firstname, 2:lastname, 3:gender, 4:birthday, 5:country, 6:email,
        # 7:contact, 8:location, 9:goal, 10:billingname, 11:gstno, 12:panno,
        # 13:password, 14:istrial, 15:isverified, ..., 20:block, 21:createdon, 22:lastlogin
        u_uid = safe_int(row[0])
        fname = (row[1] or "").strip()[:95]
        lname = (row[2] or "").strip()[:95]
        if not fname and not lname:
            fname = f"Member #{u_uid}"
        gender = (row[3] or "").strip()[:20] or None
        dob = safe_date(row[4], None)
        raw_email = (row[6] or "").strip().lower()
        phone = clean_phone(row[7])
        loc_text = (row[8] or "").strip()
        goal_text = (row[9] or "").strip()
        istrial = bool(safe_int(row[14], 0))
        u_block = safe_int(row[20], 0)
        u_created = safe_datetime(row[21], NOW)
        u_lastlogin = safe_datetime(row[22], None)

        if not raw_email or "@" not in raw_email or len(raw_email) > 300 or raw_email in seen_emails:
            email = f"member_{u_uid}@sweatfitwellness.local"
        else:
            email = raw_email
        seen_emails.add(email)

        # Match preferred branch from location text if possible
        pref_br = default_branch
        loc_lower = loc_text.lower()
        if "andheri" in loc_lower and 2 in branch_map:
            pref_br = branch_map[2]
        elif "malad" in loc_lower and 5 in branch_map:
            pref_br = branch_map[5]
        elif "online" in loc_lower and 6 in branch_map:
            pref_br = branch_map[6]

        u_id = uuid.uuid4()
        up_id = uuid.uuid4()
        user_map[u_uid] = (u_id, up_id)

        member_users_batch.append(
            TenantUser(
                id=u_id,
                organization=org,
                username=None,
                email=email,
                phone=phone,
                first_name=fname,
                last_name=lname,
                display_name=f"{fname} {lname}".strip()[:145],
                gender=gender,
                date_of_birth=dob,
                password_hash=default_member_hash,
                user_type="MEMBER",
                status="ACTIVE" if u_block == 0 else "BLOCKED",
                is_login_allowed=(u_block == 0),
                last_login_at=u_lastlogin,
                activated_at=u_created,
                home_branch=pref_br,
                created_at=u_created,
                updated_at=u_created,
            )
        )
        member_profiles_batch.append(
            UserProfile(
                id=up_id,
                user_id=u_id,
                member_number=f"MEM-{u_uid:06d}",
                first_name_snapshot=fname,
                last_name_snapshot=lname,
                gender=gender,
                date_of_birth=dob,
                address_json={"location": loc_text, "country": (row[5] or "India"), "goal": goal_text},
                preferred_branch=pref_br,
                joining_date=u_created.date(),
                member_type="TRIAL" if istrial else "MEMBER",
                acquisition_source="LEGACY_IMPORT",
                member_status="ACTIVE" if u_block == 0 else "INACTIVE",
                legacy_reference=f"user:{u_uid}",
                created_at=u_created,
                updated_at=u_created,
            )
        )
        total_members += 1

        if len(member_users_batch) >= 4000:
            TenantUser.objects.using(alias).bulk_create(member_users_batch, batch_size=4000)
            UserProfile.objects.using(alias).bulk_create(member_profiles_batch, batch_size=4000)
            member_users_batch.clear()
            member_profiles_batch.clear()

    if member_users_batch:
        TenantUser.objects.using(alias).bulk_create(member_users_batch, batch_size=4000)
        UserProfile.objects.using(alias).bulk_create(member_profiles_batch, batch_size=4000)
        member_users_batch.clear()
        member_profiles_batch.clear()

    print(f"[Step 4a] Imported {total_members} members in {time.time() - t_mem:.1f}s.")

    # Import PAR-Q forms (`par_q_forms` -> 1,018 rows)
    print("[Step 4b] Importing PAR-Q Health Intake Forms (`par_q_forms` - 1,018 rows)...")
    parq_form = IntakeForm.objects.using(alias).create(
        organization=org,
        name="PAR-Q & Health Readiness Questionnaire",
        form_type="PAR_Q",
        version_number=1,
        effective_from=NOW,
        status="ACTIVE",
    )
    q_specs = [
        ("height_cm", "Height (cm)", "NUMBER", "FITNESS", False),
        ("weight_kg", "Weight (kg)", "NUMBER", "FITNESS", False),
        ("has_medical_conditions", "Do you have any medical conditions?", "BOOLEAN", "MEDICAL", True),
        ("medical_conditions", "Medical conditions details", "TEXT", "MEDICAL", True),
        ("on_medication", "Are you currently on any medication?", "BOOLEAN", "MEDICAL", True),
        ("recent_surgeries", "Have you had any recent surgeries?", "BOOLEAN", "MEDICAL", True),
        ("surgeries_details", "Recent surgeries details", "TEXT", "MEDICAL", True),
        ("is_pregnant_or_postpartum", "Are you pregnant or postpartum?", "BOOLEAN", "MEDICAL", True),
        ("has_pain_discomfort", "Do you experience pain or discomfort during exercise?", "BOOLEAN", "MEDICAL", True),
        ("primary_goal", "Primary Fitness Goal", "TEXT", "FITNESS", False),
        ("workout_frequency", "Current Workout Frequency", "TEXT", "LIFESTYLE", False),
        ("fitness_level", "Current Fitness Level", "TEXT", "FITNESS", False),
        ("how_sweat_can_help", "How can SWEAT help you?", "TEXT", "PSYCHOLOGY", False),
    ]
    q_map = {}
    for idx, (qkey, qtext, qtype, qcat, qsens) in enumerate(q_specs, 1):
        q_map[qkey] = IntakeQuestion.objects.using(alias).create(
            intake_form=parq_form,
            question_text=qtext,
            question_type=qtype,
            category=qcat,
            is_sensitive=qsens,
            display_order=idx,
            status="ACTIVE",
        )

    subs_batch = []
    ans_batch = []
    for row in iter_table_rows(SQL_FILE_PATH, offsets, "par_q_forms"):
        # 0:uid, 1:userid, 2:name, 3:phone_number, 4:email, ..., 7:height_cm,
        # 8:has_medical_conditions, 9:medical_conditions, 10:on_medication,
        # 11:recent_surgeries, 12:surgeries_details, 13:is_pregnant_or_postpartum,
        # 14:has_pain_discomfort, 15:primary_goal, 16:workout_frequency,
        # 17:fitness_level, 18:has_pilates_or_bootcamp_experience,
        # 19:how_sweat_can_help, 20:submitted_at, ..., 23:weight_kg
        pq_userid = safe_int(row[1])
        u_pair = user_map.get(pq_userid)
        if not u_pair:
            continue
        u_id, up_id = u_pair
        sub_at = safe_datetime(row[20], NOW)
        sub_id = uuid.uuid4()
        subs_batch.append(
            IntakeSubmission(
                id=sub_id,
                intake_form=parq_form,
                user_profile_id=up_id,
                submitted_by_user_id=u_id,
                submitted_at=sub_at,
                created_at=sub_at,
            )
        )
        ans_batch.extend([
            IntakeAnswer(submission_id=sub_id, question=q_map["height_cm"], numeric_value=safe_dec(row[7], "0.00"), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["weight_kg"], numeric_value=safe_dec(row[23], "0.00"), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["has_medical_conditions"], boolean_value=bool(safe_int(row[8], 0)), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["medical_conditions"], text_value=(row[9] or ""), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["on_medication"], boolean_value=bool(safe_int(row[10], 0)), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["recent_surgeries"], boolean_value=bool(safe_int(row[11], 0)), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["primary_goal"], text_value=(row[15] or ""), created_at=sub_at),
            IntakeAnswer(submission_id=sub_id, question=q_map["fitness_level"], text_value=(row[17] or ""), created_at=sub_at),
        ])

    IntakeSubmission.objects.using(alias).bulk_create(subs_batch, batch_size=2000)
    IntakeAnswer.objects.using(alias).bulk_create(ans_batch, batch_size=4000)
    print(f"[Step 4b] Imported {len(subs_batch)} PAR-Q submissions ({len(ans_batch)} answers).")

    # ------------------------------------------------------------------------
    # STEP 5: CRM LEADS & FOLLOW-UPS (`leads` & `lcomments`)
    # ------------------------------------------------------------------------
    print("\n[Step 5] Importing CRM Leads (`leads` - 65,684 rows) & Notes/Follow-ups (`lcomments`)...")
    t_crm = time.time()
    lead_source_map = {}

    def get_lead_source(raw_src: str) -> LeadSource:
        cleaned = (raw_src or "Website").strip()[:100]
        if not cleaned:
            cleaned = "Website"
        key = cleaned.lower()
        if key in lead_source_map:
            return lead_source_map[key]
        code = re.sub(r"[^A-Z0-9]+", "_", cleaned.upper()).strip("_")[:60] or "OTHER"
        # Ensure unique code
        base_code = code
        counter = 1
        existing_codes = {ls.code for ls in lead_source_map.values()}
        while code in existing_codes:
            counter += 1
            code = f"{base_code}_{counter}"
        if "insta" in key or "fb" in key or "facebook" in key or "meta" in key:
            stype = "META"
        elif "walk" in key:
            stype = "WALK_IN"
        elif "whatsapp" in key or "wa" in key:
            stype = "WHATSAPP"
        elif "google" in key:
            stype = "GOOGLE"
        elif "refer" in key:
            stype = "REFERRAL"
        elif "app" in key:
            stype = "MOBILE_APP"
        elif "call" in key or "phone" in key:
            stype = "PHONE"
        else:
            stype = "WEBSITE"
        ls = LeadSource.objects.using(alias).create(
            organization=org,
            code=code,
            name=cleaned[:140],
            source_type=stype,
            status="ACTIVE",
        )
        lead_source_map[key] = ls
        return ls

    status_map = {
        "new deal": "NEW_LEAD",
        "new": "NEW_LEAD",
        "trial booked": "TRIAL_BOOKED",
        "trial confirmed": "TRIAL_CONFIRMED",
        "trial done": "TRIAL_ATTENDED",
        "trial attended": "TRIAL_ATTENDED",
        "no show": "NO_SHOW",
        "follow up": "FOLLOW_UP_PENDING",
        "followup": "FOLLOW_UP_PENDING",
        "conversation": "INTERESTED",
        "interested": "INTERESTED",
        "hot lead": "HOT_LEAD",
        "payment pending": "PAYMENT_PENDING",
        "converted": "CONVERTED",
        "deal won": "CONVERTED",
        "deal done": "CONVERTED",
        "paid": "CONVERTED",
        "not interested": "NOT_INTERESTED",
        "deal failed": "LOST",
        "junk": "LOST",
        "lost": "LOST",
    }

    lead_map = {}  # legacy lead uid -> (lead_id, assigned_user_id)
    leads_batch = []
    total_leads = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "leads"):
        # 0:uid, 1:userid, 2:name, 3:email, 4:number, 5:planname, 6:leadfrom,
        # 7:referby, 8:assignedto, 9:commission, 10:amount, 11:status, 12:block,
        # 13:blockedon, 14:updatedon, 15:createdon, 16:verifiedon
        l_uid = safe_int(row[0])
        l_userid = safe_int(row[1])
        fname, lname = split_name(row[2], default_first="Lead")
        l_email = (row[3] or "").strip().lower()[:250] or None
        l_phone = clean_phone(row[4]) or None
        raw_plan = (row[5] or "").strip().lower()
        raw_from = (row[6] or "Website").strip()
        l_assigned_uid = safe_int(row[8], 0)
        raw_status = (row[11] or "New deal").strip().lower()
        l_created = safe_datetime(row[15], NOW)
        l_updated = safe_datetime(row[14], l_created)

        ls = get_lead_source(raw_from)
        prog = plan_name_lookup.get(raw_plan)
        assigned_tu = agent_user_map.get(l_assigned_uid)
        br = (
            plan_branch_map.get(safe_int(raw_plan))
            or agent_branch_map.get(l_assigned_uid)
            or default_branch
        )
        c_status = status_map.get(raw_status, "FOLLOW_UP_PENDING")
        u_pair = user_map.get(l_userid)
        conv_uprof_id = u_pair[1] if (u_pair and c_status == "CONVERTED") else None

        l_id = uuid.uuid4()
        assigned_uid_uuid = assigned_tu.id if assigned_tu else admin_user.id
        lead_map[l_uid] = (l_id, assigned_uid_uuid)

        leads_batch.append(
            Lead(
                id=l_id,
                organization=org,
                branch=br,
                lead_source=ls,
                first_name=fname,
                last_name=lname,
                phone_normalized=l_phone,
                email_normalized=l_email,
                country="India",
                interested_program=prog,
                current_status=c_status,
                assigned_sales_user=assigned_tu,
                first_touch_source=ls.name[:95],
                latest_touch_source=ls.name[:95],
                converted_user_profile_id=conv_uprof_id,
                response_sla_status="MET",
                created_at=l_created,
                updated_at=l_updated,
            )
        )
        total_leads += 1

        if len(leads_batch) >= 4000:
            Lead.objects.using(alias).bulk_create(leads_batch, batch_size=4000)
            leads_batch.clear()

    if leads_batch:
        Lead.objects.using(alias).bulk_create(leads_batch, batch_size=4000)
        leads_batch.clear()

    print(f"[Step 5a] Imported {total_leads} leads in {time.time() - t_crm:.1f}s.")

    # Import `lcomments` -> LeadNote & SalesFollowupTask
    t_notes = time.time()
    notes_batch = []
    tasks_batch = []
    total_notes = 0
    total_tasks = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "lcomments"):
        # 0:uid, 1:leadid, 2:status, 3:comment, 4:calldate, 5:followup,
        # 6:followuptime, 7:response, 8:subject, 9:priority, 10:agentid, 11:createdon
        lc_uid = safe_int(row[0])
        lc_leadid = safe_int(row[1])
        l_pair = lead_map.get(lc_leadid)
        if not l_pair:
            continue
        lead_uuid, default_agent_uuid = l_pair
        comment_text = (row[3] or "").strip()
        if not comment_text:
            continue

        lc_agent_uid = safe_int(row[10], 0)
        author_tu = agent_user_map.get(lc_agent_uid)
        author_uuid = author_tu.id if author_tu else default_agent_uuid
        lc_created = safe_datetime(row[11], NOW)

        notes_batch.append(
            LeadNote(
                id=uuid.uuid4(),
                lead_id=lead_uuid,
                note_text=comment_text,
                note_type=(row[2] or "GENERAL").strip().upper()[:45] or "GENERAL",
                created_by_user_id=author_uuid,
                created_at=lc_created,
                updated_at=lc_created,
            )
        )
        total_notes += 1

        f_date = safe_date(row[5], None)
        if f_date and f_date.year >= 2024:
            f_time = safe_time(row[6], dt_time(11, 0))
            due_dt = timezone.make_aware(datetime.combine(f_date, f_time), IST)
            tasks_batch.append(
                SalesFollowupTask(
                    id=uuid.uuid4(),
                    lead_id=lead_uuid,
                    assigned_to_user_id=author_uuid,
                    task_type="CALL",
                    priority="HIGH" if "hot" in str(row[9] or "").lower() else "NORMAL",
                    due_at=due_dt,
                    status="PENDING" if f_date >= TODAY else "COMPLETED",
                    outcome=comment_text[:500],
                    external_reference=f"lcomment:{lc_uid}",
                    created_by_user_id=author_uuid,
                    created_at=lc_created,
                    updated_at=lc_created,
                )
            )
            total_tasks += 1

        if len(notes_batch) >= 5000:
            LeadNote.objects.using(alias).bulk_create(notes_batch, batch_size=5000)
            notes_batch.clear()
        if len(tasks_batch) >= 4000:
            SalesFollowupTask.objects.using(alias).bulk_create(
                tasks_batch, batch_size=4000, ignore_conflicts=True
            )
            tasks_batch.clear()

    if notes_batch:
        LeadNote.objects.using(alias).bulk_create(notes_batch, batch_size=5000)
        notes_batch.clear()
    if tasks_batch:
        SalesFollowupTask.objects.using(alias).bulk_create(
            tasks_batch, batch_size=4000, ignore_conflicts=True
        )
        tasks_batch.clear()

    print(
        f"[Step 5b] Imported {total_notes} lead notes & {total_tasks} follow-up tasks in {time.time() - t_notes:.1f}s."
    )

    # ------------------------------------------------------------------------
    # STEP 6: COMMERCE & MEMBERSHIPS (`transactions` & `userplans`)
    # ------------------------------------------------------------------------
    print("\n[Step 6] Importing Orders & Payment Transactions (`transactions` - 45,243 rows)...")
    t_ord = time.time()
    txn_map = {}  # legacy txn uid -> (order_id, order_item_id)
    orders_batch = []
    items_batch = []
    payments_batch = []
    total_orders = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "transactions"):
        # 0:uid, 1:userid, 2:agentid, 3:planid, 4:sessionid, 5:billitem, 6:contact,
        # 7:sumcost, 8:basecost, 9:amountpaid, 10:pendingamount, 11:couponcost,
        # 12:taxcost, 13:taxpercent, ..., 23:transactionid, 24:paymentmode,
        # 25:paymentmethod, 26:rzpay_order, ..., 30:status, 32:createdon
        t_uid = safe_int(row[0])
        t_userid = safe_int(row[1])
        t_agentid = safe_int(row[2])
        t_planid = safe_int(row[3])
        t_sessid = safe_int(row[4])

        u_pair = user_map.get(t_userid)
        uprof_id = u_pair[1] if u_pair else None
        sold_tu = agent_user_map.get(t_agentid)
        pkg, pver, pprice, _ = session_pkg_map.get(t_sessid, default_pkg_tuple)
        br = plan_branch_map.get(t_planid) or pprice.branch or default_branch

        sum_cost = max(Decimal("0.00"), safe_dec(row[7], "0.00"))
        base_cost = max(Decimal("0.00"), safe_dec(row[8], str(sum_cost)))
        disc_cost = max(Decimal("0.00"), safe_dec(row[11], "0.00"))
        tax_cost = max(Decimal("0.00"), safe_dec(row[12], "0.00"))
        tax_pct = max(Decimal("0.000"), safe_dec(row[13], "5.00"))

        raw_st = (row[30] or "").strip().lower()
        if raw_st in ("success", "paid", "completed", "captured", "1", "true"):
            o_status = "PAID"
            p_status = "SUCCESS"
        elif "cancel" in raw_st or "fail" in raw_st:
            o_status = "CANCELLED"
            p_status = "FAILED"
        else:
            o_status = "PENDING_PAYMENT"
            p_status = "PENDING"

        pmode = (row[24] or "").strip().lower()
        if "rzp" in pmode or "online" in pmode or row[26]:
            provider = "RAZORPAY"
        elif "pos" in pmode or "icici" in pmode or "card" in pmode:
            provider = "ICICI_POS"
        elif "bank" in pmode or "neft" in pmode or "upi" in pmode:
            provider = "BANK_TRANSFER"
        else:
            provider = "CASH"

        t_created = safe_datetime(row[32], NOW)
        ord_id = uuid.uuid4()
        item_id = uuid.uuid4()
        txn_map[t_uid] = (ord_id, item_id)

        orders_batch.append(
            Order(
                id=ord_id,
                order_number=f"ORD-{t_uid:06d}",
                user_profile_id=uprof_id,
                branch=br,
                sold_by_user=sold_tu,
                order_type="RENEWAL" if safe_int(row[35], 0) > 0 else "NEW_MEMBERSHIP",
                status=o_status,
                subtotal=base_cost,
                discount_amount=disc_cost,
                tax_amount=tax_cost,
                total_amount=sum_cost,
                currency="INR",
                source="MIGRATION",
                notes=(row[5] or None),
                created_at=t_created,
                updated_at=t_created,
            )
        )
        items_batch.append(
            OrderItem(
                id=item_id,
                order_id=ord_id,
                item_type="PACKAGE",
                package=pkg,
                package_version=pver,
                package_price=pprice,
                item_name_snapshot=(row[5] or pver.name_snapshot)[:240],
                quantity=Decimal("1.00"),
                unit_price_snapshot=base_cost,
                tax_percent_snapshot=tax_pct,
                discount_amount=disc_cost,
                tax_amount=tax_cost,
                total_amount=sum_cost,
                created_at=t_created,
                updated_at=t_created,
            )
        )
        payments_batch.append(
            PaymentTransaction(
                id=uuid.uuid4(),
                order_id=ord_id,
                user_profile_id=uprof_id,
                provider=provider,
                payment_method=(row[25] or pmode or "online")[:45],
                provider_transaction_id=(row[29] or row[26] or row[23] or f"TXN-{t_uid}")[:245],
                amount=sum_cost,
                currency="INR",
                status=p_status,
                paid_at=t_created if p_status == "SUCCESS" else None,
                created_at=t_created,
                updated_at=t_created,
            )
        )
        total_orders += 1

        if len(orders_batch) >= 3500:
            Order.objects.using(alias).bulk_create(orders_batch, batch_size=3500)
            OrderItem.objects.using(alias).bulk_create(items_batch, batch_size=3500)
            PaymentTransaction.objects.using(alias).bulk_create(payments_batch, batch_size=3500)
            orders_batch.clear()
            items_batch.clear()
            payments_batch.clear()

    if orders_batch:
        Order.objects.using(alias).bulk_create(orders_batch, batch_size=3500)
        OrderItem.objects.using(alias).bulk_create(items_batch, batch_size=3500)
        PaymentTransaction.objects.using(alias).bulk_create(payments_batch, batch_size=3500)
        orders_batch.clear()
        items_batch.clear()
        payments_batch.clear()

    print(f"[Step 6a] Imported {total_orders} orders & payment transactions in {time.time() - t_ord:.1f}s.")

    # Import `userplans` -> Membership + MembershipContractSnapshot + MembershipEntitlement
    print("[Step 6b] Importing Memberships & Entitlements (`userplans` - 20,223 rows)...")
    t_memship = time.time()
    userplan_map = {}  # legacy userplan uid -> (membership_id, entitlement_id, branch_id)
    memberships_batch = []
    snapshots_batch = []
    entitlements_batch = []
    total_memberships = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "userplans"):
        # 0:uid, 1:userid, 2:planid, 3:sessionid, 4:planname, 5:max_sessions,
        # 6:branch_id, 7:sessiontime, 8:costpaid, 9:transactionid, 10:istrial,
        # 11:attendance, 12:sesspassed, 13:startdate, 14:enddate, 15:createdon
        up_uid = safe_int(row[0])
        up_userid = safe_int(row[1])
        u_pair = user_map.get(up_userid)
        if not u_pair:
            continue
        _, uprof_id = u_pair

        up_planid = safe_int(row[2])
        up_sessid = safe_int(row[3])
        up_max_sess = max(1, safe_int(row[5], 12))
        up_branch_id = safe_int(row[6])
        up_days = max(1, safe_int(row[7], 30))
        up_cost = max(Decimal("0.00"), safe_dec(row[8], "0.00"))
        up_txnid = safe_int(row[9])
        up_sesspassed = max(0, safe_int(row[12], 0))
        up_created = safe_datetime(row[15], NOW)
        s_date = safe_date(row[13], up_created.date())
        e_date = safe_date(row[14], s_date + timedelta(days=up_days))
        if e_date < s_date:
            e_date = s_date + timedelta(days=up_days)

        pkg, pver, pprice, ent_def = session_pkg_map.get(up_sessid, default_pkg_tuple)
        prog = plan_map.get(up_planid) or pkg.program
        br = branch_map.get(up_branch_id) or plan_branch_map.get(up_planid) or default_branch
        ord_pair = txn_map.get(up_txnid)
        ord_id = ord_pair[0] if ord_pair else None
        ord_item_id = ord_pair[1] if ord_pair else None

        m_status = "ACTIVE" if e_date >= TODAY else "EXPIRED"
        m_id = uuid.uuid4()
        ent_id = uuid.uuid4()
        userplan_map[up_uid] = (m_id, ent_id, br.id)

        memberships_batch.append(
            Membership(
                id=m_id,
                user_profile_id=uprof_id,
                program=prog,
                package=pkg,
                package_version=pver,
                package_price=pprice,
                source_order_id=ord_id,
                source_order_item_id=ord_item_id,
                purchase_branch=br,
                home_branch=br,
                membership_number=f"MEMSHIP-{up_uid:06d}",
                start_date=s_date,
                end_date=e_date,
                status=m_status,
                activated_at=up_created,
                legacy_reference=f"userplan:{up_uid}",
            )
        )
        snapshots_batch.append(
            MembershipContractSnapshot(
                id=uuid.uuid4(),
                membership_id=m_id,
                package=pkg,
                package_version=pver,
                package_price=pprice,
                package_name_snapshot=(row[4] or pver.name_snapshot)[:240],
                purchase_price=up_cost,
                final_amount=up_cost,
                currency="INR",
                duration_value=up_days,
                duration_unit="DAY",
                start_date=s_date,
                end_date=e_date,
                purchase_branch=br,
                source_order_id=ord_id,
                source_order_item_id=ord_item_id,
            )
        )
        ent_status = "ACTIVE" if (m_status == "ACTIVE" and up_sesspassed < up_max_sess) else (
            "EXHAUSTED" if up_sesspassed >= up_max_sess else "EXPIRED"
        )
        entitlements_batch.append(
            MembershipEntitlement(
                id=ent_id,
                membership_id=m_id,
                source_definition=ent_def,
                entitlement_type="CLASS_SESSION",
                allocated_units=Decimal(str(up_max_sess)),
                consumed_units=Decimal(str(min(up_max_sess, up_sesspassed))),
                is_unlimited=False,
                valid_from=timezone.make_aware(datetime.combine(s_date, dt_time(0, 0)), IST),
                valid_until=timezone.make_aware(datetime.combine(e_date, dt_time(23, 59)), IST),
                status=ent_status,
            )
        )
        total_memberships += 1

        if len(memberships_batch) >= 3500:
            Membership.objects.using(alias).bulk_create(memberships_batch, batch_size=3500)
            MembershipContractSnapshot.objects.using(alias).bulk_create(snapshots_batch, batch_size=3500)
            MembershipEntitlement.objects.using(alias).bulk_create(entitlements_batch, batch_size=3500)
            memberships_batch.clear()
            snapshots_batch.clear()
            entitlements_batch.clear()

    if memberships_batch:
        Membership.objects.using(alias).bulk_create(memberships_batch, batch_size=3500)
        MembershipContractSnapshot.objects.using(alias).bulk_create(snapshots_batch, batch_size=3500)
        MembershipEntitlement.objects.using(alias).bulk_create(entitlements_batch, batch_size=3500)
        memberships_batch.clear()
        snapshots_batch.clear()
        entitlements_batch.clear()

    print(f"[Step 6b] Imported {total_memberships} memberships & entitlements in {time.time() - t_memship:.1f}s.")

    # ------------------------------------------------------------------------
    # STEP 7: CLASSES, SLOTS & BOOKINGS (`class`, `slots`, `bookings`)
    # ------------------------------------------------------------------------
    print("\n[Step 7] Importing Class Templates, Schedule Rules (`class`), Occurrences (`slots`) & Bookings...")
    t_cls = time.time()

    cls_cat_pilates = ClassCategory.objects.using(alias).create(
        organization=org, code="PILATES", name="Sweat Pilates", display_order=1, status="ACTIVE"
    )
    cls_cat_bootcamp = ClassCategory.objects.using(alias).create(
        organization=org, code="BOOTCAMP", name="Sweat Bootcamp", display_order=2, status="ACTIVE"
    )
    cls_cat_general = ClassCategory.objects.using(alias).create(
        organization=org, code="STRENGTH_MOBILITY", name="Strength & Mobility", display_order=3, status="ACTIVE"
    )

    template_by_name = {}  # normalized class_name -> ClassTemplate

    def get_or_create_class_template(raw_name: str, plan_uid: int = 13, branch_uid: int = 3) -> ClassTemplate:
        cname = (raw_name or "Sweat Total").strip()
        if not cname:
            cname = "Sweat Total"
        raw_lower = cname.lower()
        is_bootcamp = (plan_uid == 12 or branch_uid == 1 or "bootcamp" in raw_lower)
        ccat = cls_cat_bootcamp if is_bootcamp else cls_cat_pilates

        if raw_lower in ("sweat total", "sweat total 2"):
            cname = "Sweat Total (Bootcamp)" if is_bootcamp else "Sweat Total (Pilates)"
        elif raw_lower in ("sweat stretch", "sweat stretcb", "sweat strecth", "sweat | stretch", "total stretch"):
            cname = "Sweat Stretch"
        elif raw_lower == "bootcamp":
            cname = "Bootcamp"
        elif raw_lower == "bootcamp total":
            cname = "Bootcamp Total"

        key = (cname.lower(), ccat.code)
        if key in template_by_name:
            return template_by_name[key]
        code = re.sub(r"[^A-Z0-9]+", "_", cname.upper()).strip("_")[:70] or "CLASS"
        base_code = code
        cnt = 1
        existing_codes = {t.code for t in template_by_name.values()}
        while code in existing_codes:
            cnt += 1
            code = f"{base_code}_{cnt}"

        prog = plan_map.get(plan_uid) or (plan_map.get(12) if is_bootcamp else plan_map.get(13)) or default_pkg_tuple[0].program
        tpl = ClassTemplate.objects.using(alias).create(
            organization=org,
            category=ccat,
            program=prog,
            code=code,
            name=cname[:190],
            description=f"{cname} — {ccat.name} session",
            default_duration_minutes=50,
            default_capacity=20 if is_bootcamp else 12,
            default_trial_capacity=2,
            default_waitlist_capacity=3,
            default_delivery_mode="OFFLINE",
            allow_booking=True,
            allow_trial=True,
            allow_waitlist=True,
            allow_reschedule=True,
            status="ACTIVE",
        )
        template_by_name[key] = tpl
        for b in branch_map.values():
            ClassBranchAvailability.objects.using(alias).get_or_create(
                class_template=tpl,
                branch=b,
                defaults={"status": "ENABLED"},
            )
        return tpl

    day_token_map = {
        "mon": 1, "monday": 1,
        "tue": 2, "tuesday": 2,
        "wed": 3, "wednesday": 3,
        "thu": 4, "thursday": 4,
        "fri": 5, "friday": 5,
        "sat": 6, "saturday": 6,
        "sun": 7, "sunday": 7,
    }

    class_rule_map = {}  # legacy class uid -> (rule_id, branch_id, tpl_id)
    rules_batch = []

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "class"):
        # 0:uid, 1:branch_id, 2:plan_id, 3:agentid, 4:start_time, 5:end_time,
        # 6:start_date, 7:end_date, 8:book_capacity, 9:wait_capacity, 10:day,
        # 11:createdon, ..., 14:block, 15:bookable_upto, 16:class_name, 17:max_trial_booking_allowed
        c_uid = safe_int(row[0])
        c_branch_id = safe_int(row[1])
        c_plan_id = safe_int(row[2])
        s_time = safe_time(row[4], dt_time(7, 0))
        e_time = safe_time(row[5], dt_time(7, 50))
        if s_time >= dt_time(23, 0):
            s_time = dt_time(22, 0)
            e_time = dt_time(22, 50)
        elif e_time <= s_time:
            dt_end = datetime.combine(TODAY, s_time) + timedelta(minutes=50)
            e_time = dt_end.time() if dt_end.date() == TODAY else dt_time(23, 59)

        s_date = safe_date(row[6], date(2024, 1, 1))
        e_date = safe_date(row[7], None)
        if e_date and e_date < s_date:
            e_date = s_date + timedelta(days=7)

        b_cap = max(1, safe_int(row[8], 10))
        w_cap = max(0, safe_int(row[9], 2))
        t_cap = max(0, safe_int(row[17], 2))
        c_block = safe_int(row[14], 0)
        cname = (row[16] or "Sweat Total").strip()

        dows = []
        for tok in str(row[10] or "").lower().replace(" ", "").split(","):
            if tok in day_token_map and day_token_map[tok] not in dows:
                dows.append(day_token_map[tok])
        if not dows:
            dows = [1, 2, 3, 4, 5, 6]

        br = branch_map.get(c_branch_id) or plan_branch_map.get(c_plan_id) or default_branch
        tpl = get_or_create_class_template(cname, c_plan_id, c_branch_id)
        r_id = uuid.uuid4()
        class_rule_map[c_uid] = (r_id, br.id, tpl.id)

        rules_batch.append(
            ClassScheduleRule(
                id=r_id,
                class_template=tpl,
                branch=br,
                recurrence_type="WEEKLY",
                days_of_week=dows,
                start_time=s_time,
                end_time=e_time,
                valid_from=s_date,
                valid_until=e_date,
                delivery_mode="ONLINE" if br.location.area == "Online" else "OFFLINE",
                capacity_override=b_cap,
                trial_capacity_override=t_cap,
                waitlist_capacity_override=w_cap,
                status="ACTIVE" if c_block == 0 else "INACTIVE",
            )
        )

    ClassScheduleRule.objects.using(alias).bulk_create(rules_batch, batch_size=2000)
    print(f"[Step 7a] Imported {len(template_by_name)} class templates & {len(rules_batch)} schedule rules.")

    # Import `slots` -> ClassOccurrence & ClassOccurrenceTrainer (98,247 rows)
    t_slots = time.time()
    slot_map = {}  # legacy slot uid -> (occurrence_id, branch_id, start_at)
    occ_batch = []
    occ_trainer_batch = []
    total_slots = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "slots"):
        # 0:uid, 1:class_id, 2:agentid, 3:day, 4:start_time, 5:end_time,
        # 6:bookable, 7:booked, 8:waiting, 9:max_booking, 10:max_waiting,
        # 11:createdon, ..., 14:block, 15:date, 16:class_name, ..., 21:max_trial_booking_allowed
        sl_uid = safe_int(row[0])
        sl_class_id = safe_int(row[1])
        sl_agent_id = safe_int(row[2])
        s_time = safe_time(row[4], dt_time(7, 0))
        e_time = safe_time(row[5], dt_time(7, 50))
        max_b = max(1, safe_int(row[9], 10))
        max_w = max(0, safe_int(row[10], 2))
        max_t = max(0, safe_int(row[21], 2))
        sl_block = safe_int(row[14], 0)
        occ_date = safe_date(row[15], None)
        if not occ_date:
            sl_created = safe_datetime(row[11], NOW)
            occ_date = sl_created.date()

        start_dt = timezone.make_aware(datetime.combine(occ_date, s_time), IST)
        end_dt = timezone.make_aware(datetime.combine(occ_date, e_time), IST)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=50)

        rule_info = class_rule_map.get(sl_class_id)
        if rule_info:
            rule_id, br_id, tpl_id = rule_info
        else:
            rule_id = None
            br_id = (agent_branch_map.get(sl_agent_id) or default_branch).id
            tpl_id = get_or_create_class_template(row[16] or "Sweat Total").id

        if sl_block != 0:
            occ_status = "CANCELLED"
        elif occ_date < TODAY:
            occ_status = "COMPLETED"
        else:
            occ_status = "OPEN"

        occ_id = uuid.uuid4()
        slot_map[sl_uid] = (occ_id, br_id, start_dt)

        occ_batch.append(
            ClassOccurrence(
                id=occ_id,
                class_template_id=tpl_id,
                schedule_rule_id=rule_id,
                branch_id=br_id,
                occurrence_date=occ_date,
                start_at=start_dt,
                end_at=end_dt,
                delivery_mode="OFFLINE",
                capacity=max_b,
                trial_capacity=max_t,
                waitlist_capacity=max_w,
                status=occ_status,
                created_at=start_dt,
                updated_at=start_dt,
            )
        )

        tp = agent_trainer_map.get(sl_agent_id)
        if tp:
            occ_trainer_batch.append(
                ClassOccurrenceTrainer(
                    id=uuid.uuid4(),
                    occurrence_id=occ_id,
                    trainer_profile_id=tp.id,
                    trainer_role="LEAD",
                    status="CONFIRMED",
                    assigned_at=start_dt,
                    created_at=start_dt,
                )
            )

        total_slots += 1
        if len(occ_batch) >= 4000:
            ClassOccurrence.objects.using(alias).bulk_create(occ_batch, batch_size=4000)
            ClassOccurrenceTrainer.objects.using(alias).bulk_create(occ_trainer_batch, batch_size=4000)
            occ_batch.clear()
            occ_trainer_batch.clear()

    if occ_batch:
        ClassOccurrence.objects.using(alias).bulk_create(occ_batch, batch_size=4000)
        ClassOccurrenceTrainer.objects.using(alias).bulk_create(occ_trainer_batch, batch_size=4000)
        occ_batch.clear()
        occ_trainer_batch.clear()

    print(f"[Step 7b] Imported {total_slots} class occurrences & trainer assignments in {time.time() - t_slots:.1f}s.")

    # Import `bookings` -> Booking & AttendanceRecord (178,297 rows)
    t_bk = time.time()
    bookings_batch = []
    attendance_batch = []
    total_bookings = 0
    total_attendance = 0

    for row in iter_table_rows(SQL_FILE_PATH, offsets, "bookings"):
        # 0:uid, 1:book_id, 2:user_id, 3:userplan_id, 4:slot_id, 5:createdon,
        # 6:booking_status, 7:booked_by, 8:updatedon, 9:book_source, 10:attended,
        # 11:attendance_tIme, 12:waiting_time, 13:cancellation_tIme, 14:booking_time
        b_uid = safe_int(row[0])
        b_userid = safe_int(row[2])
        b_upid = safe_int(row[3])
        b_slotid = safe_int(row[4])

        u_pair = user_map.get(b_userid)
        sl_info = slot_map.get(b_slotid)
        if not u_pair or not sl_info:
            continue

        _, uprof_id = u_pair
        occ_id, br_id, sl_start_dt = sl_info
        up_info = userplan_map.get(b_upid)
        mem_id = up_info[0] if up_info else None
        ent_id = up_info[1] if up_info else None

        raw_bst = (row[6] or "booked").strip().lower()
        attended = safe_int(row[10], 0)
        b_created = safe_datetime(row[5], sl_start_dt)
        att_dt = safe_datetime(row[11], sl_start_dt)
        canc_dt = safe_datetime(row[13], None)

        if "cancel" in raw_bst:
            b_status = "CANCELLED"
        elif "wait" in raw_bst:
            b_status = "WAITLISTED"
        elif attended == 1:
            b_status = "COMPLETED"
        elif sl_start_dt.date() < TODAY:
            b_status = "NO_SHOW"
        else:
            b_status = "CONFIRMED"

        raw_bsrc = (row[9] or "").strip().lower()
        if "app" in raw_bsrc:
            b_source = "MOBILE_APP"
        elif "admin" in raw_bsrc:
            b_source = "ADMIN"
        else:
            b_source = "WEB"

        bk_uuid = uuid.uuid4()
        bookings_batch.append(
            Booking(
                id=bk_uuid,
                booking_number=f"BK-{b_uid:07d}",
                user_profile_id=uprof_id,
                membership_id=mem_id,
                entitlement_id=ent_id,
                occurrence_id=occ_id,
                branch_id=br_id,
                booking_type="MEMBER" if mem_id else "TRIAL",
                booking_source=b_source,
                status=b_status,
                booked_at=b_created,
                cancelled_at=canc_dt if b_status == "CANCELLED" else None,
                completed_at=att_dt if attended == 1 else None,
                created_at=b_created,
                updated_at=b_created,
            )
        )
        total_bookings += 1

        if attended == 1:
            attendance_batch.append(
                AttendanceRecord(
                    id=uuid.uuid4(),
                    booking_id=bk_uuid,
                    user_profile_id=uprof_id,
                    branch_id=br_id,
                    occurrence_id=occ_id,
                    status="PRESENT",
                    check_in_status="SUCCESSFUL",
                    check_in_method="MOBILE" if b_source == "MOBILE_APP" else "ADMIN",
                    check_in_at=att_dt,
                    attendance_completed=True,
                    completion_verified_at=att_dt,
                    is_within_geofence=True,
                    created_at=att_dt,
                    updated_at=att_dt,
                )
            )
            total_attendance += 1

        if len(bookings_batch) >= 5000:
            Booking.objects.using(alias).bulk_create(bookings_batch, batch_size=5000)
            bookings_batch.clear()
            if attendance_batch:
                AttendanceRecord.objects.using(alias).bulk_create(attendance_batch, batch_size=5000)
                attendance_batch.clear()

    if bookings_batch:
        Booking.objects.using(alias).bulk_create(bookings_batch, batch_size=5000)
        bookings_batch.clear()
    if attendance_batch:
        AttendanceRecord.objects.using(alias).bulk_create(attendance_batch, batch_size=5000)
        attendance_batch.clear()

    print(
        f"[Step 7c] Imported {total_bookings} bookings & {total_attendance} attendance records in {time.time() - t_bk:.1f}s."
    )

    # ------------------------------------------------------------------------
    # STEP 8: RESTORE RBAC PERMISSION SETS & MODULE ACCESS
    # ------------------------------------------------------------------------
    print("\n[Step 8] Restoring RBAC permission sets & module access...")
    from apps.master.models_saas import ProductModule, ProductSubmodule, TenantModule
    from apps.master.provisioning import sync_tenant_catalog_and_rbac
    from apps.tenant_core.rbac_defaults import sync_default_role_permissions
    from apps.tenant_core.models_rbac import (
        SubmoduleCatalog, Permission, RolePermissionSet,
        RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess,
    )

    for pm in ProductModule.objects.using("default").filter(is_active=True):
        sub_codes = [
            f"/{pm.code.lower()}/{psm.code.lower()}"
            for psm in ProductSubmodule.objects.using("default").filter(module=pm, is_active=True)
        ]
        TenantModule.objects.using("default").update_or_create(
            tenant=tenant,
            module=pm,
            defaults={
                "is_enabled": True,
                "availability_mode": "ALL_BRANCHES",
                "configuration": {"enabled_submodules": sub_codes},
            },
        )

    sync_tenant_catalog_and_rbac(alias, org=org)
    sync_default_role_permissions(alias, org=org, overwrite_custom=True)

    org_admin_role = Role.objects.using(alias).get(code="ORG_ADMIN")
    perm_set = RolePermissionSet.objects.using(alias).filter(role=org_admin_role, is_active=True).first()
    for mod in ModuleCatalog.objects.using(alias).all():
        RoleModuleAccess.objects.using(alias).update_or_create(
            role=org_admin_role,
            module=mod,
            defaults={"permission_set": perm_set, "can_access": True, "is_visible": True},
        )
    for sm in SubmoduleCatalog.objects.using(alias).all():
        RoleSubmoduleAccess.objects.using(alias).update_or_create(
            role=org_admin_role,
            submodule=sm,
            defaults={"permission_set": perm_set, "can_access": True, "is_visible": True},
        )
    for p in Permission.objects.using(alias).all():
        RolePermissionSetItem.objects.using(alias).update_or_create(
            permission_set=perm_set,
            permission=p,
            defaults={"granted": True, "is_allowed": True},
        )

    print("\n" + "=" * 72)
    print(f"MIGRATION COMPLETED SUCCESSFULLY IN {time.time() - t_start:.1f}s!")
    print("=" * 72)


if __name__ == "__main__":
    run_import()
