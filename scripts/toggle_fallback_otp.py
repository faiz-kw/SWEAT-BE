#!/usr/bin/env python
"""
Script: Dynamic Fallback OTP Switch for SWEAT Tenant Databases
Allows administrators to safely turn ON/OFF a temporary master verification code
to diagnose and test member app issues without leaving permanent security backdoors.

Usage:
  # Check status:
  python scripts/toggle_fallback_otp.py --status

  # Turn ON for 2 hours with default code 123456 (all numbers):
  python scripts/toggle_fallback_otp.py --enable

  # Turn ON with custom code for 4 hours restricted to a specific member's phone:
  python scripts/toggle_fallback_otp.py --enable --code 849201 --hours 4 --phone 9930599345

  # Turn OFF immediately:
  python scripts/toggle_fallback_otp.py --disable

  # Or via Django shell:
  python manage.py shell < scripts/toggle_fallback_otp.py
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

# Ensure Django environment is configured
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.utils import timezone
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_govern import OrganizationSettings
from apps.tenant_core.models_org import Organization


def resolve_tenant_aliases():
    targets = []
    try:
        tenants = Tenant.objects.using('default').all()
        for t in tenants:
            alias = build_tenant_db_alias(t.id)
            ds = TenantDataSource.objects.using('default').filter(tenant=t).first()
            if ds:
                _register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
                targets.append((alias, t.slug))
            else:
                db_name = f"tenant_{t.slug.replace('-', '_')}"
                _register_tenant_connection(alias, db_name=db_name, tenant_id=t.id)
                targets.append((alias, t.slug))
    except Exception as exc:
        print(f"Master tenant lookup note: {exc}")

    if not targets:
        targets.append(('tenant_sweat_demo', 'sweat_demo'))

    return targets


def manage_fallback_otp(action='status', code=None, hours=2, phone=None):
    targets = resolve_tenant_aliases()
    now = timezone.now()

    print("==========================================================")
    print("  SWEAT Dynamic Fallback Verification Code Manager        ")
    print("==========================================================")

    for alias, slug in targets:
        set_tenant_db_alias(alias)
        print(f"\n[Tenant: {slug} | Database: {alias}]")

        try:
            org = Organization.objects.using(alias).first()
            if not org:
                print(f"  Organization record not found in {alias}. Skipping.")
                continue

            settings_obj, _ = OrganizationSettings.objects.using(alias).get_or_create(
                organization=org,
                defaults={'currency': org.currency or 'INR'}
            )

            cfg = settings_obj.membership_config or {}
            fb_cfg = cfg.get('fallback_otp', {})

            if action == 'enable':
                custom_code = str(code or fb_cfg.get('fallback_code') or '123456').strip()
                duration = max(1, min(int(hours or 2), 72))
                exp_dt = now + timedelta(hours=duration)
                target_p = str(phone).strip() if phone else None

                fb_cfg = {
                    'is_enabled': True,
                    'fallback_code': custom_code,
                    'expires_at': exp_dt.isoformat(),
                    'target_phone': target_p,
                    'updated_at': now.isoformat(),
                    'updated_by': 'cli_admin',
                }
                cfg['fallback_otp'] = fb_cfg
                settings_obj.membership_config = cfg
                settings_obj.save(using=alias, update_fields=['membership_config', 'updated_at'])
                print(f"  >>> Fallback OTP successfully turned ON!")
                print(f"      Code:         {custom_code}")
                print(f"      Expires At:   {exp_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} ({duration} hours)")
                print(f"      Target Phone: {target_p or 'ALL PHONES (Global)'}")

            elif action == 'disable':
                fb_cfg = {
                    'is_enabled': False,
                    'fallback_code': None,
                    'expires_at': None,
                    'target_phone': None,
                    'updated_at': now.isoformat(),
                    'updated_by': 'cli_admin',
                }
                cfg['fallback_otp'] = fb_cfg
                settings_obj.membership_config = cfg
                settings_obj.save(using=alias, update_fields=['membership_config', 'updated_at'])
                print(f"  >>> Fallback OTP successfully turned OFF!")

            else:  # status
                is_active = fb_cfg.get('is_enabled', False)
                exp_str = fb_cfg.get('expires_at')
                is_expired = False
                rem_mins = 0
                if is_active and exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(str(exp_str))
                        if timezone.is_naive(exp_dt):
                            exp_dt = timezone.make_aware(exp_dt)
                        if now > exp_dt:
                            is_expired = True
                            is_active = False
                        else:
                            rem_mins = int((exp_dt - now).total_seconds() // 60)
                    except Exception:
                        pass

                status_label = "ENABLED (ACTIVE)" if (is_active and not is_expired) else ("EXPIRED (INACTIVE)" if is_expired else "DISABLED")
                print(f"  Status:       {status_label}")
                if is_active and not is_expired:
                    print(f"  Code:         {fb_cfg.get('fallback_code')}")
                    print(f"  Expires in:   {rem_mins} minutes ({exp_str})")
                    print(f"  Target Phone: {fb_cfg.get('target_phone') or 'ALL PHONES'}")
                else:
                    print("  No fallback code is currently accepted on this tenant.")

        except Exception as e:
            print(f"  Error inspecting/updating {alias}: {e}")

    print("\n==========================================================")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="SWEAT Dynamic Fallback OTP Manager")
    parser.add_argument('--status', action='store_true', help="Show current status")
    parser.add_argument('--enable', action='store_true', help="Enable fallback OTP")
    parser.add_argument('--disable', action='store_true', help="Disable fallback OTP")
    parser.add_argument('--code', type=str, default='123456', help="Fallback code (default: 123456)")
    parser.add_argument('--hours', type=int, default=2, help="Active duration in hours (default: 2)")
    parser.add_argument('--phone', type=str, default=None, help="Restrict to single phone (e.g. 9930599345)")

    # When run via 'python manage.py shell < scripts/toggle_fallback_otp.py', sys.argv might contain manage.py args
    args, unknown = parser.parse_known_args()

    action = 'status'
    if args.enable:
        action = 'enable'
    elif args.disable:
        action = 'disable'
    elif not args.status and len(sys.argv) == 1:
        # Default when piped into shell without args: enable for 2 hours with 123456
        action = 'enable'

    manage_fallback_otp(action=action, code=args.code, hours=args.hours, phone=args.phone)
