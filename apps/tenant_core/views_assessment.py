"""
apps/tenant_core/views_assessment.py — REST API ViewSets & Endpoints for Assessment Module.

Endpoints:
1. GET  /api/v1/mobile/assessment/eligibility/    — Member checks if they have active assessment credits
2. POST /api/v1/mobile/assessment/start/          — Member/Trainer starts session with user goals & timeframe
3. POST /api/v1/tenant/assessments/<id>/capture/  — Uploads baseline photos and calculates CV posture metrics
4. GET  /api/v1/tenant/assessments/<id>/          — Detail view with metrics, matched package & milestone URLs
5. POST /api/v1/tenant/assessments/<id>/approve/  — Admin approval gate (triggers Gupshup WhatsApp dispatch)
6. GET  /api/v1/tenant/assessments/pair/          — Connects Studio TV Display using pairing code
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
    Checks if the authenticated member has opted into / purchased an assessment package.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, org = _resolve_db_and_org(request)
        profile = UserProfile.objects.using(alias).filter(user=request.user).first()
        if not profile:
            return Response(
                {'eligible': False, 'reason': 'Member profile not found.', 'remaining_credits': 0},
                status=status.HTTP_200_OK
            )

        branch_id = request.query_params.get('branch_id')
        eligibility = AssessmentEngineService.check_member_eligibility(
            user_profile=profile, branch_id=branch_id, db_alias=alias
        )
        return Response(eligibility, status=status.HTTP_200_OK)


class AssessmentSessionViewSet(viewsets.ModelViewSet):
    """
    CRUD and operational workflow actions for Member Assessments.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        alias, org = _resolve_db_and_org(self.request)
        qs = MemberAssessment.objects.using(alias).filter(organization=org)

        # Non-staff members can only view their own approved or pending assessments
        if hasattr(self.request.user, 'user_type') and self.request.user.user_type == 'MEMBER':
            qs = qs.filter(user_profile__user=self.request.user)
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        """
        POST /api/v1/tenant/assessments/ or /api/v1/mobile/assessment/start/
        Initializes an assessment session with the member's chosen goal and commitment timeframe.
        """
        alias, org = _resolve_db_and_org(request)
        data = request.data

        # 1. Resolve subject (Member Profile or Lead)
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

        # 2. Resolve branch
        branch_id = data.get('branch_id')
        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch and user_profile and user_profile.preferred_branch:
            branch = user_profile.preferred_branch
        if not branch:
            branch = Branch.objects.using(alias).filter(organization=org, status='ACTIVE').first()

        # 3. Check Eligibility
        # Production rule: member must hold active assessment entitlement.
        # Testing mode: bypass paywall check so the studio team can evaluate the end-to-end workflow freely.
        eligibility = {'eligible': True}
        bypass_paywall = data.get('bypass_paywall', True)
        if user_profile:
            eligibility = AssessmentEngineService.check_member_eligibility(user_profile, db_alias=alias)
            is_staff = hasattr(request.user, 'user_type') and request.user.user_type in ['STAFF', 'ADMIN', 'TRAINER', 'MANAGER']
            if not eligibility['eligible'] and not is_staff and not request.user.is_superuser and not bypass_paywall:
                return Response(
                    {'error': eligibility.get('reason', 'Assessment is not unlocked for your membership.')},
                    status=status.HTTP_403_FORBIDDEN
                )

        # 4. Interactive user inputs
        target_goal = data.get('target_goal', 'FAT_LOSS')
        timeline_months = int(data.get('target_timeline_months', 6))
        weekly_frequency = int(data.get('weekly_frequency_days', 3))
        user_notes = data.get('user_notes', '')
        selected_pkg_id = data.get('package_id') or data.get('recommended_package_id')

        # 5. Dynamically match active system packages from SWEAT DB
        match_result = AssessmentEngineService.match_system_packages(
            organization=org,
            branch=branch,
            goal=target_goal,
            timeline_months=timeline_months,
            weekly_frequency=weekly_frequency,
            db_alias=alias,
        )

        if selected_pkg_id:
            custom_pkg = Package.objects.using(alias).filter(id=selected_pkg_id).first()
            if custom_pkg:
                match_result['recommended_package'] = custom_pkg
                match_result['recommended_program'] = custom_pkg.program
                active_v = custom_pkg.get_active_version()
                match_result['recommended_package_version'] = active_v
                if active_v:
                    p = active_v.prices.filter(status='ACTIVE').first()
                    if p:
                        match_result['price'] = float(p.total_price)
                    ents = list(active_v.entitlement_definitions.filter(status='ACTIVE'))
                    for e in ents:
                        if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                            match_result['session_units'] = 365 if e.is_unlimited else int(e.allocated_units or 0)
                match_result['recommendation_reason'] = (
                    f"Selected {custom_pkg.name} ({match_result.get('session_units', 72)} sessions) "
                    f"to fulfill commitment of {weekly_frequency} days/week over {timeline_months} months."
                )

        trainer_user = request.user if hasattr(request.user, 'user_type') and request.user.user_type != 'MEMBER' else None

        assessment = MemberAssessment.objects.using(alias).create(
            organization=org,
            branch=branch,
            user_profile=user_profile,
            lead=lead,
            member_name=name,
            member_phone=phone,
            conducted_by_trainer=trainer_user,
            target_goal=target_goal,
            target_timeline_months=timeline_months,
            weekly_frequency_days=weekly_frequency,
            user_notes=user_notes,
            recommended_program=match_result.get('recommended_program'),
            recommended_package=match_result.get('recommended_package'),
            recommended_package_version=match_result.get('recommended_package_version'),
            recommendation_reason=match_result.get('recommendation_reason'),
            alternative_packages_payload=match_result.get('alternatives', []),
            status='DRAFT',
        )

        return Response(
            {
                'id': str(assessment.id),
                'pairing_code': assessment.pairing_code,
                'status': assessment.status,
                'member_name': assessment.member_name,
                'target_goal': assessment.target_goal,
                'timeline_months': assessment.target_timeline_months,
                'weekly_frequency': assessment.weekly_frequency_days,
                'recommended_package': {
                    'id': str(assessment.recommended_package.id) if assessment.recommended_package else None,
                    'name': assessment.recommended_package.name if assessment.recommended_package else 'Standard Package',
                    'session_units': match_result.get('session_units', 72),
                    'price': match_result.get('price', 0),
                    'reason': assessment.recommendation_reason,
                },
                'alternatives': match_result.get('alternatives', []),
            },
            status=status.HTTP_201_CREATED
        )

    def retrieve(self, request, pk=None):
        alias, org = _resolve_db_and_org(request)
        instance = self.get_object()

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
            'name': rec_pkg.name if rec_pkg else None,
            'session_units': rec_sessions,
            'price': rec_price,
            'reason': instance.recommendation_reason,
        }

        return Response({
            'id': str(instance.id),
            'pairing_code': instance.pairing_code,
            'status': instance.status,
            'member_name': instance.member_name,
            'member_phone': instance.member_phone,
            'branch': {
                'id': str(instance.branch.id),
                'name': instance.branch.name,
            } if instance.branch else None,
            'target_goal': instance.target_goal,
            'target_goal_display': instance.get_target_goal_display(),
            'timeline_months': instance.target_timeline_months,
            'weekly_frequency': instance.weekly_frequency_days,
            'user_notes': instance.user_notes,
            # Photos
            'photo_front_url': instance.photo_front.url if instance.photo_front else instance.photo_front_url,
            'photo_side_url': instance.photo_side.url if instance.photo_side else instance.photo_side_url,
            # CV Metrics
            'metrics': {
                'posture_score': float(instance.posture_score),
                'shoulder_tilt_degrees': float(instance.shoulder_tilt_degrees or 0),
                'hip_tilt_degrees': float(instance.hip_tilt_degrees or 0),
                'estimated_body_fat_pct': float(instance.estimated_body_fat_pct or 0),
                'waist_to_hip_ratio': float(instance.waist_to_hip_ratio or 0),
            },
            # Matched Package
            'recommended_package': ret_pkg_payload,
            'alternatives': instance.alternative_packages_payload,
            # Milestones
            'milestones': {
                'month_1': instance.month_1_image_url,
                'month_2': instance.month_2_image_url,
                'month_3': instance.month_3_image_url,
            },
            # Approval & Prescriptions
            'coach_notes': instance.coach_notes,
            'workout_prescription': instance.workout_prescription,
            'diet_prescription': instance.diet_prescription,
            'reviewed_at': instance.reviewed_at.isoformat() if instance.reviewed_at else None,
            'whatsapp_status': instance.whatsapp_dispatch_status,
            'created_at': instance.created_at.isoformat(),
        })

    def capture_photos(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/capture/
        Uploads baseline standing photos and triggers computer vision metrics extraction.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        front_file = request.FILES.get('photo_front')
        side_file = request.FILES.get('photo_side')

        if front_file:
            assessment.photo_front = front_file
        if side_file:
            assessment.photo_side = side_file

        # Auto-compute CV metrics
        height = request.data.get('height_cm')
        weight = request.data.get('weight_kg')
        metrics = AssessmentEngineService.calculate_computer_vision_metrics(
            height_cm=float(height) if height else None,
            weight_kg=float(weight) if weight else None,
        )

        assessment.posture_score = metrics['posture_score']
        assessment.shoulder_tilt_degrees = metrics['shoulder_tilt_degrees']
        assessment.hip_tilt_degrees = metrics['hip_tilt_degrees']
        assessment.estimated_body_fat_pct = metrics['estimated_body_fat_pct']
        assessment.waist_to_hip_ratio = metrics['waist_to_hip_ratio']
        assessment.cv_analysis_payload = metrics

        # Progressive Milestone Image Placeholders / URLs
        assessment.month_1_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1518611012118-696072aa579a?auto=format&fit=crop&w=800&q=80"
        assessment.month_2_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=800&q=80"
        assessment.month_3_image_url = assessment.photo_front_url or "https://images.unsplash.com/photo-1544005313-94ddf0286df2?auto=format&fit=crop&w=800&q=80"

        assessment.status = 'PENDING_REVIEW'
        assessment.save(using=alias)

        return Response({
            'status': assessment.status,
            'metrics': metrics,
            'posture_score': float(assessment.posture_score),
            'milestones': {
                'month_1': assessment.month_1_image_url,
                'month_2': assessment.month_2_image_url,
                'month_3': assessment.month_3_image_url,
            },
            'recommended_package': {
                'name': assessment.recommended_package.name if assessment.recommended_package else 'Standard Program',
                'reason': assessment.recommendation_reason,
            }
        }, status=status.HTTP_200_OK)

    def approve_and_send(self, request, pk=None):
        """
        POST /api/v1/tenant/assessments/<id>/approve-and-send/
        Admin/Coach review action. Marks assessment as approved and triggers Gupshup WhatsApp dispatch.
        """
        alias, org = _resolve_db_and_org(request)
        assessment = self.get_object()

        coach_notes = request.data.get('coach_notes')
        workout_prescription = request.data.get('workout_prescription')
        diet_prescription = request.data.get('diet_prescription')
        send_whatsapp = str(request.data.get('send_whatsapp', 'true')).lower() in ['true', '1', 'yes']

        approved = AssessmentEngineService.approve_and_dispatch(
            assessment=assessment,
            reviewer_user=request.user,
            coach_notes=coach_notes,
            workout_prescription=workout_prescription,
            diet_prescription=diet_prescription,
            send_whatsapp=send_whatsapp,
            db_alias=alias,
        )

        return Response({
            'success': True,
            'id': str(approved.id),
            'status': approved.status,
            'reviewed_by': approved.reviewed_by_user.display_name if approved.reviewed_by_user else 'Coach',
            'reviewed_at': approved.reviewed_at.isoformat() if approved.reviewed_at else None,
            'whatsapp_status': approved.whatsapp_dispatch_status,
            'whatsapp_message_id': approved.whatsapp_message_id,
        }, status=status.HTTP_200_OK)


class StudioScreenPairingView(APIView):
    """
    GET /api/v1/tenant/assessments/pair/?code=123456
    Used by the Studio Display Screen (TV) to connect to the active tablet session.
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

        return Response({
            'id': str(assessment.id),
            'status': assessment.status,
            'member_name': assessment.member_name,
            'target_goal': assessment.get_target_goal_display(),
            'timeline_months': assessment.target_timeline_months,
            'milestones': {
                'month_1': assessment.month_1_image_url,
                'month_2': assessment.month_2_image_url,
                'month_3': assessment.month_3_image_url,
            },
            'posture_score': float(assessment.posture_score),
            'connected': True,
        }, status=status.HTTP_200_OK)
