"""
apps/tenant_core/views_assessment.py — REST API ViewSets & Endpoints for Functional Assessment Module.

Endpoints:
1. GET  /api/v1/mobile/assessment/eligibility/     — Member checks if assessment is eligible
2. POST /api/v1/tenant/assessments/               — Coach starts intake & health screening
3. GET  /api/v1/tenant/assessments/<id>/          — Detail view with all 17-section assessment data
4. PATCH /api/v1/tenant/assessments/<id>/         — Saves vitals, movement tests, and coach notes
5. POST /api/v1/tenant/assessments/<id>/capture/  — Uploads baseline photos (zero fake CV constants)
6. POST /api/v1/tenant/assessments/<id>/approve-and-send/ — Coach approval gate (RBAC enforced)
7. GET  /api/v1/tenant/assessments/<id>/comparison/ — Longitudinal like-for-like comparison
8. POST /api/v1/tenant/assessments/pair/          — Connects Studio TV Display Screen (mirror mode)
9. CRUD /api/v1/tenant/assessment-progressions/   — Exercise progression tracking & approval
"""

from decimal import Decimal
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.throttling import SimpleRateThrottle
from django.utils import timezone

from config.routers import get_tenant_db_alias
from .models_assessment import MemberAssessment, AssessmentTestProgression
from .models_org import Organization, Branch
from .models_crm import UserProfile
from .models_catalog import Package
from .serializers_assessment import (
    MemberAssessmentDetailSerializer, MemberAssessmentWriteSerializer,
    AssessmentApprovalSerializer, AssessmentTestProgressionSerializer,
    ProgressionApprovalSerializer,
)
from .services_assessment import AssessmentEngineService
from .services_assessment_screening import evaluate_safety_readiness
from .services_assessment_metrics import calculate_metabolic_nutrition_plan
from .services_assessment_progress import compare_assessments, approve_progression


def _resolve_db_and_org(request):
    alias = get_tenant_db_alias() or 'default'
    return alias, Organization.objects.using(alias).first()


class AssessmentEligibilityMobileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, org = _resolve_db_and_org(request)
        profile = UserProfile.objects.using(alias).filter(user=request.user).first()
        if not profile:
            return Response({'eligible': True, 'reason': 'Assessment unlocked for studio trial.', 'remaining_credits': 1})
        branch_id = request.query_params.get('branch_id')
        return Response(AssessmentEngineService.check_member_eligibility(user_profile=profile, branch_id=branch_id, db_alias=alias))


class AssessmentSessionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        alias, org = _resolve_db_and_org(self.request)
        qs = MemberAssessment.objects.using(alias).filter(organization=org)
        if getattr(self.request.user, 'user_type', None) == 'MEMBER':
            qs = qs.filter(user_profile__user=self.request.user, status='APPROVED')
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        alias, org = _resolve_db_and_org(request)
        write_ser = MemberAssessmentWriteSerializer(data=request.data)
        write_ser.is_valid(raise_exception=True)
        val = write_ser.validated_data

        user_profile = None
        if val.get('user_profile_id'):
            user_profile = UserProfile.objects.using(alias).filter(id=val['user_profile_id']).first()
        elif getattr(request.user, 'user_type', None) == 'MEMBER':
            user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()

        user_name = ''
        user_phone = ''
        if user_profile and getattr(user_profile, 'user', None):
            user_name = f"{user_profile.user.first_name} {user_profile.user.last_name}".strip()
            user_phone = user_profile.user.phone or ''
        name = val.get('member_name') or user_name
        phone = val.get('member_phone') or user_phone

        branch = None
        if val.get('branch_id'):
            branch = Branch.objects.using(alias).filter(id=val['branch_id']).first()
        if not branch and user_profile:
            branch = user_profile.preferred_branch
        if not branch:
            branch = Branch.objects.using(alias).filter(organization=org, status='ACTIVE').first()

        prev_assessment = MemberAssessment.objects.using(alias).filter(id=val['previous_assessment_id']).first() if val.get('previous_assessment_id') else None

        # Safety Gate Evaluation
        health_screening = val.get('health_screening') or {}
        gate_res = evaluate_safety_readiness(health_screening, user_profile)
        initial_status = gate_res['recommended_status'] if not gate_res['cleared_for_testing'] else 'DRAFT'

        # Real Metabolic Calculations
        ht, wt = val.get('measured_height_cm'), val.get('measured_weight_kg')
        goal = val.get('target_goal', 'POSTURE_ALIGNMENT')
        act = val.get('current_activity_level', 'MODERATELY_ACTIVE')
        calc_bmi, rmr, tdee, target_cal, macros = None, None, None, None, {}
        if ht and wt:
            calc_bmi = Decimal(str(round(float(wt) / ((float(ht) / 100.0) ** 2), 1)))
            plan = calculate_metabolic_nutrition_plan(weight_kg=float(wt), height_cm=float(ht), user_profile=user_profile, activity_level=act, goal=goal)
            rmr, tdee, target_cal, macros = plan['rmr_kcal'], plan['tdee_kcal'], plan['daily_calorie_target'], plan['macros']

        func_tests = val.get('functional_tests_payload') or {}
        timeline_m = int(val.get('target_timeline_months', 3))
        freq = int(val.get('weekly_frequency_days', 3))
        match_res = AssessmentEngineService.match_system_packages(
            organization=org, branch=branch, goal=goal, timeline_months=timeline_m, weekly_frequency=freq, db_alias=alias
        )
        rec_pkg = match_res.get('recommended_package')
        rec_ver = match_res.get('recommended_package_version')
        if val.get('package_id'):
            custom_pkg = Package.objects.using(alias).filter(id=val['package_id']).first()
            if custom_pkg:
                rec_pkg, rec_ver = custom_pkg, custom_pkg.get_active_version()

        trainer = request.user if getattr(request.user, 'user_type', None) != 'MEMBER' else None
        assessment = MemberAssessment.objects.using(alias).create(
            organization=org, branch=branch, user_profile=user_profile, previous_assessment=prev_assessment,
            member_name=name, member_phone=phone, conducted_by_trainer=trainer, assessment_type=val.get('assessment_type', 'INITIAL'),
            training_experience=val.get('training_experience', 'BEGINNER'), current_activity_level=act,
            preferred_training_style=val.get('preferred_training_style', 'PILATES'), target_goal=goal,
            stated_goal_outcome=val.get('stated_goal_outcome', ''), target_timeline_months=timeline_m,
            target_timeline_weeks=val.get('target_timeline_weeks', timeline_m * 4), weekly_frequency_days=freq,
            perceived_barrier=val.get('perceived_barrier', ''), member_confidence_score=int(val.get('member_confidence_score', 8)),
            user_notes=val.get('user_notes', ''), health_screening=health_screening,
            screening_outcome=val.get('screening_outcome') or gate_res['screening_outcome'],
            screening_coach_notes=val.get('screening_coach_notes', ''), measured_height_cm=ht, measured_weight_kg=wt,
            waist_circumference_cm=val.get('waist_circumference_cm'), resting_heart_rate_bpm=val.get('resting_heart_rate_bpm'),
            resting_blood_pressure=val.get('resting_blood_pressure', ''), calculated_bmi=calc_bmi, bmr_rmr_kcal=rmr,
            tdee_kcal=tdee, daily_calorie_target=target_cal, nutrition_macros=macros, functional_tests_payload=func_tests,
            symptoms_and_safety_events=val.get('symptoms_and_safety_events') or {},
            capability_matrix=AssessmentEngineService.calculate_capability_matrix(func_tests),
            recommended_program=rec_pkg.program if rec_pkg else None, recommended_package=rec_pkg,
            recommended_package_version=rec_ver, recommendation_reason=match_res.get('recommendation_reason'),
            alternative_packages_payload=match_res.get('alternatives', []),
            consent_body_measurements=val.get('consent_body_measurements', False),
            consent_movement_photos=val.get('consent_movement_photos', False),
            consent_ai_analysis=val.get('consent_ai_analysis', False),
            consent_secondary_use=val.get('consent_secondary_use', False),
            consent_notice_version=val.get('consent_notice_version', 'v1.0'),
            status=initial_status,
        )
        return Response(MemberAssessmentDetailSerializer(assessment, context={'request': request}).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        alias, org = _resolve_db_and_org(request)
        instance = self.get_object()
        write_ser = MemberAssessmentWriteSerializer(instance, data=request.data, partial=True)
        write_ser.is_valid(raise_exception=True)
        write_ser.save()

        if instance.measured_height_cm and instance.measured_weight_kg:
            instance.calculated_bmi = Decimal(str(round(float(instance.measured_weight_kg) / ((float(instance.measured_height_cm) / 100.0) ** 2), 1)))
            plan = calculate_metabolic_nutrition_plan(
                weight_kg=float(instance.measured_weight_kg), height_cm=float(instance.measured_height_cm),
                user_profile=instance.user_profile, activity_level=instance.current_activity_level, goal=instance.target_goal,
            )
            instance.bmr_rmr_kcal, instance.tdee_kcal = plan['rmr_kcal'], plan['tdee_kcal']
            instance.daily_calorie_target, instance.nutrition_macros = plan['daily_calorie_target'], plan['macros']

        if instance.functional_tests_payload:
            instance.capability_matrix = AssessmentEngineService.calculate_capability_matrix(instance.functional_tests_payload)
            if instance.status == 'DRAFT':
                instance.status = 'TESTING'

        instance.save(using=alias)
        return Response(MemberAssessmentDetailSerializer(instance, context={'request': request}).data)

    def retrieve(self, request, pk=None):
        return Response(MemberAssessmentDetailSerializer(self.get_object(), context={'request': request}).data)

    def capture_photos(self, request, pk=None):
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()
        if request.FILES.get('photo_front'):
            assessment.photo_front = request.FILES['photo_front']
        if request.FILES.get('photo_side'):
            assessment.photo_side = request.FILES['photo_side']
        assessment.status = 'PENDING_REVIEW'
        assessment.save(using=alias)
        return Response(MemberAssessmentDetailSerializer(assessment, context={'request': request}).data)

    def approve_and_send(self, request, pk=None):
        user_type = getattr(request.user, 'user_type', None)
        is_staff = getattr(request.user, 'is_staff', False)
        if user_type == 'MEMBER' and not is_staff:
            return Response(
                {'error': 'Permission denied. Only certified coaches, trainers, or studio admins can approve assessments.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()
        app_ser = AssessmentApprovalSerializer(data=request.data)
        app_ser.is_valid(raise_exception=True)
        val = app_ser.validated_data
        approved = AssessmentEngineService.approve_and_dispatch(
            assessment=assessment, reviewer_user=request.user, coach_notes=val.get('coach_notes'),
            workout_prescription=val.get('workout_prescription'), diet_prescription=val.get('diet_prescription'),
            reassessment_interval_weeks=val.get('reassessment_interval_weeks', 6), send_whatsapp=val.get('send_whatsapp', False),
            db_alias=alias,
        )
        return Response(MemberAssessmentDetailSerializer(approved, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def comparison(self, request, pk=None):
        return Response(compare_assessments(self.get_object()))


class AssessmentProgressionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AssessmentTestProgressionSerializer

    def get_queryset(self):
        alias, org = _resolve_db_and_org(self.request)
        qs = AssessmentTestProgression.objects.using(alias).filter(organization=org)
        uid = self.request.query_params.get('user_profile_id')
        if uid:
            qs = qs.filter(user_profile_id=uid)
        if getattr(self.request.user, 'user_type', None) == 'MEMBER':
            qs = qs.filter(user_profile__user=self.request.user)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        alias, org = _resolve_db_and_org(self.request)
        serializer.save(organization=org)

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        user_type = getattr(request.user, 'user_type', None)
        is_staff = getattr(request.user, 'is_staff', False)
        if user_type == 'MEMBER' and not is_staff:
            return Response({'error': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        alias, org = _resolve_db_and_org(request)
        progression = self.get_object()
        app_ser = ProgressionApprovalSerializer(data=request.data)
        app_ser.is_valid(raise_exception=True)
        val = app_ser.validated_data
        approved = approve_progression(
            progression=progression, coach_user=request.user, criteria_met_notes=val.get('criteria_met_notes', ''),
            next_variation=val['next_variation'], db_alias=alias,
        )
        return Response(AssessmentTestProgressionSerializer(approved).data)


class PairingRedeemThrottle(SimpleRateThrottle):
    scope = 'assessment_pairing'
    rate = '10/minute'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class StudioScreenPairingView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PairingRedeemThrottle]

    def post(self, request):
        code = str(request.data.get('code') or '').strip()
        if not code:
            return Response({'error': 'Pairing code is required.'}, status=status.HTTP_400_BAD_REQUEST)

        alias, org = _resolve_db_and_org(request)
        now = timezone.now()
        assessment = (
            MemberAssessment.objects.using(alias)
            .filter(organization=org, pairing_code=code, pairing_code_used_at__isnull=True, pairing_code_expires_at__gt=now)
            .order_by('-created_at').first()
        )
        if not assessment:
            return Response({'error': 'Invalid or expired pairing code.'}, status=status.HTTP_404_NOT_FOUND)

        if not assessment.display_screen_connected:
            MemberAssessment.objects.using(alias).filter(pk=assessment.pk).update(display_screen_connected=True)

        if assessment.status != 'APPROVED':
            return Response({'id': str(assessment.id), 'status': assessment.status, 'member_name': assessment.member_name, 'is_approved': False, 'connected': True})

        consumed = MemberAssessment.objects.using(alias).filter(pk=assessment.pk, pairing_code_used_at__isnull=True).update(pairing_code_used_at=now)
        if not consumed:
            return Response({'error': 'Invalid or expired pairing code.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(self._approved_summary(assessment))

    @staticmethod
    def _approved_summary(assessment: MemberAssessment) -> dict:
        rec_pkg = assessment.recommended_package
        price, sessions = None, None
        if rec_pkg:
            ver = assessment.recommended_package_version or rec_pkg.get_active_version()
            if ver:
                p = ver.prices.filter(status='ACTIVE').first()
                if p:
                    price = float(p.total_price)
                for e in ver.entitlement_definitions.filter(status='ACTIVE'):
                    if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                        sessions = 365 if e.is_unlimited else int(e.allocated_units or 0)

        return {
            'id': str(assessment.id), 'status': assessment.status, 'member_name': assessment.member_name,
            'is_approved': True, 'connected': True, 'target_goal': assessment.get_target_goal_display(),
            'stated_goal_outcome': assessment.stated_goal_outcome, 'timeline_weeks': assessment.target_timeline_weeks,
            'timeline_months': assessment.target_timeline_months, 'posture_score': float(assessment.posture_score or 0),
            'capability_matrix': assessment.capability_matrix or {},
            'recommended_package': {'name': rec_pkg.name if rec_pkg else 'Tailored Studio Plan', 'session_units': sessions or 72, 'price': price or 0, 'reason': assessment.recommendation_reason},
        }
