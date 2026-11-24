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

import logging
import uuid
from datetime import date
from decimal import Decimal
from django.conf import settings as djsettings
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q
from typing import Optional

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from .models_assessment import MemberAssessment
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import UserProfile, Lead
from .models_catalog import Package
from .services_assessment import AssessmentEngineService
from .services_reliability import record_business_audit

logger = logging.getLogger(__name__)


def _image_url(field) -> Optional[str]:
    """Returns a stored image's URL, or None when the field is empty or unreadable."""
    if not field:
        return None
    try:
        return field.url
    except Exception:
        return None


def _scan_provider_ready() -> bool:
    """True when the configured scan provider can actually run on this server."""
    try:
        from .services_body_scan import get_scan_client
        return bool(get_scan_client().is_configured)
    except Exception:
        logger.warning("Could not determine body scan provider readiness.", exc_info=True)
        return False


def _derive_age(dob) -> Optional[int]:
    """Whole years between a date of birth and today."""
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


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


class AssessmentPagination(PageNumberPagination):
    """Server-side paging for the assessment register."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200


class AssessmentSessionViewSet(viewsets.ModelViewSet):
    """
    CRUD and operational workflow actions for Functional Fitness Assessments.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = AssessmentPagination

    # Status groupings the register filters by. `needs_review` is the approval inbox: the
    # queue a coach or admin has to clear, which has no other entry point in the product.
    OPEN_STATUSES = ('DRAFT', 'TESTING')
    REVIEW_STATUSES = ('PENDING_REVIEW',)

    def get_queryset(self):
        return self._filtered_queryset(self.request.query_params)

    def _filtered_queryset(self, params, apply_status_filters: bool = True):
        """
        The register queryset.

        Split out from `get_queryset` so the filter-chip counts can be computed against the
        same visibility rules with the status filters lifted, without mutating the request.
        """
        alias, org = _resolve_db_and_org(self.request)
        qs = MemberAssessment.objects.using(alias).filter(organization=org).select_related(
            'branch', 'user_profile', 'conducted_by_trainer', 'recommended_package',
        )

        user = self.request.user
        if hasattr(user, 'user_type') and user.user_type == 'MEMBER':
            # A member only ever sees their own assessments.
            return qs.filter(user_profile__user=user).order_by('-created_at')

        # Branch scope: honour the staff member's permitted branches, then any explicit filter.
        try:
            from .views_crm import get_user_effective_branch_ids
            permitted = get_user_effective_branch_ids(user, alias)
            if permitted is not None:
                qs = qs.filter(branch_id__in=list(permitted))
        except Exception:
            logger.warning("Could not resolve permitted branches for the assessment register.", exc_info=True)

        branch_id = (params.get('branch_id') or params.get('branch') or '').strip()
        if branch_id and branch_id.lower() not in ('all', 'any'):
            qs = qs.filter(branch_id=branch_id)

        if apply_status_filters:
            bucket = (params.get('bucket') or '').strip().lower()
            if bucket == 'needs_review':
                qs = qs.filter(status__in=self.REVIEW_STATUSES)
            elif bucket == 'in_progress':
                qs = qs.filter(status__in=self.OPEN_STATUSES)
            elif bucket == 'approved':
                qs = qs.filter(status='APPROVED')

            status_param = (params.get('status') or '').strip().upper()
            if status_param and status_param not in ('ALL', 'ANY'):
                qs = qs.filter(status__in=[s.strip() for s in status_param.split(',') if s.strip()])

        scan_status = (params.get('scan_status') or '').strip().upper()
        if scan_status and scan_status not in ('ALL', 'ANY'):
            qs = qs.filter(scan_status__in=[s.strip() for s in scan_status.split(',') if s.strip()])

        assessment_type = (params.get('assessment_type') or '').strip().upper()
        if assessment_type and assessment_type not in ('ALL', 'ANY'):
            qs = qs.filter(assessment_type=assessment_type)

        trainer_id = (params.get('conducted_by') or params.get('trainer_id') or '').strip()
        if trainer_id:
            qs = qs.filter(conducted_by_trainer_id=trainer_id)

        date_from = (params.get('date_from') or params.get('start_date') or '').strip()
        date_to = (params.get('date_to') or params.get('end_date') or '').strip()
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        search = (params.get('search') or params.get('q') or '').strip()
        if search:
            qs = qs.filter(
                Q(member_name__icontains=search)
                | Q(member_phone__icontains=search)
                | Q(user_profile__first_name_snapshot__icontains=search)
                | Q(user_profile__last_name_snapshot__icontains=search)
                | Q(user_profile__member_number__icontains=search)
            )

        ordering = (params.get('ordering') or '-created_at').strip()
        allowed_ordering = {
            'created_at', '-created_at', 'updated_at', '-updated_at',
            'member_name', '-member_name', 'status', '-status',
        }
        if ordering not in allowed_ordering:
            ordering = '-created_at'

        return qs.order_by(ordering, '-id')

    def list(self, request, *args, **kwargs):
        """
        GET /api/v1/tenant/assessments/

        The assessment register: newest first, paged server-side, with search and filters.
        Rows are deliberately compact — the full seventeen-section payload is only built on
        retrieve, so listing a thousand assessments does not mean a thousand SLA computations.
        """
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        rows = [self._serialize_row(item) for item in (page if page is not None else queryset[:200])]

        if page is None:
            return Response({'count': len(rows), 'results': rows, 'summary': self._register_summary(request)})

        response = self.get_paginated_response(rows)
        if isinstance(response.data, dict):
            response.data['summary'] = self._register_summary(request)
        return response

    def _register_summary(self, request) -> dict:
        """Counts for the register's filter chips, computed under the same visibility rules."""
        try:
            scoped = self._filtered_queryset(request.query_params, apply_status_filters=False)
            return {
                'total': scoped.count(),
                'in_progress': scoped.filter(status__in=self.OPEN_STATUSES).count(),
                'needs_review': scoped.filter(status__in=self.REVIEW_STATUSES).count(),
                'approved': scoped.filter(status='APPROVED').count(),
                'scan_failed': scoped.filter(scan_status__in=('FAILED', 'PARTIAL')).count(),
            }
        except Exception:
            logger.warning("Assessment register summary failed.", exc_info=True)
            return {}

    def _serialize_row(self, instance: MemberAssessment) -> dict:
        """One register row. Compact by design; `retrieve` returns the whole assessment."""
        from .security_utils import mask_phone_if_restricted

        member_name = instance.member_name
        if not member_name and instance.user_profile:
            member_name = (
                f"{getattr(instance.user_profile, 'first_name_snapshot', '') or ''} "
                f"{getattr(instance.user_profile, 'last_name_snapshot', '') or ''}"
            ).strip()

        trainer = instance.conducted_by_trainer
        trainer_name = None
        if trainer:
            trainer_name = f"{trainer.first_name or ''} {trainer.last_name or ''}".strip() or trainer.email

        captured = sum(1 for f in (
            instance.photo_front, instance.photo_back,
            instance.photo_side_left, instance.photo_side_right or instance.photo_side,
        ) if f)

        return {
            'id': str(instance.id),
            'member_name': member_name or 'Guest',
            'member_phone': mask_phone_if_restricted(getattr(self, 'request', None), instance.member_phone),
            'member_id': str(instance.user_profile_id) if instance.user_profile_id else None,
            'lead_id': str(instance.lead_id) if instance.lead_id else None,
            'assessment_type': instance.assessment_type,
            'assessment_type_display': instance.get_assessment_type_display(),
            'status': instance.status,
            'status_display': instance.get_status_display(),
            'screening_outcome': instance.screening_outcome,
            'scan_status': instance.scan_status,
            'captures_taken': captured,
            'posture_score': float(instance.posture_score) if instance.posture_score is not None else None,
            'branch': {'id': str(instance.branch_id), 'name': instance.branch.name} if instance.branch_id else None,
            'conducted_by': trainer_name,
            'recommended_package': instance.recommended_package.name if instance.recommended_package else None,
            'reviewed_at': instance.reviewed_at.isoformat() if instance.reviewed_at else None,
            'created_at': instance.created_at.isoformat() if instance.created_at else None,
            'updated_at': instance.updated_at.isoformat() if instance.updated_at else None,
        }

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

        # 3b. Demographics. Mifflin-St Jeor takes age and sex; without them the equation
        # silently returns someone else's resting metabolic rate, so resolve them from the
        # payload first, then the member profile, then the lead.
        dob = data.get('member_date_of_birth') or data.get('date_of_birth')
        if not dob and user_profile:
            dob = getattr(user_profile, 'date_of_birth', None)
        if isinstance(dob, str):
            try:
                dob = date.fromisoformat(dob.strip()[:10])
            except ValueError:
                dob = None

        member_sex = (data.get('member_sex') or data.get('sex') or data.get('gender') or '').strip().upper()
        if not member_sex and user_profile:
            member_sex = (getattr(user_profile, 'gender', '') or '').strip().upper()
        if member_sex in ('F', 'WOMAN', 'W'):
            member_sex = 'FEMALE'
        elif member_sex in ('M', 'MAN'):
            member_sex = 'MALE'
        if member_sex not in ('FEMALE', 'MALE', 'OTHER', 'UNDISCLOSED'):
            member_sex = ''

        member_age = data.get('member_age_years') or _derive_age(dob)
        try:
            member_age = int(member_age) if member_age else None
        except (TypeError, ValueError):
            member_age = None

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
                age=member_age,
                sex=member_sex,
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
            member_date_of_birth=dob or None,
            member_age_years=member_age,
            member_sex=member_sex,
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
        if 'member_date_of_birth' in data and data['member_date_of_birth']:
            try:
                instance.member_date_of_birth = date.fromisoformat(str(data['member_date_of_birth']).strip()[:10])
                instance.member_age_years = _derive_age(instance.member_date_of_birth)
            except ValueError:
                pass
        if 'member_age_years' in data and data['member_age_years']:
            try:
                instance.member_age_years = int(data['member_age_years'])
            except (TypeError, ValueError):
                pass
        if 'member_sex' in data and data['member_sex']:
            candidate = str(data['member_sex']).strip().upper()
            if candidate in ('FEMALE', 'MALE', 'OTHER', 'UNDISCLOSED'):
                instance.member_sex = candidate

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
                age=instance.member_age_years or _derive_age(instance.member_date_of_birth),
                sex=instance.member_sex,
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

    # Field name on the request -> model attribute. The studio captures four standardised
    # views; `photo_side` stays accepted so older clients keep working, landing on the right
    # profile rather than being silently dropped.
    CAPTURE_FIELDS = {
        'photo_front': 'photo_front',
        'photo_back': 'photo_back',
        'photo_side_left': 'photo_side_left',
        'photo_side_right': 'photo_side_right',
        'photo_side': 'photo_side_right',
    }

    def capture_photos(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/capture/

        Stores the four standardised baseline captures (front, back, left profile, right
        profile) and queues the pose analysis. No measurement is produced here: the response
        reports that the scan is queued, and the client polls `/scan/` for the result.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        received = []
        for field_name, attr in self.CAPTURE_FIELDS.items():
            uploaded = request.FILES.get(field_name)
            if uploaded:
                setattr(assessment, attr, uploaded)
                if attr not in received:
                    received.append(attr)

        if not received:
            return Response(
                {'error': 'At least one capture is required. Expected any of: photo_front, photo_back, photo_side_left, photo_side_right.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Consent gate. The specification treats movement photography as a separate, optional
        # consent, so a member who declined it gets their assessment without a scan rather
        # than having their images analysed anyway.
        consent_param = request.data.get('consent_movement_photos')
        if consent_param is not None:
            assessment.consent_movement_photos = str(consent_param).lower() in ('true', '1', 'yes')
        consent_ai_param = request.data.get('consent_ai_analysis')
        if consent_ai_param is not None:
            assessment.consent_ai_analysis = str(consent_ai_param).lower() in ('true', '1', 'yes')

        # Capture conditions the camera cannot infer, recorded by the coach so a later reader
        # knows how comparable two assessments are.
        conditions = {}
        for key in ('clothing_fit', 'capture_mode', 'lighting', 'footwear', 'camera_distance_m'):
            value = request.data.get(key)
            if value not in (None, ''):
                conditions[key] = value
        if conditions:
            payload = dict(assessment.cv_analysis_payload or {})
            payload['capture_conditions'] = conditions
            assessment.cv_analysis_payload = payload

        scan_requested = bool(assessment.consent_movement_photos and assessment.consent_ai_analysis)
        if not scan_requested:
            assessment.scan_status = 'SKIPPED_NO_CONSENT'
            assessment.scan_error = 'The member did not consent to photographic / AI movement analysis.'
        elif not getattr(djsettings, 'BODY_SCAN_ENABLED', True):
            assessment.scan_status = 'UNAVAILABLE'
            assessment.scan_error = 'Body scanning is switched off on this server.'
        elif not _scan_provider_ready():
            assessment.scan_status = 'UNAVAILABLE'
            assessment.scan_error = (
                'Body scanning is not configured on this server. The captures were stored and no '
                'measurements were produced.'
            )
        else:
            assessment.scan_status = 'QUEUED'
            assessment.scan_error = None
            assessment.scan_started_at = timezone.now()
            assessment.scan_completed_at = None

        if assessment.status == 'DRAFT':
            assessment.status = 'TESTING'

        assessment.save(using=alias)

        queued = False
        if assessment.scan_status == 'QUEUED':
            queued = self._enqueue_scan(request, assessment, alias)

        body = self._serialize_assessment(assessment)
        body['scan'] = self._serialize_scan(assessment)
        body['scan']['queued'] = queued
        body['views_received'] = received
        return Response(body, status=status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK)

    def _enqueue_scan(self, request, assessment, alias: str) -> bool:
        """
        Hands the scan to Celery, falling back to running it inline when no broker is
        reachable so a single-process deployment still produces results.
        """
        tenant_id = getattr(getattr(request, 'tenant', None), 'id', None) or getattr(request, 'tenant_id', None)
        if tenant_id:
            try:
                from .tasks_body_scan import run_assessment_body_scan
                run_assessment_body_scan.delay(tenant_id=str(tenant_id), assessment_id=str(assessment.id))
                return True
            except Exception:
                logger.warning(
                    "Could not queue the body scan for assessment %s; running it inline.",
                    assessment.id, exc_info=True,
                )
        else:
            logger.info(
                "No tenant id on the request for assessment %s; running the body scan inline.",
                assessment.id,
            )

        try:
            from .services_body_scan import BodyScanService, get_scan_client
            assessment.scan_status = 'PROCESSING'
            assessment.save(using=alias, update_fields=['scan_status', 'updated_at'])
            result = BodyScanService.analyse(assessment, client=get_scan_client())
            BodyScanService.apply_to_assessment(assessment, result, db_alias=alias)
        except Exception as exc:
            logger.exception("Inline body scan failed for assessment %s", assessment.id)
            assessment.scan_status = 'FAILED'
            assessment.scan_error = str(exc)[:2000]
            assessment.scan_completed_at = timezone.now()
            assessment.save(using=alias, update_fields=['scan_status', 'scan_error', 'scan_completed_at', 'updated_at'])
        return False

    def scan_status_view(self, request, pk=None):
        """
        GET /api/v1/tenant/assessments/<id>/scan/

        Poll target while the analysis runs, and the record of what it concluded afterwards.
        """
        assessment = self.get_object()
        return Response(self._serialize_scan(assessment), status=status.HTTP_200_OK)

    def rescan(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/rescan/

        Re-runs the analysis against the captures already on file, for when a transient
        provider failure is the only thing standing between the coach and a result.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        if assessment.status == 'APPROVED':
            return Response(
                {'error': 'This assessment is already approved. Re-open it before re-running the scan.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not (assessment.consent_movement_photos and assessment.consent_ai_analysis):
            return Response(
                {'error': 'The member did not consent to photographic / AI movement analysis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .services_body_scan import BodyScanService
        if not BodyScanService.collect_views(assessment):
            return Response(
                {'error': 'There are no captures on file to analyse. Take the baseline photos first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assessment.scan_status = 'QUEUED'
        assessment.scan_error = None
        assessment.scan_started_at = timezone.now()
        assessment.scan_completed_at = None
        assessment.save(
            using=alias,
            update_fields=['scan_status', 'scan_error', 'scan_started_at', 'scan_completed_at', 'updated_at'],
        )

        queued = self._enqueue_scan(request, assessment, alias)
        payload = self._serialize_scan(assessment)
        payload['queued'] = queued
        return Response(payload, status=status.HTTP_202_ACCEPTED)

    def _serialize_scan(self, assessment) -> dict:
        """The scan's own view of itself: state, provenance, quality per view, and results."""
        payload = assessment.cv_analysis_payload or {}
        raw = assessment.scan_raw_payload or {}
        return {
            'assessment_id': str(assessment.id),
            'status': assessment.scan_status,
            'is_terminal': assessment.scan_status in (
                'COMPLETED', 'PARTIAL', 'FAILED', 'UNAVAILABLE', 'SKIPPED_NO_CONSENT',
            ),
            'provider': assessment.scan_provider or None,
            'model': assessment.scan_model or None,
            'error': assessment.scan_error,
            'started_at': assessment.scan_started_at.isoformat() if assessment.scan_started_at else None,
            'completed_at': assessment.scan_completed_at.isoformat() if assessment.scan_completed_at else None,
            'views_submitted': raw.get('views_submitted') or [],
            'views_usable': raw.get('views_usable') or [],
            'capture_quality': assessment.scan_capture_quality or {},
            'warnings': raw.get('warnings') or [],
            'per_view': raw.get('per_view') or {},
            'posture': {
                'posture_score': float(assessment.posture_score) if assessment.posture_score is not None else None,
                'shoulder_tilt_degrees': float(assessment.shoulder_tilt_degrees) if assessment.shoulder_tilt_degrees is not None else None,
                'hip_tilt_degrees': float(assessment.hip_tilt_degrees) if assessment.hip_tilt_degrees is not None else None,
                'forward_head_angle_degrees': float(assessment.forward_head_angle_degrees) if assessment.forward_head_angle_degrees is not None else None,
                'lateral_shift_degrees': float(assessment.lateral_shift_degrees) if assessment.lateral_shift_degrees is not None else None,
                'breakdown': payload.get('posture_score_breakdown'),
            },
            'circumference_estimates': payload.get('circumference_estimates') or {},
            'capture_conditions': payload.get('capture_conditions') or {},
            'measurement_metadata': assessment.measurement_metadata or {},
        }

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

        from .security_utils import mask_phone_if_restricted
        req = getattr(self, 'request', None)
        return {
            'id': str(instance.id),
            'pairing_code': instance.pairing_code,
            'status': instance.status,
            'member_name': instance.member_name,
            'member_phone': mask_phone_if_restricted(req, instance.member_phone),
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
                'estimated_body_fat_pct': float(instance.estimated_body_fat_pct) if instance.estimated_body_fat_pct is not None else None,
                'waist_to_hip_ratio': float(instance.waist_to_hip_ratio) if instance.waist_to_hip_ratio is not None else None,
            },

            # Sections 4-11: Movement Tests & Capability Matrix
            'functional_tests': instance.functional_tests_payload,
            'capability_matrix': instance.capability_matrix,

            # Four standardised captures. A view with no image returns null rather than a
            # stand-in, so the studio screen can show it as outstanding.
            'photos': {
                'front': _image_url(instance.photo_front) or instance.photo_front_url,
                'back': _image_url(instance.photo_back) or instance.photo_back_url,
                'left': _image_url(instance.photo_side_left) or instance.photo_side_left_url,
                'right': _image_url(instance.photo_side_right) or _image_url(instance.photo_side)
                         or instance.photo_side_right_url or instance.photo_side_url,
            },
            'photo_front_url': _image_url(instance.photo_front) or instance.photo_front_url,
            'photo_side_url': _image_url(instance.photo_side_right) or _image_url(instance.photo_side) or instance.photo_side_url,

            # Measured posture geometry. These stay null until a scan succeeds — there is no
            # placeholder value, so "not assessed" is distinguishable from "assessed as 0".
            'metrics': {
                'posture_score': float(instance.posture_score) if instance.posture_score is not None else None,
                'shoulder_tilt_degrees': float(instance.shoulder_tilt_degrees) if instance.shoulder_tilt_degrees is not None else None,
                'hip_tilt_degrees': float(instance.hip_tilt_degrees) if instance.hip_tilt_degrees is not None else None,
                'forward_head_angle_degrees': float(instance.forward_head_angle_degrees) if instance.forward_head_angle_degrees is not None else None,
                'lateral_shift_degrees': float(instance.lateral_shift_degrees) if instance.lateral_shift_degrees is not None else None,
            },
            'scan': {
                'status': instance.scan_status,
                'provider': instance.scan_provider or None,
                'model': instance.scan_model or None,
                'error': instance.scan_error,
                'views_usable': (instance.scan_raw_payload or {}).get('views_usable') or [],
                'capture_quality': instance.scan_capture_quality or {},
            },
            'estimated_measurements': {
                'waist_cm': float(instance.estimated_waist_cm) if instance.estimated_waist_cm is not None else None,
                'hip_cm': float(instance.estimated_hip_cm) if instance.estimated_hip_cm is not None else None,
                'chest_cm': float(instance.estimated_chest_cm) if instance.estimated_chest_cm is not None else None,
                'shoulder_width_cm': float(instance.estimated_shoulder_width_cm) if instance.estimated_shoulder_width_cm is not None else None,
                'disclaimer': 'Estimated from capture geometry. Tape measurements remain authoritative.',
            },
            'measurement_metadata': instance.measurement_metadata or {},

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
