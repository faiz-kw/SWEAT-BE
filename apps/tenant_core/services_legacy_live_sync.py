import os
import re
import uuid
import logging
from datetime import datetime, date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_memberships import Membership, MembershipEntitlement
from .models_org import Branch, Organization
from .models_catalog import Package, PackageVersion, PackagePrice, Program
from .models_crm import Lead

logger = logging.getLogger(__name__)

SWEAT_NS = uuid.UUID('e6a23bd5-cdf7-4fd9-a921-9bc848db4052')

def gen_uuid(prefix: str, val) -> str:
    return str(uuid.uuid5(SWEAT_NS, f"{prefix}:{val}"))

def get_legacy_db_connection():
    """
    Establishes connection to the legacy MySQL database.
    Supports PyMySQL, mysql.connector, or MySQLdb.
    """
    host = os.getenv('LEGACY_DB_HOST', '3.108.108.46')
    port = int(os.getenv('LEGACY_DB_PORT', '3306'))
    db = os.getenv('LEGACY_DB_NAME', 'bpcadmin')
    user = os.getenv('LEGACY_DB_USER', 'root')
    password = os.getenv('LEGACY_DB_PASSWORD', '')

    driver = None
    try:
        import pymysql
        driver = pymysql
    except ImportError:
        try:
            import mysql.connector as mysql_connector
            driver = mysql_connector
        except ImportError:
            try:
                import MySQLdb
                driver = MySQLdb
            except ImportError:
                pass

    if not driver:
        raise RuntimeError("No MySQL client driver found. Please run 'pip install pymysql' on the server.")

    conn = driver.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        charset='utf8mb4',
        connect_timeout=10,
    )
    return conn

def search_legacy_members(query: str) -> list[dict]:
    """
    Searches the live legacy database for members matching phone, email, or name.
    Consolidates multiple accounts/leads by 10-digit phone number.
    """
    query = (query or '').strip()
    if not query:
        return []

    clean_digits = re.sub(r'[^\d]', '', query)
    phone_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits

    conn = get_legacy_db_connection()
    results = []

    try:
        with conn.cursor() as cursor:
            # 1. Search Users
            if len(phone_10) >= 10:
                sql = "SELECT uid, firstname, lastname, email, contact, createdon FROM users WHERE contact LIKE %s"
                cursor.execute(sql, (f"%{phone_10}%",))
            elif '@' in query:
                sql = "SELECT uid, firstname, lastname, email, contact, createdon FROM users WHERE LOWER(email) = LOWER(%s)"
                cursor.execute(sql, (query,))
            else:
                sql = "SELECT uid, firstname, lastname, email, contact, createdon FROM users WHERE firstname LIKE %s OR lastname LIKE %s LIMIT 20"
                cursor.execute(sql, (f"%{query}%", f"%{query}%"))

            user_rows = cursor.fetchall()
            if not user_rows:
                # Also check leads table if not registered in users yet
                if len(phone_10) >= 10:
                    cursor.execute("SELECT id, name, email, phone, created_at FROM leads WHERE phone LIKE %s LIMIT 10", (f"%{phone_10}%",))
                    for l_row in cursor.fetchall():
                        results.append({
                            'source': 'lead_only',
                            'legacy_lead_id': l_row[0],
                            'name': l_row[1],
                            'email': l_row[2],
                            'phone': l_row[3],
                            'created_at': str(l_row[4]),
                            'has_plans': False,
                        })
                return results

            # Map users
            user_ids = [r[0] for r in user_rows]
            users_dict = {}
            for r in user_rows:
                u_phone = re.sub(r'[^\d]', '', str(r[4] or ''))[-10:]
                users_dict[r[0]] = {
                    'uid': r[0],
                    'first_name': str(r[1] or '').strip(),
                    'last_name': str(r[2] or '').strip(),
                    'email': str(r[3] or '').strip().lower(),
                    'phone': str(r[4] or '').strip(),
                    'phone_10': u_phone,
                    'created_on': str(r[5] or ''),
                    'plans': [],
                }

            # 2. Fetch Userplans for all found user IDs
            format_strings = ','.join(['%s'] * len(user_ids))
            cursor.execute(f"""
                SELECT uid, userid, planname, max_sessions, branch_id, costpaid, sesspassed,
                       startdate, enddate, passport_max_sessions, passport_sesspassed, createdon
                FROM userplans
                WHERE userid IN ({format_strings})
                ORDER BY startdate DESC
            """, tuple(user_ids))

            today_str = timezone.now().date().isoformat()

            for p_row in cursor.fetchall():
                p_uid = p_row[0]
                u_id = p_row[1]
                planname = p_row[2] or 'Sweat Pilates'
                max_s = int(p_row[3] or 0)
                branch_id = int(p_row[4] or 1)
                cost = float(p_row[5] or 0)
                sess_p = int(p_row[6] or 0)
                start_d = str(p_row[7] or '')
                end_d = str(p_row[8] or '')
                pass_max = int(p_row[9] or 0)
                pass_p = int(p_row[10] or 0)
                rem = max(0, max_s - sess_p) + max(0, pass_max - pass_p)

                is_active = (end_d >= today_str and rem > 0)

                plan_obj = {
                    'plan_id': p_uid,
                    'user_id': u_id,
                    'plan_name': planname,
                    'max_sessions': max_s,
                    'consumed_sessions': sess_p,
                    'remaining_sessions': rem,
                    'passport_max': pass_max,
                    'passport_consumed': pass_p,
                    'branch_id': branch_id,
                    'cost': cost,
                    'start_date': start_d,
                    'end_date': end_d,
                    'status': 'ACTIVE' if is_active else 'EXPIRED',
                }

                if u_id in users_dict:
                    users_dict[u_id]['plans'].append(plan_obj)

            # Consolidate by phone if multiple user accounts exist
            phone_groups = defaultdict(list)
            for u in users_dict.values():
                key = u['phone_10'] if len(u['phone_10']) == 10 else f"uid_{u['uid']}"
                phone_groups[key].append(u)

            for key, group in phone_groups.items():
                # Pick primary user (the one with active plans or latest created)
                active_user = next((u for u in group if any(p['status'] == 'ACTIVE' for p in u['plans'])), group[-1])
                all_plans = []
                for u in group:
                    all_plans.extend(u['plans'])

                primary_active_plan = next((p for p in all_plans if p['status'] == 'ACTIVE'), (all_plans[0] if all_plans else None))

                results.append({
                    'source': 'live_mysql',
                    'primary_uid': active_user['uid'],
                    'linked_uids': [u['uid'] for u in group],
                    'name': f"{active_user['first_name']} {active_user['last_name']}".strip(),
                    'first_name': active_user['first_name'],
                    'last_name': active_user['last_name'],
                    'email': active_user['email'],
                    'phone': active_user['phone'],
                    'phone_10': active_user['phone_10'],
                    'has_plans': len(all_plans) > 0,
                    'active_plan': primary_active_plan,
                    'all_plans': all_plans,
                })

    finally:
        conn.close()

    return results

def sync_live_legacy_member_to_tenant(primary_uid: int, tenant_alias: str, org_id: str = None) -> UserProfile:
    """
    Performs on-demand live extraction from MySQL and creates/updates the member in PostgreSQL.
    Deduplicates accounts and sets the active membership & exact session balances.
    """
    conn = get_legacy_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Fetch user
            cursor.execute("SELECT uid, firstname, lastname, email, contact, createdon FROM users WHERE uid = %s", (primary_uid,))
            u_row = cursor.fetchone()
            if not u_row:
                raise ValueError(f"Legacy user {primary_uid} not found in live database.")

            fn = str(u_row[1] or '').strip()
            ln = str(u_row[2] or '').strip()
            email = str(u_row[3] or '').strip().lower()
            phone = str(u_row[4] or '').strip()
            phone_10 = re.sub(r'[^\d]', '', phone)[-10:]

            # 2. Check for other user IDs with same phone
            linked_uids = [primary_uid]
            if len(phone_10) == 10:
                cursor.execute("SELECT uid FROM users WHERE contact LIKE %s", (f"%{phone_10}%",))
                linked_uids = list(set([r[0] for r in cursor.fetchall()] + [primary_uid]))

            # 3. Fetch all userplans
            format_strings = ','.join(['%s'] * len(linked_uids))
            cursor.execute(f"""
                SELECT uid, userid, planname, max_sessions, branch_id, costpaid, sesspassed,
                       startdate, enddate, passport_max_sessions, passport_sesspassed, createdon
                FROM userplans
                WHERE userid IN ({format_strings})
                ORDER BY startdate DESC
            """, tuple(linked_uids))
            plans = cursor.fetchall()

    finally:
        conn.close()

    today_str = timezone.now().date().isoformat()
    clean_email = email if ('@' in email) else f"member_{primary_uid}@sweatfit.local"
    mem_number = f"MEM-{primary_uid:06d}"

    # Default entities matching server
    BRANCH_MAP = {
        1: 'e6a23bd5-cdf7-4fd9-a921-9bc848db4052', # Bandra West
        2: 'cf447d73-a91b-4c0c-95d4-9fcf4844363c', # Juhu / Andheri West
        3: '033b5c3c-0477-4b98-a473-0dd91fdf1bfe', # Lower Parel
        5: 'e6a23bd5-cdf7-4fd9-a921-9bc848db4052', # Fallback
    }
    DEFAULT_BRANCH_ID = 'e6a23bd5-cdf7-4fd9-a921-9bc848db4052'
    DEFAULT_PACKAGE_ID = 'eff2f058-83d4-40e8-bcef-80e1dfa05e2a'
    DEFAULT_PRICE_ID = 'e27d8237-30b6-4659-bd4b-bcc6ca5f7757'
    DEFAULT_VERSION_ID = '23c385f5-ec3c-4947-a86f-280fb0577273'
    DEFAULT_PROGRAM_ID = '424724f2-21b6-4c72-95a3-73a64e6f78a4'

    with transaction.atomic(using=tenant_alias):
        # 1. TenantUser
        user = TenantUser.objects.using(tenant_alias).filter(email__iexact=clean_email).first()
        if not user and len(phone_10) == 10:
            user = TenantUser.objects.using(tenant_alias).filter(phone__contains=phone_10).first()

        if not user:
            org_uuid = uuid.UUID(org_id) if org_id else uuid.UUID('299b7ef9-00c1-4d62-937f-4554b3ba0810')
            user = TenantUser.objects.using(tenant_alias).create(
                id=uuid.UUID(gen_uuid('tenantuser', primary_uid)),
                organization_id=org_uuid,
                email=clean_email,
                phone=phone[:30],
                first_name=fn,
                last_name=ln,
                display_name=f"{fn} {ln}".strip() or clean_email,
                status='ACTIVE',
                user_type='MEMBER',
                is_login_allowed=True,
            )
        else:
            if not user.phone and phone:
                user.phone = phone[:30]
                user.save(using=tenant_alias, update_fields=['phone'])

        # 2. UserProfile
        profile = UserProfile.objects.using(tenant_alias).filter(user=user).first()
        if not profile:
            profile = UserProfile.objects.using(tenant_alias).filter(member_number=mem_number).first()

        if not profile:
            profile = UserProfile.objects.using(tenant_alias).create(
                id=uuid.UUID(gen_uuid('userprofile', primary_uid)),
                user=user,
                member_number=mem_number,
                first_name_snapshot=fn,
                last_name_snapshot=ln,
                member_type='MEMBER',
                member_status='ACTIVE',
                legacy_reference=f"user:{primary_uid}",
                preferred_branch_id=uuid.UUID(DEFAULT_BRANCH_ID),
            )
        else:
            profile.member_status = 'ACTIVE'
            profile.first_name_snapshot = profile.first_name_snapshot or fn
            profile.last_name_snapshot = profile.last_name_snapshot or ln
            profile.legacy_reference = profile.legacy_reference or f"user:{primary_uid}"
            profile.save(using=tenant_alias)

        # 3. Synchronize All Userplans
        for p in plans:
            p_uid = p[0]
            planname = p[2] or 'Sweat Pilates'
            max_s = int(p[3] or 0)
            branch_id_num = int(p[4] or 1)
            sess_p = int(p[6] or 0)
            start_d_str = str(p[7] or '2026-01-01')
            end_d_str = str(p[8] or '2026-01-01')
            pass_max = int(p[9] or 0)
            pass_p = int(p[10] or 0)
            rem = max(0, max_s - sess_p) + max(0, pass_max - pass_p)

            is_active = (end_d_str >= today_str and rem > 0)
            status_val = 'ACTIVE' if is_active else 'EXPIRED'
            m_number = f"MEMSHIP-{p_uid:06d}"
            branch_uuid = uuid.UUID(BRANCH_MAP.get(branch_id_num, DEFAULT_BRANCH_ID))

            mem, created = Membership.objects.using(tenant_alias).update_or_create(
                membership_number=m_number,
                defaults={
                    'user_profile': profile,
                    'start_date': datetime.strptime(start_d_str, '%Y-%m-%d').date() if start_d_str else timezone.now().date(),
                    'end_date': datetime.strptime(end_d_str, '%Y-%m-%d').date() if end_d_str else timezone.now().date(),
                    'status': status_val,
                    'home_branch_id': branch_uuid,
                    'purchase_branch_id': branch_uuid,
                    'package_id': uuid.UUID(DEFAULT_PACKAGE_ID),
                    'package_price_id': uuid.UUID(DEFAULT_PRICE_ID),
                    'package_version_id': uuid.UUID(DEFAULT_VERSION_ID),
                    'program_id': uuid.UUID(DEFAULT_PROGRAM_ID),
                    'legacy_reference': f"userplan:{p_uid}",
                    'parq_status': 'PENDING',
                }
            )

            # Entitlement (Base Sessions)
            ent_base_uuid = uuid.UUID(gen_uuid('entitlement_base', p_uid))
            MembershipEntitlement.objects.using(tenant_alias).update_or_create(
                id=ent_base_uuid,
                defaults={
                    'membership': mem,
                    'entitlement_type': 'CLASS_SESSION',
                    'allocated_units': max_s,
                    'consumed_units': sess_p,
                    'is_unlimited': False,
                    'valid_from': mem.start_date,
                    'valid_until': datetime.combine(mem.end_date, datetime.max.time()),
                    'status': status_val,
                }
            )

            # Entitlement (Passport Sessions)
            if pass_max > 0:
                ent_pass_uuid = uuid.UUID(gen_uuid('entitlement_pass', p_uid))
                MembershipEntitlement.objects.using(tenant_alias).update_or_create(
                    id=ent_pass_uuid,
                    defaults={
                        'membership': mem,
                        'entitlement_type': 'CROSS_BRANCH',
                        'allocated_units': pass_max,
                        'consumed_units': pass_p,
                        'is_unlimited': False,
                        'valid_from': mem.start_date,
                        'valid_until': datetime.combine(mem.end_date, datetime.max.time()),
                        'status': status_val,
                    }
                )

    return profile
