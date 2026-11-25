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

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from django.conf import settings as djsettings
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.pagination import PageNumberPagination
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
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .services_assessment import AssessmentEngineService
from .services_assessment_screening import evaluate_safety_readiness
from .services_assessment_metrics import calculate_metabolic_nutrition_plan
from .services_assessment_progress import compare_assessments, approve_progression


logger = logging.getLogger(__name__)


def _resolve_db_and_org(request):
    alias = get_tenant_db_alias() or 'default'
    return alias, Organization.objects.using(alias).first()


def _image_url(field) -> Optional[str]:
    """Returns a stored image's URL, or None when the field is empty or unreadable."""
    if not field:
        return None
    try:
        return field.url
    except Exception:
        return None


def _derive_age(dob) -> Optional[int]:
    """Whole years between a date of birth and today."""
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _scan_provider_ready() -> bool:
    """True when the configured body-scan provider can actually run on this server."""
    try:
        from .services_body_scan import get_scan_client
        return bool(get_scan_client().is_configured)
    except Exception:
        logger.warning("Could not determine body scan provider readiness.", exc_info=True)
        return False


class AssessmentEligibilityMobileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, org = _resolve_db_and_org(request)
        profile = UserProfile.objects.using(alias).filter(user=request.user).first()
        if not profile:
            return Response({'eligible': True, 'reason': 'Assessment unlocked for studio trial.', 'remaining_credits': 1})
        branch_id = request.query_params.get('branch_id')
        return Response(AssessmentEngineService.check_member_eligibility(user_profile=profile, branch_id=branch_id, db_alias=alias))


class AssessmentPagination(PageNumberPagination):
    """Server-side paging for the assessment register."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200


class AssessmentSessionViewSet(viewsets.ModelViewSet):
    # Assessments hold health screening answers, body measurements and photographs, and the
    # register makes them enumerable, so access is tenant-scoped and branch-scoped.
    #
    # NOT yet enforced by TenantRBACPermission: the finer-grained codes below exist in
    # rbac_defaults but are not provisioned to any role in a live tenant — verified against
    # this database, where not even performance.assessments.view is attached to a role.
    # Turning the engine on now would deny every trainer and front-desk user and leave only
    # org admins (who pass via the super-admin bypass) able to run an assessment.
    #
    # Enforcement is behind ASSESSMENT_RBAC_ENFORCED, which defaults to off. A tenant that
    # has not yet had these codes granted would otherwise deny every trainer and front-desk
    # user the moment this deployed, leaving only org admins via the super-admin bypass.
    #
    # To turn it on for a tenant:
    #     python manage.py sync_tenant_catalog --tenant=<slug>
    #     python manage.py provision_assessment_rbac --tenant=<slug>
    # confirm a trainer can still open and save an assessment, then set
    # ASSESSMENT_RBAC_ENFORCED=True.
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if getattr(djsettings, 'ASSESSMENT_RBAC_ENFORCED', False):
            return [IsAuthenticated(), TenantRBACPermission()]
        return [IsAuthenticated()]
    required_module = 'performance'
    required_submodule = 'assessments'
    action_permission_map = {
        'list': 'performance.assessments.view',
        'retrieve': 'performance.assessments.view',
        'scan_status_view': 'performance.assessments.view',
        'comparison': 'performance.assessments.view',
        'test_library': 'performance.assessments.view',
        'transformation_status_view': 'performance.assessments.view',
        'transformation': 'performance.assessments.edit',
        'create': 'performance.assessments.create',
        'update': 'performance.assessments.edit',
        'partial_update': 'performance.assessments.edit',
        'capture_photos': 'performance.assessments.edit',
        'rescan': 'performance.assessments.edit',
        'approve_and_send': 'performance.assessments.approve',
    }
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    serializer_class = MemberAssessmentDetailSerializer
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

        Split out from `get_queryset` so the filter-chip counts can be computed under the
        same visibility rules with the status filters lifted, without mutating the request.
        """
        alias, org = _resolve_db_and_org(self.request)
        qs = MemberAssessment.objects.using(alias).filter(organization=org).select_related(
            'branch', 'user_profile', 'conducted_by_trainer', 'recommended_package',
        )

        user = self.request.user
        if getattr(user, 'user_type', None) == 'MEMBER':
            # A member only ever sees their own approved assessments.
            return qs.filter(user_profile__user=user, status='APPROVED').order_by('-created_at')

        # Branch scope: honour the staff member's permitted branches before any explicit filter.
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
                qs = qs.filter(status__in=[v.strip() for v in status_param.split(',') if v.strip()])

        scan_status = (params.get('scan_status') or '').strip().upper()
        if scan_status and scan_status not in ('ALL', 'ANY'):
            qs = qs.filter(scan_status__in=[v.strip() for v in scan_status.split(',') if v.strip()])

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
        retrieve, so listing a thousand assessments does not mean a thousand detail builds.
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
        """Counts for the register's filter chips, under the same visibility rules."""
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
            'previous_assessment_id': str(instance.previous_assessment_id) if instance.previous_assessment_id else None,
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
            'reassessment_due_date': instance.reassessment_due_date.isoformat() if instance.reassessment_due_date else None,
            'created_at': instance.created_at.isoformat() if instance.created_at else None,
            'updated_at': instance.updated_at.isoformat() if instance.updated_at else None,
        }

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

        # Section 1 demographics. When the coach picked a member from the directory, these
        # come from that profile rather than from the request, because the profile is the
        # authoritative record. Mifflin-St Jeor takes both, so a wrong value here moves the
        # calorie target by hundreds of kilocalories.
        dob = getattr(user_profile, 'date_of_birth', None) if user_profile else None
        if not dob and val.get('member_date_of_birth'):
            dob = val['member_date_of_birth']
        sex = (getattr(user_profile, 'gender', '') if user_profile else '') or val.get('member_sex') or ''
        sex = str(sex).strip().upper()
        if sex in ('F', 'WOMAN'):
            sex = 'FEMALE'
        elif sex in ('M', 'MAN'):
            sex = 'MALE'
        if sex not in ('FEMALE', 'MALE', 'OTHER', 'UNDISCLOSED'):
            sex = ''
        age = _derive_age(dob) if dob else None

        assessment = MemberAssessment.objects.using(alias).create(
            organization=org, branch=branch, user_profile=user_profile, previous_assessment=prev_assessment,
            member_date_of_birth=dob or None, member_age_years=age, member_sex=sex,
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

        # Consent gate. Movement photography and automated analysis are separate, opt-in
        # consents, so a member who declined gets their assessment without a scan rather than
        # having their images analysed anyway.
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

        body = MemberAssessmentDetailSerializer(assessment, context={'request': request}).data
        body['scan'] = self._serialize_scan(assessment)
        body['scan']['queued'] = queued
        body['views_received'] = received
        body['photos'] = self._photo_urls(assessment)
        return Response(body, status=status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK)

    @staticmethod
    def _photo_urls(assessment) -> dict:
        """The four capture URLs. A view with no image is null, never a stand-in."""
        return {
            'front': _image_url(assessment.photo_front) or assessment.photo_front_url,
            'back': _image_url(assessment.photo_back) or assessment.photo_back_url,
            'left': _image_url(assessment.photo_side_left) or assessment.photo_side_left_url,
            'right': (_image_url(assessment.photo_side_right) or _image_url(assessment.photo_side)
                      or assessment.photo_side_right_url or assessment.photo_side_url),
        }

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

    def transformation(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/transformation/

        Builds the milestone previews from the member's own baseline capture.

        Refuses outright without `consent_image_generation`: a simulated image of someone's
        future body is a separate act from analysing their posture. Each rendered milestone
        is verified against the baseline pose and discarded if the subject moved, so a
        rejected milestone is reported rather than shown with a caveat.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        from .services_transformation import (
            TransformationError, TransformationNotPermitted, TransformationPipeline, get_renderer,
        )

        renderer = get_renderer(request.data.get('renderer'))
        try:
            result = TransformationPipeline.build(assessment, renderer=renderer, db_alias=alias)
        except TransformationNotPermitted as exc:
            assessment.transformation_status = 'REFUSED_NO_CONSENT'
            assessment.transformation_payload = {'error': str(exc)}
            assessment.save(using=alias, update_fields=[
                'transformation_status', 'transformation_payload', 'updated_at'])
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except TransformationError as exc:
            assessment.transformation_status = 'FAILED'
            assessment.transformation_payload = {'error': str(exc)}
            assessment.save(using=alias, update_fields=[
                'transformation_status', 'transformation_payload', 'updated_at'])
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        accepted = result.get('accepted_count', 0)
        if accepted == 0:
            assessment.transformation_status = 'FAILED'
        elif result.get('rejected'):
            assessment.transformation_status = 'PARTIAL'
        else:
            assessment.transformation_status = 'COMPLETED'

        assessment.transformation_payload = result
        assessment.transformation_generated_at = timezone.now()
        assessment.save(using=alias)

        return Response({
            'status': assessment.transformation_status,
            **result,
        }, status=status.HTTP_200_OK)

    def transformation_status_view(self, request, pk=None):
        """GET /api/v1/tenant/assessments/<id>/transformation/ — what was produced, and how."""
        assessment = self.get_object()
        return Response({
            'status': assessment.transformation_status,
            'generated_at': assessment.transformation_generated_at.isoformat()
                            if assessment.transformation_generated_at else None,
            **(assessment.transformation_payload or {}),
        }, status=status.HTTP_200_OK)

    def test_library(self, request):
        """
        GET /api/v1/tenant/assessments/test-library/

        The assessment catalogue: every test the battery can run, its fields and the current
        protocol version. Served rather than duplicated in the client so both sides agree on
        which protocol a stored result was recorded under.
        """
        from .assessment_test_library import library_payload
        return Response(library_payload(), status=status.HTTP_200_OK)

    def scan_status_view(self, request, pk=None):
        """
        GET /api/v1/tenant/assessments/<id>/scan/

        Poll target while the analysis runs, and the record of what it concluded afterwards.
        """
        return Response(self._serialize_scan(self.get_object()), status=status.HTTP_200_OK)

    def rescan(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/rescan/

        Re-runs the analysis against the captures already on file, for when a transient
        provider failure is the only thing between the coach and a result.
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
            member_acknowledgement_name=val.get('member_acknowledgement_name'),
            coach_sign_off_name=val.get('coach_sign_off_name'),
            agreed_first_milestone=val.get('agreed_first_milestone'),
            goal_review_date=val.get('goal_review_date'),
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
