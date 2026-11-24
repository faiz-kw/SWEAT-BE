"""
apps/tenant_core/services_assessment.py — Core Business Service for Assessment & Transformation Engine.

Key Rules & Capabilities:
1. Gated Access: Only members with an active ASSESSMENT entitlement or package credit can access assessments.
2. Dynamic Catalog Recommendation: Recommends real active programs & packages from the SWEAT database.
3. Realistic Scientific Progression: Delegates metabolic & safety calculations to dedicated sub-services.
4. Manual Admin Approval Gate: Coach/Admin explicitly approves; updates sign-off timestamps and due dates.
"""

import uuid
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Dict, Any, Optional, List
from django.db import transaction, models
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_assessment import MemberAssessment, AssessmentTestProgression
from .models_catalog import Program, Package, PackageVersion, PackagePrice
from .models_memberships import Membership, MembershipEntitlement
from .models_crm import UserProfile
from .models_org import Organization, Branch
from .models_users import TenantUser
from .services_reliability import record_business_audit
from .services_assessment_screening import evaluate_safety_readiness
from .services_assessment_metrics import calculate_metabolic_nutrition_plan, calculate_bmi
from .services_assessment_progress import compare_assessments, approve_progression

logger = logging.getLogger(__name__)


def _get_alias(db_alias=None, obj=None):
    from config.routers import get_tenant_db_alias, set_tenant_db_alias
    if db_alias:
        set_tenant_db_alias(db_alias)
        return db_alias
    tenant_alias = get_tenant_db_alias()
    if tenant_alias:
        return tenant_alias
    if obj and getattr(obj, '_state', None) and getattr(obj._state, 'db', None):
        target = obj._state.db
        set_tenant_db_alias(target)
        return target
    return 'default'


class AssessmentEngineService:
    @classmethod
    def check_member_eligibility(
        cls, user_profile: UserProfile, branch_id: Optional[str] = None, db_alias: Optional[str] = None
    ) -> Dict[str, Any]:
        alias = _get_alias(db_alias, user_profile)
        now = timezone.now()

        entitlements = (
            MembershipEntitlement.objects.using(alias)
            .filter(
                membership__user_profile=user_profile,
                membership__status='ACTIVE',
                status='ACTIVE',
                entitlement_type__in=['ASSESSMENT', 'ASSESSMENT_SESSION'],
            )
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=now))
            .select_related('membership', 'membership__package')
        )
        for ent in entitlements:
            if ent.is_unlimited or (ent.allocated_units and (ent.allocated_units - ent.consumed_units) > 0):
                remaining = None if ent.is_unlimited else float(ent.allocated_units - ent.consumed_units)
                return {
                    'eligible': True,
                    'entitlement_id': str(ent.id),
                    'membership_id': str(ent.membership_id),
                    'package_name': ent.membership.package.name if ent.membership and ent.membership.package else 'Assessment Pack',
                    'remaining_credits': remaining,
                    'is_unlimited': ent.is_unlimited,
                }

        active_memberships = (
            Membership.objects.using(alias)
            .filter(user_profile=user_profile, status='ACTIVE', end_date__gte=now.date())
            .select_related('package')
        )
        for mb in active_memberships:
            pkg_name = (mb.package.name or '').lower()
            if any(term in pkg_name for term in ['transformation', 'vip', 'annual', '365', 'assessment']):
                return {
                    'eligible': True,
                    'entitlement_id': None,
                    'membership_id': str(mb.id),
                    'package_name': mb.package.name,
                    'remaining_credits': 1,
                    'is_unlimited': False,
                }

        return {
            'eligible': False,
            'reason': 'No active assessment credit found. Please purchase an assessment package.',
            'remaining_credits': 0,
        }

    @classmethod
    def match_system_packages(
        cls,
        organization: Organization,
        branch: Optional[Branch] = None,
        goal: str = 'FAT_LOSS',
        timeline_months: int = 6,
        weekly_frequency: int = 3,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        alias = _get_alias(db_alias, organization)
        now = timezone.now()
        total_estimated_sessions = int(weekly_frequency * 4.33 * timeline_months)

        packages_qs = (
            Package.objects.using(alias)
            .filter(organization=organization, status='ACTIVE')
            .select_related('program')
            .prefetch_related('versions__prices', 'versions__entitlement_definitions')
        )
        if branch:
            packages_qs = packages_qs.filter(
                models.Q(branch_availabilities__branch=branch, branch_availabilities__status='ENABLED')
                | models.Q(branch_availabilities__isnull=True)
            ).distinct()

        all_pkgs = list(packages_qs) or list(Package.objects.using(alias).filter(status='ACTIVE')[:5])
        scored = []
        for pkg in all_pkgs:
            ver = pkg.get_active_version(as_of=now)
            if not ver:
                continue
            ents = list(ver.entitlement_definitions.filter(status='ACTIVE'))
            sessions = 0
            for e in ents:
                if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                    sessions = 365 if e.is_unlimited else int(e.allocated_units or 0)
            if sessions == 0:
                import re
                m = re.search(r'\b(\d{2,3})\b', pkg.name or '')
                sessions = int(m.group(1)) if m else 0

            p = ver.prices.filter(status='ACTIVE').first()
            price = float(p.total_price) if p else 0.0
            name_low = (pkg.name or '').lower()

            score = 100
            if goal in ['POSTURE_ALIGNMENT'] and ('pilates' in name_low or '1:1' in name_low):
                score += 50
            elif goal in ['FAT_LOSS', 'STRENGTH_CONDITIONING'] and ('bootcamp' in name_low or 'hiit' in name_low):
                score += 50
            if sessions > 0:
                score -= min(abs(sessions - total_estimated_sessions), 60)

            scored.append({'package': pkg, 'ver': ver, 'sessions': sessions, 'price': price, 'score': score})

        scored.sort(key=lambda x: x['score'], reverse=True)
        if not scored:
            return {'recommended_package': None, 'recommendation_reason': 'No matching package.', 'alternatives': []}

        top = scored[0]
        alts = scored[1:4]
        goal_labels = dict(MemberAssessment.GOAL_CHOICES)
        reason = (
            f"Based on {weekly_frequency} days/week for {timeline_months} months for {goal_labels.get(goal, goal)}, "
            f"we recommend {top['package'].name} ({top['sessions']} sessions)."
        )
        return {
            'recommended_package': top['package'],
            'recommended_package_version': top['ver'],
            'recommended_program': top['package'].program,
            'session_units': top['sessions'],
            'price': top['price'],
            'recommendation_reason': reason,
            'alternatives': [{'id': str(a['package'].id), 'name': a['package'].name, 'sessions': a['sessions'], 'price': a['price']} for a in alts],
        }

    @classmethod
    def calculate_metabolic_nutrition_plan(cls, **kwargs) -> Dict[str, Any]:
        return calculate_metabolic_nutrition_plan(**kwargs)

    @classmethod
    def calculate_capability_matrix(cls, tests_payload: Dict[str, Any]) -> Dict[str, str]:
        matrix = {
            'lower_body': 'Developing', 'upper_body': 'Developing', 'core': 'Developing',
            'balance': 'Developing', 'mobility': 'Within Protocol', 'cardiovascular': 'Establishing Baseline',
            'functional_movement': 'Independent',
        }
        if not isinstance(tests_payload, dict):
            return matrix

        # Lower body
        lb = tests_payload.get('lower_body', {})
        assessed_lb = lb.get('assessed_tests') if isinstance(lb, dict) and isinstance(lb.get('assessed_tests'), list) else ([lb] if isinstance(lb, dict) and lb else [])
        assessed_lb = [t for t in assessed_lb if isinstance(t, dict)]
        if assessed_lb:
            if any((int(t.get('reps') or 0) >= 15 or 'goblet' in str(t.get('name') or t.get('variation') or '').lower()) and t.get('movement_quality') == 'Meets criteria' for t in assessed_lb):
                matrix['lower_body'] = 'Established'
            elif any('box' in str(t.get('name') or t.get('variation') or '').lower() or int(t.get('reps') or 0) < 8 for t in assessed_lb):
                matrix['lower_body'] = 'Beginner'

        # Upper body
        ub = tests_payload.get('upper_body', {})
        assessed_ub = ub.get('assessed_tests') if isinstance(ub, dict) and isinstance(ub.get('assessed_tests'), list) else ([ub] if isinstance(ub, dict) and ub else [])
        assessed_ub = [t for t in assessed_ub if isinstance(t, dict)]
        if assessed_ub:
            if any(('floor' in str(t.get('name') or t.get('pushup_variation') or '').lower() and int(t.get('pushup_reps') or t.get('reps') or 0) >= 12) for t in assessed_ub):
                matrix['upper_body'] = 'Established'
            elif any('wall' in str(t.get('name') or t.get('pushup_variation') or '').lower() or int(t.get('pushup_reps') or t.get('reps') or 0) < 6 for t in assessed_ub):
                matrix['upper_body'] = 'Beginner'

        # Core
        core = tests_payload.get('core', {})
        if isinstance(core, dict) and int(core.get('hold_seconds') or 0) > 0:
            h = int(core['hold_seconds'])
            matrix['core'] = 'Beginner' if h < 25 else ('Established' if h >= 60 else 'Developing')

        # Balance
        bal = tests_payload.get('balance', {})
        if isinstance(bal, dict):
            min_bal = min(int(bal.get('single_leg_left_sec') or 0), int(bal.get('single_leg_right_sec') or 0))
            if min_bal > 0:
                matrix['balance'] = 'Needs Support' if min_bal < 15 else ('Established' if min_bal >= 30 else 'Developing')

        return matrix

    @classmethod
    @transaction.atomic
    def approve_and_dispatch(
        cls,
        assessment: MemberAssessment,
        reviewer_user: TenantUser,
        coach_notes: Optional[str] = None,
        workout_prescription: Optional[str] = None,
        diet_prescription: Optional[str] = None,
        reassessment_interval_weeks: int = 6,
        send_whatsapp: bool = False,
        db_alias: Optional[str] = None,
    ) -> MemberAssessment:
        alias = _get_alias(db_alias, assessment)
        now = timezone.now()

        user_type = getattr(reviewer_user, 'user_type', None)
        is_staff = getattr(reviewer_user, 'is_staff', False)
        if user_type == 'MEMBER' and not is_staff:
            raise ValidationError("Permission Denied: Only coaches or admins can approve assessments.")

        assessment.status = 'APPROVED'
        assessment.reviewed_by_user = reviewer_user
        assessment.reviewed_at = now
        assessment.coach_signed_off_at = now
        assessment.coach_signed_off_by = reviewer_user
        assessment.reassessment_interval_weeks = reassessment_interval_weeks
        assessment.reassessment_due_date = now.date() + timedelta(weeks=reassessment_interval_weeks)

        if coach_notes:
            assessment.coach_notes = coach_notes.strip()
        if workout_prescription:
            assessment.workout_prescription = workout_prescription.strip()
        if diet_prescription:
            assessment.diet_prescription = diet_prescription.strip()

        phone = assessment.member_phone or (
            assessment.user_profile.user.phone if assessment.user_profile and getattr(assessment.user_profile, 'user', None) else None
        )
        if send_whatsapp and phone:
            try:
                from .communication.whatsapp_gupshup import GupshupWhatsAppAdapter
                adapter = GupshupWhatsAppAdapter(organization=assessment.organization, db_alias=alias)
                msg = f"Hi {assessment.member_name or 'Member'}! Your SWEAT Assessment has been approved! Goal: {assessment.get_target_goal_display()}"
                res = adapter.send_text_message(destination_phone=phone, message_text=msg)
                assessment.whatsapp_dispatch_status = 'SENT' if res.get('success') else 'FAILED'
                assessment.whatsapp_dispatched_at = now
            except Exception as e:
                logger.error("WhatsApp dispatch error: %s", str(e), exc_info=True)
                assessment.whatsapp_dispatch_status = 'FAILED'

        assessment.save(using=alias)

        if assessment.assessment_entitlement:
            ent = assessment.assessment_entitlement
            if not ent.is_unlimited and ent.allocated_units:
                ent.consumed_units = (ent.consumed_units or Decimal('0.00')) + Decimal('1.00')
                if ent.consumed_units >= ent.allocated_units:
                    ent.status = 'EXHAUSTED'
                ent.save(using=alias, update_fields=['consumed_units', 'status', 'updated_at'])

        record_business_audit(
            organization=assessment.organization,
            module='core',
            action_code='ASSESSMENT_APPROVED',
            entity_type='MemberAssessment',
            entity_id=assessment.id,
            actor_user=reviewer_user,
            event_description=f"Approved assessment for {assessment.member_name}",
            after_data={'status': assessment.status, 'reassessment_due_date': assessment.reassessment_due_date.isoformat()},
            db_alias=alias,
        )
        return assessment
