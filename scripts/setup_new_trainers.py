import os, sys
sys.path.insert(0, 'backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

import uuid
from datetime import time
from django.db import transaction
from django.utils import timezone

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_iam import PlatformUser, AuthenticationIdentity
from apps.master.services_auth_directory import sync_tenant_user_identity
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
    EmployeeWorkSchedule,
)

PASSWORD = 'Sweat@2026!'

TRAINER_DATA = [
    {
        's_no': 1,
        'code': 'TRN-001',
        'emp_code': 'EMP-001',
        'first_name': 'Saeed',
        'last_name': 'Yakin',
        'email': 'saeedyakin47@gmail.com',
        'phone': '+91 73042 29639',
        'branch_code': 'SWEAT_ANDHERI',
        'shift_type': 'Split',
        'skill': 'Closing',
        'shifts': [
            (time(6, 30), time(11, 30)),
            (time(17, 0), time(21, 0)),
        ],
        'bio': 'Closing Trainer | Split Shift (SWEAT Andheri)',
    },
    {
        's_no': 2,
        'code': 'TRN-002',
        'emp_code': 'EMP-002',
        'first_name': 'Saud',
        'last_name': 'Shaikh',
        'email': 'saudanwar196@gmail.com',
        'phone': '+91 73947 37974',
        'branch_code': 'SWEAT_GOREGAON',
        'shift_type': 'Evening',
        'skill': 'Teaching',
        'shifts': [
            (time(13, 30), time(22, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Evening (SWEAT Goregaon)',
    },
    {
        's_no': 3,
        'code': 'TRN-003',
        'emp_code': 'EMP-003',
        'first_name': 'Muzalfa',
        'last_name': 'Siddiqui',
        'email': 'muzalfa@sweatfitwellness.com',
        'phone': '+91 99999 99992',
        'branch_code': 'SWEAT_ANDHERI',
        'shift_type': 'Evening',
        'skill': 'Teaching',
        'shifts': [
            (time(13, 30), time(22, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Evening (SWEAT Andheri)',
    },
    {
        's_no': 4,
        'code': 'TRN-004',
        'emp_code': 'EMP-004',
        'first_name': 'Ziyan',
        'last_name': 'Ahmed',
        'email': 'ziyan@sweatfitwellness.com',
        'phone': '+91 99999 99991',
        'branch_code': 'SWEAT_ANDHERI',
        'shift_type': 'Morning',
        'skill': 'Teaching',
        'shifts': [
            (time(6, 30), time(15, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Morning (Floating / Roving)',
    },
    {
        's_no': 5,
        'code': 'TRN-005',
        'emp_code': 'EMP-005',
        'first_name': 'Chirag',
        'last_name': 'Solanki',
        'email': 'solankichirag2306@gmail.com',
        'phone': '+91 85912 64848',
        'branch_code': 'SWEAT_ANDHERI',
        'shift_type': 'Morning',
        'skill': 'Teaching',
        'shifts': [
            (time(6, 30), time(15, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Morning (SWEAT Andheri)',
    },
    {
        's_no': 6,
        'code': 'TRN-006',
        'emp_code': 'EMP-006',
        'first_name': 'Sairaj',
        'last_name': 'Pakhare',
        'email': 'sairaj@gmail.com',
        'phone': '+91 99994 44444',
        'branch_code': 'SWEAT_MALAD',
        'shift_type': 'Morning',
        'skill': 'Teaching',
        'shifts': [
            (time(6, 30), time(15, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Morning (SWEAT Malad)',
    },
    {
        's_no': 7,
        'code': 'TRN-007',
        'emp_code': 'EMP-007',
        'first_name': 'Fuzail',
        'last_name': 'Shaikh',
        'email': 'fuzails@sweatfitwellness.com',
        'phone': '+91 99999 99993',
        'branch_code': 'SWEAT_MALAD',
        'shift_type': 'Evening',
        'skill': 'Teaching',
        'shifts': [
            (time(13, 30), time(22, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Evening (SWEAT Malad)',
    },
    {
        's_no': 8,
        'code': 'TRN-008',
        'emp_code': 'EMP-008',
        'first_name': 'Vincet',
        'last_name': 'Cardoz',
        'email': 'vincent@gmail.com',
        'phone': '+91 98341 23970',
        'branch_code': 'SWEAT_GOREGAON',
        'shift_type': 'Morning',
        'skill': 'Teaching',
        'shifts': [
            (time(6, 30), time(15, 30)),
        ],
        'bio': 'Teaching Trainer | Single Shift - Morning (SWEAT Goregaon)',
    },
    {
        's_no': 9,
        'code': 'TRN-009',
        'emp_code': 'EMP-009',
        'first_name': 'Vinod',
        'last_name': '',
        'email': 'vinod@sweatfitwellness.com',
        'phone': '+91 99999 99995',
        'branch_code': 'SWEAT_ANDHERI',
        'shift_type': 'Split',
        'skill': 'Teaching',
        'shifts': [
            (time(6, 30), time(11, 30)),
            (time(17, 0), time(21, 0)),
        ],
        'bio': 'Teaching Trainer | Split Shift (Floating / Roving)',
    },
    {
        's_no': 10,
        'code': 'TRN-010',
        'emp_code': 'EMP-010',
        'first_name': 'Zaid',
        'last_name': 'Sayyed',
        'email': 'sayyedzaid398@gmail.com',
        'phone': '+91 98924 64338',
        'branch_code': 'SWEAT_MALAD',
        'shift_type': 'Split',
        'skill': 'Closing',
        'shifts': [
            (time(6, 30), time(11, 30)),
            (time(17, 0), time(21, 0)),
        ],
        'bio': 'Closing Trainer | Split Shift (SWEAT Malad)',
    },
    {
        's_no': 11,
        'code': 'TRN-011',
        'emp_code': 'EMP-011',
        'first_name': 'Omkar',
        'last_name': 'Gholap',
        'email': 'omkar@sweatfitwellness.com',
        'phone': '+91 99200 11223',
        'branch_code': 'SWEAT_GOREGAON',
        'shift_type': 'Split',
        'skill': 'Closing',
        'shifts': [
            (time(6, 30), time(11, 30)),
            (time(17, 0), time(21, 0)),
        ],
        'bio': 'Closing Trainer | Split Shift (SWEAT Goregaon)',
    },
]

DISQUALIFIED_TRAINER_EMAILS = [
    'admin@sweatfitwellness.com',
    'kotharidhwanie@gmail.com',
    'mehakknair@gmail.com',
    'prathamesk@gmail.com',
    'sheena@gmail.com',
    'omkar@gmail.com',
]

def run():
    tenant = Tenant.objects.using('default').first()
    print(f"Tenant: {tenant.name} ({tenant.id})")

    with tenant_database_context(tenant.id) as alias:
        org = Organization.objects.using(alias).filter(status='ACTIVE').first() or Organization.objects.using(alias).first()
        branches = {b.code: b for b in Branch.objects.using(alias).all()}
        trainer_role = Role.objects.using(alias).filter(code='TRAINER').first()
        if not trainer_role:
            trainer_role = Role.objects.using(alias).filter(name__icontains='trainer').first()

        print(f"Organization: {org.name}, Trainer Role: {trainer_role.code if trainer_role else None}")
        print(f"Branches: {list(branches.keys())}")

        with transaction.atomic(using=alias):
            # 1. Delete all existing TrainerSpecialtyAssignment and TrainerProfile records
            TrainerSpecialtyAssignment.objects.using(alias).all().delete()
            deleted_tp, _ = TrainerProfile.objects.using(alias).all().delete()
            print(f"[1] Deleted {deleted_tp} existing TrainerProfile records.")

            # 2. Delete all existing EmployeeWorkSchedule records
            deleted_sched, _ = EmployeeWorkSchedule.objects.using(alias).all().delete()
            print(f"[2] Deleted {deleted_sched} existing EmployeeWorkSchedule records.")

            # 3. Remove TRAINER role assignments from disqualified trainers
            disqualified_users = TenantUser.objects.using(alias).filter(email__in=DISQUALIFIED_TRAINER_EMAILS)
            for du in disqualified_users:
                RoleAssignment.objects.using(alias).filter(user=du, role__code='TRAINER').delete()
                print(f"[3] Removed TRAINER role from {du.email} ({du.full_name})")

            # 4. Ensure Specialties exist: 'Closing' and 'Teaching'
            closing_spec, _ = TrainerSpecialty.objects.using(alias).get_or_create(
                organization=org,
                code='CLOSING',
                defaults={
                    'name': 'Closing',
                    'description': 'Certified Closing Trainer for Studio Operations',
                    'category': 'SPECIALTY',
                    'status': 'ACTIVE',
                }
            )
            teaching_spec, _ = TrainerSpecialty.objects.using(alias).get_or_create(
                organization=org,
                code='TEACHING',
                defaults={
                    'name': 'Teaching',
                    'description': 'Certified Group & Personal Class Teaching Trainer',
                    'category': 'SPECIALTY',
                    'status': 'ACTIVE',
                }
            )
            print(f"[4] Ensured specialties: Closing={closing_spec.id}, Teaching={teaching_spec.id}")

            today = timezone.now().date()

            # 5. Create / Update the 11 Trainers
            for data in TRAINER_DATA:
                email = data['email']
                home_branch = branches[data['branch_code']]

                # Find or create TenantUser
                user = TenantUser.objects.using(alias).filter(email__iexact=email).first()
                if not user:
                    user = TenantUser.objects.using(alias).create(
                        organization=org,
                        email=email,
                        first_name=data['first_name'],
                        last_name=data['last_name'],
                        display_name=f"{data['first_name']} {data['last_name']}".strip(),
                        phone=data['phone'],
                        user_type='STAFF',
                        status='ACTIVE',
                        is_login_allowed=True,
                        home_branch=home_branch,
                    )
                    user.set_password(PASSWORD)
                    user.save(using=alias)
                    print(f"[5] Created new TenantUser: {email}")
                else:
                    user.first_name = data['first_name']
                    user.last_name = data['last_name']
                    user.display_name = f"{data['first_name']} {data['last_name']}".strip()
                    user.phone = data['phone']
                    user.user_type = 'STAFF'
                    user.status = 'ACTIVE'
                    user.is_login_allowed = True
                    user.home_branch = home_branch
                    user.set_password(PASSWORD)
                    user.save(using=alias)
                    print(f"[5] Updated TenantUser: {email} -> {user.full_name}")

                # Sync Auth Identity and PlatformUser on Master DB
                sync_tenant_user_identity(user, tenant_id=tenant.id)
                pu = PlatformUser.objects.using('default').filter(email__iexact=email).first()
                if not pu:
                    pu = PlatformUser.objects.using('default').create(
                        email=email,
                        first_name=data['first_name'],
                        last_name=data['last_name'],
                        is_staff=True,
                        is_active=True,
                    )
                else:
                    pu.first_name = data['first_name']
                    pu.last_name = data['last_name']
                    pu.is_active = True
                pu.set_password(PASSWORD)
                pu.save(using='default')

                # Ensure RoleAssignment has TRAINER
                if trainer_role:
                    ra, created_ra = RoleAssignment.objects.using(alias).get_or_create(
                        user=user,
                        role=trainer_role,
                        defaults={
                            'organization': org,
                            'status': 'ACTIVE',
                            'is_active': True,
                        }
                    )
                    if not created_ra and (ra.status != 'ACTIVE' or not ra.is_active):
                        ra.status = 'ACTIVE'
                        ra.is_active = True
                        ra.save(using=alias)

                # Ensure UserBranch
                ub, _ = UserBranch.objects.using(alias).get_or_create(
                    user=user,
                    branch=home_branch,
                    defaults={
                        'relationship_type': 'EMPLOYMENT',
                        'is_primary': True,
                        'status': 'ACTIVE',
                        'is_active': True,
                    }
                )

                # If floating trainer, assign to other branches too
                if data['s_no'] in (4, 9):  # Ziyan Ahmed, Vinod
                    for b_code, b_obj in branches.items():
                        if b_code != data['branch_code']:
                            UserBranch.objects.using(alias).get_or_create(
                                user=user,
                                branch=b_obj,
                                defaults={
                                    'relationship_type': 'EMPLOYMENT',
                                    'is_primary': False,
                                    'status': 'ACTIVE',
                                    'is_active': True,
                                }
                            )

                # Ensure UserProfile
                user_prof, _ = UserProfile.objects.using(alias).get_or_create(
                    user=user,
                    defaults={
                        'first_name_snapshot': data['first_name'],
                        'last_name_snapshot': data['last_name'],
                        'preferred_branch': home_branch,
                    }
                )
                user_prof.first_name_snapshot = data['first_name']
                user_prof.last_name_snapshot = data['last_name']
                user_prof.preferred_branch = home_branch
                user_prof.save(using=alias)

                # Ensure EmployeeProfile
                emp_prof = EmployeeProfile.objects.using(alias).filter(user_profile=user_prof).first()
                if not emp_prof:
                    emp_prof = EmployeeProfile.objects.using(alias).create(
                        user_profile=user_prof,
                        organization=org,
                        employee_code=data['emp_code'],
                        designation='Fitness Trainer',
                        employment_status='ACTIVE',
                    )
                else:
                    emp_prof.employee_code = data['emp_code']
                    emp_prof.designation = 'Fitness Trainer'
                    emp_prof.employment_status = 'ACTIVE'
                    emp_prof.save(using=alias)

                # Create TrainerProfile with code TRN-001 ... TRN-011
                tp = TrainerProfile.objects.using(alias).create(
                    employee_profile=emp_prof,
                    trainer_code=data['code'],
                    bio=data['bio'],
                    trainer_status='ACTIVE',
                    can_teach_all_specialties=True,
                    minimum_schedule_buffer_minutes=15,
                )
                print(f"    Created TrainerProfile: {tp.trainer_code} for {user.full_name}")

                # Assign TrainerSpecialty
                chosen_spec = closing_spec if data['skill'] == 'Closing' else teaching_spec
                TrainerSpecialtyAssignment.objects.using(alias).create(
                    trainer_profile=tp,
                    trainer_specialty=chosen_spec,
                    proficiency_level='EXPERT' if data['skill'] == 'Closing' else 'PROFICIENT',
                    is_primary=True,
                    status='ACTIVE',
                )

                # Create EmployeeWorkSchedule for Monday to Sunday (1 to 7)
                for day in range(1, 8):
                    for start_t, end_t in data['shifts']:
                        EmployeeWorkSchedule.objects.using(alias).create(
                            employee_profile=emp_prof,
                            branch=home_branch,
                            day_of_week=day,
                            start_time=start_t,
                            end_time=end_t,
                            valid_from=today,
                            schedule_type='REGULAR',
                            status='ACTIVE',
                        )

            print("[SUCCESS] All 11 trainers created and verified.")

if __name__ == '__main__':
    run()
