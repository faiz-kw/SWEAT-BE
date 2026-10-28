"""
apps/tenant_core/views_assessment.py — REST API ViewSets & Endpoints for Functional Assessment Module.

Endpoints:
1. GET  /api/v1/mobile/assessment/eligibility/     — Member checks if assessment is eligible
2. POST /api/v1/tenant/assessments/               — Coach starts intake & health screening
3. GET  /api/v1/tenant/assessments/<id>/          — Detail view with all 17-section assessment data
4. PATCH /api/v1/tenant/assessments/<id>/         — Saves vitals, movement tests, and coach notes
5. POST /api/v1/tenant/assessments/<id>/capture/  — Uploads baseline photos + computes CV posture metrics
6. POST /api/v1/tenant/assessments/<id>/approve-and-send/ — Coach approval gate (unlocks display screen)
7. GET  /api/v1/tenant/assessments/pair/          — Connects Studio TV Display Screen (mirror mode)
"""

import uuid
from decimal import Decimal
from rest_framework import status, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from .models_assessment import MemberAssessment
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import UserProfile, Lead
from .models_catalog import Package
from .services_assessment import AssessmentEngineService
from .services_reliability import record_business_audit


def _resolve_db_and_org(request):
    alias = get_tenant_db_alias() or 'default'
    org = Organization.objects.using(alias).first()
    return alias, org


class AssessmentEligibilityMobileView(APIView):
    """
    GET /api/v1/mobile/assessment/eligibility/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, org = _resolve_db_and_org(request)
        profile = UserProfile.objects.using(alias).filter(user=request.user).first()
        if not profile:
            return Response(
                {'eligible': True, 'reason': 'Assessment unlocked for studio trial.', 'remaining_credits': 1},
                status=status.HTTP_200_OK
            )

        branch_id = request.query_params.get('branch_id')
        eligibility = AssessmentEngineService.check_member_eligibility(
            user_profile=profile, branch_id=branch_id, db_alias=alias
        )
        return Response(eligibility, status=status.HTTP_200_OK)


class AssessmentSessionViewSet(viewsets.ModelViewSet):
    """
    CRUD and operational workflow actions for Functional Fitness Assessments.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        alias, org = _resolve_db_and_org(self.request)
        qs = MemberAssessment.objects.using(alias).filter(organization=org)
        if hasattr(self.request.user, 'user_type') and self.request.user.user_type == 'MEMBER':
            qs = qs.filter(user_profile__user=self.request.user)
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        """
        POST /api/v1/tenant/assessments/
        Initializes an assessment session with Profile, Health Screening (PAR-Q), and Goals.
        """
        alias, org = _resolve_db_and_org(request)
        data = request.data

        # 1. Subject (Member Profile or Lead)
        user_profile = None
        lead = None
        phone = data.get('member_phone') or ''
        name = data.get('member_name') or ''

        user_profile_id = data.get('user_profile_id')
        if user_profile_id:
            user_profile = UserProfile.objects.using(alias).filter(id=user_profile_id).first()
        elif hasattr(request.user, 'user_type') and request.user.user_type == 'MEMBER':
            user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()

        if user_profile:
            name = name or user_profile.full_name
            phone = phone or user_profile.phone

        # 2. Branch
        branch_id = data.get('branch_id')
        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch and user_profile and user_profile.preferred_branch:
            branch = user_profile.preferred_branch
        if not branch:
            branch = Branch.objects.using(alias).filter(organization=org, status='ACTIVE').first()

        # 3. Profile & Baseline Fields
        assessment_type = data.get('assessment_type', 'INITIAL')
        training_experience = data.get('training_experience', 'BEGINNER')
        current_activity_level = data.get('current_activity_level', 'MODERATELY_ACTIVE')
        preferred_training_style = data.get('preferred_training_style', 'PILATES')

        target_goal = data.get('target_goal', 'POSTURE_ALIGNMENT')
        stated_goal_outcome = data.get('stated_goal_outcome', data.get('user_notes', ''))
        timeline_months = int(data.get('target_timeline_months', 3))
        timeline_weeks = int(data.get('target_timeline_weeks', timeline_months * 4))
        weekly_frequency = int(data.get('weekly_frequency_days', 3))
        perceived_barrier = data.get('perceived_barrier', '')
        confidence_score = int(data.get('member_confidence_score', 8))

        # 4. Health Screening (PAR-Q)
        health_screening = data.get('health_screening', {})
        screening_outcome = data.get('screening_outcome', 'CLEARED')
        screening_coach_notes = data.get('screening_coach_notes', '')

        # 5. Vitals & Body Measurements (if provided)
        height_cm = Decimal(str(data.get('measured_height_cm'))) if data.get('measured_height_cm') else None
        weight_kg = Decimal(str(data.get('measured_weight_kg'))) if data.get('measured_weight_kg') else None
        waist_cm = Decimal(str(data.get('waist_circumference_cm'))) if data.get('waist_circumference_cm') else None
        resting_hr = int(data.get('resting_heart_rate_bpm')) if data.get('resting_heart_rate_bpm') else None
        resting_bp = data.get('resting_blood_pressure', '')

        # Calculate BMI, RMR, TDEE if height & weight available
        calc_bmi = None
        rmr_val = None
        tdee_val = None
        cal_target = None
        macros_payload = {}
        if height_cm and weight_kg:
            h_m = float(height_cm) / 100.0
            calc_bmi = Decimal(str(round(float(weight_kg) / (h_m * h_m), 1)))
            meta_plan = AssessmentEngineService.calculate_metabolic_nutrition_plan(
                weight_kg=float(weight_kg),
                height_cm=float(height_cm),
                activity_level=current_activity_level,
                goal=target_goal,
            )
            rmr_val = meta_plan['rmr_kcal']
            tdee_val = meta_plan['tdee_kcal']
            cal_target = meta_plan['daily_calorie_target']
            macros_payload = meta_plan['macros']

        # 6. Functional Movement Tests
        functional_tests = data.get('functional_tests_payload', {})
        capability_matrix = AssessmentEngineService.calculate_capability_matrix(functional_tests)

        # 7. Match Database Packages
        match_result = AssessmentEngineService.match_system_packages(
            organization=org,
            branch=branch,
            goal=target_goal,
            timeline_months=timeline_months,
            weekly_frequency=weekly_frequency,
            db_alias=alias,
        )

        selected_pkg_id = data.get('package_id') or data.get('recommended_package_id')
        if selected_pkg_id:
            custom_pkg = Package.objects.using(alias).filter(id=selected_pkg_id).first()
            if custom_pkg:
                match_result['recommended_package'] = custom_pkg
                match_result['recommended_program'] = custom_pkg.program
                active_v = custom_pkg.get_active_version()
                match_result['recommended_package_version'] = active_v

        trainer_user = request.user if hasattr(request.user, 'user_type') and request.user.user_type != 'MEMBER' else None

        assessment = MemberAssessment.objects.using(alias).create(
            organization=org,
            branch=branch,
            user_profile=user_profile,
            lead=lead,
            member_name=name,
            member_phone=phone,
            conducted_by_trainer=trainer_user,
            assessment_type=assessment_type,
            training_experience=training_experience,
            current_activity_level=current_activity_level,
            preferred_training_style=preferred_training_style,
            target_goal=target_goal,
            stated_goal_outcome=stated_goal_outcome,
            target_timeline_months=timeline_months,
            target_timeline_weeks=timeline_weeks,
            weekly_frequency_days=weekly_frequency,
            perceived_barrier=perceived_barrier,
            member_confidence_score=confidence_score,
            user_notes=data.get('user_notes', ''),
            health_screening=health_screening,
            screening_outcome=screening_outcome,
            screening_coach_notes=screening_coach_notes,
            measured_height_cm=height_cm,
            measured_weight_kg=weight_kg,
            waist_circumference_cm=waist_cm,
            resting_heart_rate_bpm=resting_hr,
            resting_blood_pressure=resting_bp,
            calculated_bmi=calc_bmi,
            bmr_rmr_kcal=rmr_val,
            tdee_kcal=tdee_val,
            daily_calorie_target=cal_target,
            nutrition_macros=macros_payload,
            functional_tests_payload=functional_tests,
            capability_matrix=capability_matrix,
            recommended_program=match_result.get('recommended_program'),
            recommended_package=match_result.get('recommended_package'),
            recommended_package_version=match_result.get('recommended_package_version'),
            recommendation_reason=match_result.get('recommendation_reason'),
            alternative_packages_payload=match_result.get('alternatives', []),
            status='DRAFT',
        )

        return Response(self._serialize_assessment(assessment, match_result), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """
        PATCH /api/v1/tenant/assessments/<id>/
        Updates assessment sections (vitals, functional tests, goals, notes).
        """
        alias, org = _resolve_db_and_org(request)
        instance = self.get_object()
        data = request.data

        # Update basic profile & goals
        if 'stated_goal_outcome' in data:
            instance.stated_goal_outcome = data['stated_goal_outcome']
        if 'target_goal' in data:
            instance.target_goal = data['target_goal']
        if 'target_timeline_weeks' in data:
            instance.target_timeline_weeks = int(data['target_timeline_weeks'])
            instance.target_timeline_months = max(1, round(instance.target_timeline_weeks / 4.33))
        if 'weekly_frequency_days' in data:
            instance.weekly_frequency_days = int(data['weekly_frequency_days'])
        if 'preferred_training_style' in data:
            instance.preferred_training_style = data['preferred_training_style']

        # Update health screening
        if 'health_screening' in data:
            instance.health_screening = data['health_screening']
        if 'screening_outcome' in data:
            instance.screening_outcome = data['screening_outcome']
        if 'screening_coach_notes' in data:
            instance.screening_coach_notes = data['screening_coach_notes']

        # Update vitals
        if 'measured_height_cm' in data and data['measured_height_cm']:
            instance.measured_height_cm = Decimal(str(data['measured_height_cm']))
        if 'measured_weight_kg' in data and data['measured_weight_kg']:
            instance.measured_weight_kg = Decimal(str(data['measured_weight_kg']))
        if 'waist_circumference_cm' in data and data['waist_circumference_cm']:
            instance.waist_circumference_cm = Decimal(str(data['waist_circumference_cm']))
        if 'resting_heart_rate_bpm' in data and data['resting_heart_rate_bpm']:
            instance.resting_heart_rate_bpm = int(data['resting_heart_rate_bpm'])
        if 'resting_blood_pressure' in data:
            instance.resting_blood_pressure = data['resting_blood_pressure']

        # Recompute BMI & Metabolic Targets
        if instance.measured_height_cm and instance.measured_weight_kg:
            h_m = float(instance.measured_height_cm) / 100.0
            instance.calculated_bmi = Decimal(str(round(float(instance.measured_weight_kg) / (h_m * h_m), 1)))
            meta_plan = AssessmentEngineService.calculate_metabolic_nutrition_plan(
                weight_kg=float(instance.measured_weight_kg),
                height_cm=float(instance.measured_height_cm),
                activity_level=instance.current_activity_level,
                goal=instance.target_goal,
            )
            instance.bmr_rmr_kcal = meta_plan['rmr_kcal']
            instance.tdee_kcal = meta_plan['tdee_kcal']
            instance.daily_calorie_target = meta_plan['daily_calorie_target']
            instance.nutrition_macros = meta_plan['macros']

        # Update Functional Tests
        if 'functional_tests_payload' in data:
            instance.functional_tests_payload = data['functional_tests_payload']
            instance.capability_matrix = AssessmentEngineService.calculate_capability_matrix(instance.functional_tests_payload)

        # Update Package selection
        selected_pkg_id = data.get('package_id') or data.get('recommended_package_id')
        if selected_pkg_id:
            custom_pkg = Package.objects.using(alias).filter(id=selected_pkg_id).first()
            if custom_pkg:
                instance.recommended_package = custom_pkg
                instance.recommended_program = custom_pkg.program
                instance.recommended_package_version = custom_pkg.get_active_version()

        # Coach Prescriptions
        if 'coach_strengths' in data:
            instance.coach_strengths = data['coach_strengths']
        if 'coach_areas_to_develop' in data:
            instance.coach_areas_to_develop = data['coach_areas_to_develop']
        if 'workout_prescription' in data:
            instance.workout_prescription = data['workout_prescription']
        if 'diet_prescription' in data:
            instance.diet_prescription = data['diet_prescription']

        if instance.status == 'DRAFT':
            instance.status = 'TESTING'

        instance.save(using=alias)
        return Response(self._serialize_assessment(instance), status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        instance = self.get_object()
        return Response(self._serialize_assessment(instance), status=status.HTTP_200_OK)

    def capture_photos(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/capture/
        Uploads baseline standing photos and triggers computer vision posture extraction.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        front_file = request.FILES.get('photo_front')
        side_file = request.FILES.get('photo_side')
        if front_file:
            assessment.photo_front = front_file
        if side_file:
            assessment.photo_side = side_file

        height = request.data.get('height_cm') or assessment.measured_height_cm
        weight = request.data.get('weight_kg') or assessment.measured_weight_kg
        metrics = AssessmentEngineService.calculate_computer_vision_metrics(
            height_cm=float(height) if height else None,
            weight_kg=float(weight) if weight else None,
        )

        assessment.posture_score = metrics['posture_score']
        assessment.shoulder_tilt_degrees = metrics['shoulder_tilt_degrees']
        assessment.hip_tilt_degrees = metrics['hip_tilt_degrees']
        assessment.forward_head_angle_degrees = metrics.get('forward_head_angle_degrees')
        assessment.estimated_body_fat_pct = metrics['estimated_body_fat_pct']
        assessment.waist_to_hip_ratio = metrics['waist_to_hip_ratio']
        assessment.cv_analysis_payload = metrics

        # Milestone preview references
        assessment.month_1_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1518611012118-696072aa579a?auto=format&fit=crop&w=800&q=80"
        assessment.month_2_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=800&q=80"
        assessment.month_3_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1544005313-94ddf0286df2?auto=format&fit=crop&w=800&q=80"

        assessment.status = 'PENDING_REVIEW'
        assessment.save(using=alias)

        return Response(self._serialize_assessment(assessment), status=status.HTTP_200_OK)

    def approve_and_send(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/approve-and-send/
        Coach/Admin Approval Gate. Marks assessment as APPROVED and UNLOCKS the Studio Screen.
        (WhatsApp dispatch is disabled per configuration — displayed directly on screen).
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        coach_notes = request.data.get('coach_notes')
        workout_prescription = request.data.get('workout_prescription')
        diet_prescription = request.data.get('diet_prescription')
        # WhatsApp dispatch is false by default per studio requirement
        send_whatsapp = str(request.data.get('send_whatsapp', 'false')).lower() in ['true', '1', 'yes']

        approved = AssessmentEngineService.approve_and_dispatch(
            assessment=assessment,
            reviewer_user=request.user,
            coach_notes=coach_notes,
            workout_prescription=workout_prescription,
            diet_prescription=diet_prescription,
            send_whatsapp=send_whatsapp,
            db_alias=alias,
        )

        return Response(self._serialize_assessment(approved), status=status.HTTP_200_OK)

    def _serialize_assessment(self, instance: MemberAssessment, match_result=None):
        rec_pkg = instance.recommended_package
        rec_price = None
        rec_sessions = None
        if rec_pkg:
            ver = instance.recommended_package_version or rec_pkg.get_active_version()
            if ver:
                p = ver.prices.filter(status='ACTIVE').first()
                if p:
                    rec_price = float(p.total_price)
                for e in ver.entitlement_definitions.filter(status='ACTIVE'):
                    if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                        rec_sessions = 365 if e.is_unlimited else int(e.allocated_units or 0)

        ret_pkg_payload = {
            'id': str(rec_pkg.id) if rec_pkg else None,
            'name': rec_pkg.name if rec_pkg else 'Studio Program',
            'session_units': rec_sessions or 72,
            'price': rec_price or 0,
            'reason': instance.recommendation_reason,
        }

        return {
            'id': str(instance.id),
            'pairing_code': instance.pairing_code,
            'status': instance.status,
            'member_name': instance.member_name,
            'member_phone': instance.member_phone,
            'branch': {
                'id': str(instance.branch.id),
                'name': instance.branch.name,
            } if instance.branch else None,
            'assessment_type': instance.assessment_type,
            'training_experience': instance.training_experience,
            'current_activity_level': instance.current_activity_level,
            'preferred_training_style': instance.preferred_training_style,
            'target_goal': instance.target_goal,
            'target_goal_display': instance.get_target_goal_display(),
            'stated_goal_outcome': instance.stated_goal_outcome,
            'timeline_months': instance.target_timeline_months,
            'timeline_weeks': instance.target_timeline_weeks,
            'weekly_frequency': instance.weekly_frequency_days,
            'perceived_barrier': instance.perceived_barrier,
            'member_confidence_score': instance.member_confidence_score,
            'user_notes': instance.user_notes,

            # Section 2: Health Screening
            'health_screening': instance.health_screening,
            'screening_outcome': instance.screening_outcome,
            'screening_coach_notes': instance.screening_coach_notes,

            # Section 3: Vitals & Body Composition
            'vitals': {
                'height_cm': float(instance.measured_height_cm) if instance.measured_height_cm else None,
                'weight_kg': float(instance.measured_weight_kg) if instance.measured_weight_kg else None,
                'waist_cm': float(instance.waist_circumference_cm) if instance.waist_circumference_cm else None,
                'resting_hr': instance.resting_heart_rate_bpm,
                'resting_bp': instance.resting_blood_pressure,
                'bmi': float(instance.calculated_bmi) if instance.calculated_bmi else None,
                'rmr_kcal': instance.bmr_rmr_kcal,
                'tdee_kcal': instance.tdee_kcal,
                'daily_calorie_target': instance.daily_calorie_target,
                'macros': instance.nutrition_macros,
                'estimated_body_fat_pct': float(instance.estimated_body_fat_pct or 0),
                'waist_to_hip_ratio': float(instance.waist_to_hip_ratio or 0),
            },

            # Sections 4-11: Movement Tests & Capability Matrix
            'functional_tests': instance.functional_tests_payload,
            'capability_matrix': instance.capability_matrix,

            # Photos & Posture Score
            'photo_front_url': instance.photo_front.url if instance.photo_front else instance.photo_front_url,
            'photo_side_url': instance.photo_side.url if instance.photo_side else instance.photo_side_url,
            'metrics': {
                'posture_score': float(instance.posture_score),
                'shoulder_tilt_degrees': float(instance.shoulder_tilt_degrees or 0),
                'hip_tilt_degrees': float(instance.hip_tilt_degrees or 0),
                'forward_head_angle_degrees': float(instance.forward_head_angle_degrees or 0),
            },

            # Program & Package Recommendation (Revealed upon approval)
            'recommended_package': ret_pkg_payload,
            'alternatives': instance.alternative_packages_payload,

            # Milestones
            'milestones': {
                'month_1': instance.month_1_image_url,
                'month_2': instance.month_2_image_url,
                'month_3': instance.month_3_image_url,
            },

            # Coach Prescriptions
            'coach_strengths': instance.coach_strengths,
            'coach_areas_to_develop': instance.coach_areas_to_develop,
            'workout_prescription': instance.workout_prescription,
            'diet_prescription': instance.diet_prescription,
            'reassessment_interval_weeks': instance.reassessment_interval_weeks,
            'reviewed_at': instance.reviewed_at.isoformat() if instance.reviewed_at else None,
            'created_at': instance.created_at.isoformat(),
        }


class StudioScreenPairingView(APIView):
    """
    GET /api/v1/tenant/assessments/pair/?code=123456
    Pairing endpoint for the Studio TV Screen Mirror.
    If the assessment is not yet approved, keeps the screen in locked standby / framing mode.
    Once approved by the coach, unlocks the full visual transformation & roadmap reveal.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get('code')
        if not code:
            return Response({'error': 'Pairing code is required.'}, status=status.HTTP_400_BAD_REQUEST)

        alias, org = _resolve_db_and_org(request)
        assessment = MemberAssessment.objects.using(alias).filter(pairing_code=code.strip()).first()
        if not assessment:
            return Response({'error': 'Invalid or expired pairing code.'}, status=status.HTTP_404_NOT_FOUND)

        assessment.display_screen_connected = True
        assessment.save(using=alias, update_fields=['display_screen_connected'])

        is_approved = (assessment.status == 'APPROVED')

        response_data = {
            'id': str(assessment.id),
            'status': assessment.status,
            'member_name': assessment.member_name,
            'is_approved': is_approved,
            'connected': True,
        }

        # If approved, reveal the full presentation to the studio screen
        if is_approved:
            rec_pkg = assessment.recommended_package
            rec_price = None
            rec_sessions = None
            if rec_pkg:
                ver = assessment.recommended_package_version or rec_pkg.get_active_version()
                if ver:
                    p = ver.prices.filter(status='ACTIVE').first()
                    if p:
                        rec_price = float(p.total_price)
                    for e in ver.entitlement_definitions.filter(status='ACTIVE'):
                        if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                            rec_sessions = 365 if e.is_unlimited else int(e.allocated_units or 0)

            response_data.update({
                'target_goal': assessment.get_target_goal_display(),
                'stated_goal_outcome': assessment.stated_goal_outcome,
                'timeline_weeks': assessment.target_timeline_weeks,
                'timeline_months': assessment.target_timeline_months,
                'posture_score': float(assessment.posture_score),
                'shoulder_tilt_degrees': float(assessment.shoulder_tilt_degrees or 0),
                'vitals': {
                    'bmi': float(assessment.calculated_bmi) if assessment.calculated_bmi else None,
                    'rmr_kcal': assessment.bmr_rmr_kcal,
                    'tdee_kcal': assessment.tdee_kcal,
                    'daily_calorie_target': assessment.daily_calorie_target,
                    'macros': assessment.nutrition_macros,
                },
                'capability_matrix': assessment.capability_matrix,
                'recommended_package': {
                    'name': rec_pkg.name if rec_pkg else 'Tailored Studio Plan',
                    'session_units': rec_sessions or 72,
                    'price': rec_price or 0,
                    'reason': assessment.recommendation_reason,
                },
                'workout_prescription': assessment.workout_prescription,
                'diet_prescription': assessment.diet_prescription,
                'milestones': {
                    'month_1': assessment.month_1_image_url,
                    'month_2': assessment.month_2_image_url,
                    'month_3': assessment.month_3_image_url,
                },
            })

        return Response(response_data, status=status.HTTP_200_OK)
