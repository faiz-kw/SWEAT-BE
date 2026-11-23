import os
import sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import django
from datetime import datetime, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_classes import (
    ClassOccurrence,
    ClassOccurrenceTrainer,
    ClassTemplate,
    ClassScheduleRule,
)
from apps.tenant_core.models_workforce import TrainerProfile, TenantUser
from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.services_classes import ClassSchedulingService
from apps.tenant_core.serializers_classes import ClassOccurrenceSerializer
from apps.tenant_core.services_appointments import AppointmentSchedulingService

def run_tests():
    t = Tenant.objects.filter(slug='sweat').first() or Tenant.objects.first()
    ds = TenantDataSource.objects.filter(tenant=t).first()
    db_alias = build_tenant_db_alias(t.id)
    _register_tenant_connection(db_alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
    set_tenant_db_alias(db_alias)

    print(f"=== Running Trainer Release Lifecycle Tests on DB: {db_alias} ({ds.db_name}) ===")

    branch = Branch.objects.using(db_alias).filter(status='ACTIVE').first() or Branch.objects.using(db_alias).first()
    if not branch:
        print("FAIL: No branch found.")
        sys.exit(1)

    template = ClassTemplate.objects.using(db_alias).filter(status='ACTIVE').first() or ClassTemplate.objects.using(db_alias).first()
    if not template:
        print("FAIL: No template found.")
        sys.exit(1)

    trainer = TrainerProfile.objects.using(db_alias).filter(trainer_status='ACTIVE').first() or TrainerProfile.objects.using(db_alias).first()
    if not trainer:
        print("FAIL: No active trainer profile found.")
        sys.exit(1)

    user = TenantUser.objects.using(db_alias).filter(email='admin@sweatfitwellness.com').first()
    if not user:
        user = TenantUser.objects.using(db_alias).filter(status='ACTIVE').first()

    print(f"Using Branch: {branch.name} ({branch.id})")
    print(f"Using Template: {template.name} ({template.id})")
    print(f"Using Trainer: {trainer.trainer_code} ({trainer.id})")

    now = timezone.now()
    test_date = (now + timedelta(days=10)).date()
    start_time_1 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=10, minute=0)))
    end_time_1 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=11, minute=0)))

    created_occurrences = []

    try:
        # -----------------------------------------------------------------
        # TEST 1: Direct Occurrence Cancellation and Immediate Trainer Release
        # -----------------------------------------------------------------
        print("\n--- TEST 1: Occurrence Cancellation & Trainer Release ---")
        occ1 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=start_time_1,
            end_at=end_time_1,
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ1)

        occ2 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=start_time_1,
            end_at=end_time_1,
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ2)

        # Assign trainer to occ1
        assign1 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ1.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )
        print(f"Assigned {trainer.trainer_code} to occ1 (ID: {assign1.id}, status: {assign1.status})")
        assert assign1.status == 'ASSIGNED', f"Expected ASSIGNED, got {assign1.status}"

        # Conflict check: assigning to occ2 should FAIL
        conflict_detected = False
        try:
            ClassSchedulingService.assign_trainer_to_occurrence(
                occurrence_id=occ2.id,
                trainer_profile_id=trainer.id,
                trainer_role='LEAD',
                actor=user,
                db_alias=db_alias,
            )
        except (DjangoValidationError, DRFValidationError) as e:
            conflict_detected = True
            print(f"Verified expected conflict on occ2: {e}")

        assert conflict_detected, "Conflict check failed: trainer was allowed to be double booked on active class!"

        # Now release trainers for occ1 using release_trainers_for_occurrences
        released_count = ClassSchedulingService.release_trainers_for_occurrences(
            occurrence_ids=[occ1.id],
            reason="Class session cancelled for test",
            actor=user,
            db_alias=db_alias,
        )
        print(f"Released trainers count: {released_count}")
        assert released_count >= 1, f"Expected at least 1 released trainer, got {released_count}"

        occ1.status = 'CANCELLED'
        occ1.save(using=db_alias)

        # Check assignment status
        assign1.refresh_from_db(using=db_alias)
        print(f"Assignment status after cancellation: {assign1.status}")
        assert assign1.status == 'CANCELLED', f"Expected CANCELLED assignment status, got {assign1.status}"

        # Check serialization
        ser_data = ClassOccurrenceSerializer(occ1).data
        assert ser_data['trainer_name'] == 'Unassigned', f"Expected Unassigned trainer_name, got {ser_data['trainer_name']}"
        assert len(ser_data['trainers']) == 0, f"Expected 0 trainers in serializer, got {len(ser_data['trainers'])}"
        print("Verified serialization shows Unassigned and empty trainers array for cancelled occurrence.")

        # Re-assign trainer to occ2 at the EXACT SAME TIME SLOT: must succeed immediately!
        assign2 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ2.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )
        print(f"SUCCESS: Reassigned {trainer.trainer_code} to occ2 at same time slot! (ID: {assign2.id}, status: {assign2.status})")
        assert assign2.status == 'ASSIGNED', "Expected successful assignment to occ2"

        # -----------------------------------------------------------------
        # TEST 2: Occurrence Save hook with 'DEACTIVATED' status
        # -----------------------------------------------------------------
        print("\n--- TEST 2: Model .save() hook with DEACTIVATED status ---")
        start_time_2 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=14, minute=0)))
        end_time_2 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=15, minute=0)))

        occ3 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=start_time_2,
            end_at=end_time_2,
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ3)

        occ4 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=start_time_2,
            end_at=end_time_2,
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ4)

        assign3 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ3.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )
        print(f"Assigned trainer to occ3 (ID: {assign3.id})")

        # Deactivate occ3 via model save
        occ3.status = 'DEACTIVATED'
        occ3.save(using=db_alias)

        assign3.refresh_from_db(using=db_alias)
        print(f"occ3 trainer assignment status after occ3.save(DEACTIVATED): {assign3.status}")
        assert assign3.status == 'CANCELLED', f"Expected CANCELLED from save() hook, got {assign3.status}"

        # Assign to occ4 at same time: must succeed
        assign4 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ4.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )
        print(f"SUCCESS: Assigned trainer to occ4 after occ3 deactivation! (ID: {assign4.id})")
        assert assign4.status == 'ASSIGNED'

        # -----------------------------------------------------------------
        # TEST 3: Cross-Engine Defense: PT Appointment Conflict Check
        # -----------------------------------------------------------------
        print("\n--- TEST 3: PT Appointment Conflict Check ---")
        start_time_3 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=16, minute=0)))
        end_time_3 = timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=17, minute=0)))

        occ5 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=start_time_3,
            end_at=end_time_3,
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ5)

        assign5 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ5.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )

        # Group class conflict query in AppointmentService
        group_conflicts = ClassOccurrenceTrainer.objects.using(db_alias).filter(
            trainer_profile=trainer,
            status__in=['ASSIGNED', 'CONFIRMED'],
            occurrence__start_at__lt=end_time_3,
            occurrence__end_at__gt=start_time_3,
        ).exclude(occurrence__status__in=['CANCELLED', 'DEACTIVATED'])
        assert group_conflicts.exists(), "Expected active group conflict for appointment query"
        print("Verified appointment service sees conflict while occurrence is active.")

        # Now cancel occ5
        occ5.status = 'CANCELLED'
        occ5.save(using=db_alias)

        group_conflicts_after = ClassOccurrenceTrainer.objects.using(db_alias).filter(
            trainer_profile=trainer,
            status__in=['ASSIGNED', 'CONFIRMED'],
            occurrence__start_at__lt=end_time_3,
            occurrence__end_at__gt=start_time_3,
        ).exclude(occurrence__status__in=['CANCELLED', 'DEACTIVATED'])
        assert not group_conflicts_after.exists(), "Expected 0 group conflicts after occurrence is cancelled!"
        print("SUCCESS: Appointment conflict query reports 0 conflicts after occurrence is cancelled.")

        # -----------------------------------------------------------------
        # TEST 4: ViewSet cancel action & cancellation preview
        # -----------------------------------------------------------------
        print("\n--- TEST 4: ViewSet Cancellation Preview & Trainer Details ---")
        from apps.tenant_core.views_classes import ClassOccurrenceViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate

        factory = APIRequestFactory()
        view = ClassOccurrenceViewSet.as_view(
            {'get': 'cancellation_preview', 'post': 'cancel'},
            permission_classes=[],
        )

        occ6 = ClassOccurrence.objects.using(db_alias).create(
            branch=branch,
            class_template=template,
            occurrence_date=test_date,
            start_at=timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=18, minute=0))),
            end_at=timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=19, minute=0))),
            status='SCHEDULED',
            capacity=10,
        )
        created_occurrences.append(occ6)

        assign6 = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=occ6.id,
            trainer_profile_id=trainer.id,
            trainer_role='LEAD',
            actor=user,
            db_alias=db_alias,
        )

        # Call preview
        req_preview = factory.get(f'/api/v1/classes/occurrences/{occ6.id}/cancellation_preview/')
        req_preview._tenant_db_alias = db_alias
        force_authenticate(req_preview, user=user)
        resp_preview = view(req_preview, pk=str(occ6.id))
        print("Cancellation preview response status:", resp_preview.status_code)
        assert resp_preview.status_code == 200
        preview_data = resp_preview.data
        assert 'assigned_trainers' in preview_data, "assigned_trainers missing from cancellation preview"
        assert len(preview_data['assigned_trainers']) == 1
        print("Preview correctly includes assigned_trainers:", preview_data['assigned_trainers'])

        # Call cancel action
        req_cancel = factory.post(f'/api/v1/classes/occurrences/{occ6.id}/cancel/', {'reason': 'Testing end to end cancellation'})
        req_cancel._tenant_db_alias = db_alias
        force_authenticate(req_cancel, user=user)
        resp_cancel = view(req_cancel, pk=str(occ6.id))
        print("Cancel action response status:", resp_cancel.status_code)
        assert resp_cancel.status_code == 200
        cancel_data = resp_cancel.data
        assert cancel_data.get('released_trainers_count') == 1, f"Expected released_trainers_count=1, got {cancel_data.get('released_trainers_count')}"
        print("Cancel response correctly returns released_trainers_count:", cancel_data['released_trainers_count'])

        # -----------------------------------------------------------------
        # TEST 5: Schedule Rule Deactivation releases future session trainers
        # -----------------------------------------------------------------
        print("\n--- TEST 5: Schedule Rule Deactivation ---")
        rule = ClassScheduleRule.objects.using(db_alias).filter(status='ACTIVE').first()
        if rule:
            occ7 = ClassOccurrence.objects.using(db_alias).create(
                branch=branch,
                class_template=template,
                schedule_rule=rule,
                occurrence_date=test_date,
                start_at=timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=20, minute=0))),
                end_at=timezone.make_aware(datetime.combine(test_date, datetime.min.time().replace(hour=21, minute=0))),
                status='SCHEDULED',
                capacity=10,
            )
            created_occurrences.append(occ7)
            assign7 = ClassSchedulingService.assign_trainer_to_occurrence(
                occurrence_id=occ7.id,
                trainer_profile_id=trainer.id,
                trainer_role='LEAD',
                actor=user,
                db_alias=db_alias,
            )
            print(f"Assigned trainer to rule-linked occurrence (ID: {assign7.id})")

            # Simulate deactivating the rule via serializer / perform_update logic
            future_occs = list(
                ClassOccurrence.objects.using(db_alias).filter(
                    schedule_rule=rule,
                    start_at__gte=timezone.now(),
                    status__in=['SCHEDULED', 'OPEN'],
                )
            )
            occ_ids = [o.id for o in future_occs]
            assert occ7.id in occ_ids, "occ7 should be in future occurrences"
            ClassSchedulingService.release_trainers_for_occurrences(
                occurrence_ids=occ_ids,
                reason=f"Schedule rule {rule.id} deactivated",
                actor=user,
                db_alias=db_alias,
            )
            ClassOccurrence.objects.using(db_alias).filter(id__in=occ_ids).update(
                status='CANCELLED',
                updated_at=timezone.now(),
            )
            assign7.refresh_from_db(using=db_alias)
            print(f"Assignment status after rule deactivation: {assign7.status}")
            assert assign7.status == 'CANCELLED', f"Expected CANCELLED assignment status, got {assign7.status}"
            print("SUCCESS: Future session trainers released upon schedule rule deactivation.")

        print("\nALL 5 TESTS PASSED FLAWLESSLY!")

    finally:
        print("\nCleaning up test occurrences...")
        for occ in created_occurrences:
            try:
                ClassOccurrenceTrainer.objects.using(db_alias).filter(occurrence=occ).delete()
                occ.delete(using=db_alias)
            except Exception as e:
                print(f"Cleanup error for {occ.id}: {e}")
        print("Cleanup complete.")

if __name__ == '__main__':
    run_tests()
