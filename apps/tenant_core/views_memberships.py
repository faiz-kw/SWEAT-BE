"""
apps/tenant_core/views_memberships.py — ViewSets for Layer 2 Module I: Memberships & Entitlements
"""

import uuid
from decimal import Decimal
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.utils import timezone
from config.routers import set_tenant_db_alias, get_tenant_db_alias

from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from apps.tenant_core.context import get_tenant_db_alias

from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
    MembershipExtension,
    MembershipPolicyConfig,
)
from .models_org import Organization
from .models_commerce import Order, OrderItem
from .serializers_memberships import (
    MembershipSerializer,
    MembershipContractSnapshotSerializer,
    MembershipEntitlementSerializer,
    MembershipEntitlementLedgerSerializer,
    MembershipBranchHistorySerializer,
    MembershipStatusHistorySerializer,
    MembershipFreezeSerializer,
    MembershipRenewalPolicySerializer,
    MembershipChangePolicySerializer,
    MembershipChangePolicyRuleSerializer,
    MembershipChangeRequestSerializer,
    MembershipPackageHistorySerializer,
    ActivateMembershipRequestSerializer,
    ConsumeEntitlementRequestSerializer,
    FreezeRequestSerializer,
    MembershipExtensionSerializer,
    MembershipPolicyConfigSerializer,
    ExtendMembershipRequestSerializer,
    CalculateExtensionRequestSerializer,
    CalculateFreezeRequestSerializer,
)
from .services_memberships import MembershipLifecycleService


def _get_db(request):
    alias = (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )
    if alias and alias != 'default':
        set_tenant_db_alias(alias)
    return alias


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
    return org


class MembershipPagination(PageNumberPagination):
    """Standard cursor-based page pagination for the Membership list endpoint."""
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 200
    page_query_param = 'page'

    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'total_pages': self.page.paginator.num_pages,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data,
        })


class MembershipViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'
    permission_action_map = {
        'create': 'core.users.edit',
        'update': 'core.users.edit',
        'destroy': 'core.users.edit',
        'activate': 'core.users.edit',
        'consume_entitlement': 'core.users.edit',
        'reverse_entitlement': 'core.users.edit',
        'freeze': 'core.users.edit',
        'extend': 'core.users.edit',
        'calculate_extension': 'core.users.view',
        'calculate_freeze': 'core.users.view',
    }
    pagination_class = MembershipPagination
    filter_backends = [filters.OrderingFilter]
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ['parq_requirement', 'sign_parq']:
            from rest_framework.permissions import IsAuthenticated
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        db = _get_db(self.request)
        if self.action in ['parq_requirement', 'sign_parq']:
            return Membership.objects.using(db).select_related(
                'user_profile', 'user_profile__user', 'program', 'package', 'package_version', 'parq_form', 'parq_submission'
            )
        org = getattr(self.request.user, 'organization', None)
        qs = Membership.objects.using(db).select_related(
            'user_profile', 'user_profile__user', 'package', 'package_version', 'home_branch', 'purchase_branch'
        ).prefetch_related('entitlements')
        if org:
            qs = qs.filter(home_branch__organization=org)
        user_prof = self.request.query_params.get('user_profile_id')
        if user_prof:
            qs = qs.filter(user_profile_id=user_prof)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(home_branch_id=branch_id)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        # Server-side search across member identity, contact, and package fields
        search = self.request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(membership_number__icontains=search)
                | Q(user_profile__member_number__icontains=search)
                | Q(user_profile__first_name_snapshot__icontains=search)
                | Q(user_profile__last_name_snapshot__icontains=search)
                | Q(user_profile__user__email__icontains=search)
                | Q(user_profile__user__phone__icontains=search)
                | Q(package__name__icontains=search)
            )

        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save()

    @action(detail=False, methods=['post'], url_path='activate')
    def activate(self, request):
        serializer = ActivateMembershipRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            order = Order.objects.using(db).get(id=data['order_id'])
            order_item = OrderItem.objects.using(db).get(id=data['order_item_id'], order=order)
        except (Order.DoesNotExist, OrderItem.DoesNotExist) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)

        try:
            membership = MembershipLifecycleService.activate_membership_from_order(
                order=order,
                order_item=order_item,
                start_date=data.get('start_date'),
                db_alias=db,
                created_by_user=request.user,
            )
            return Response(MembershipSerializer(membership).data, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='consume-entitlement')
    def consume_entitlement(self, request, pk=None):
        membership = self.get_object()
        serializer = ConsumeEntitlementRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            ledger = MembershipLifecycleService.consume_entitlement(
                membership=membership,
                entitlement_type=data['entitlement_type'],
                units=data['units'],
                booking_id=data.get('booking_id'),
                reason_text=data.get('reason_text'),
                created_by_user=request.user,
                db_alias=db,
            )
            return Response(MembershipEntitlementLedgerSerializer(ledger).data, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='reverse-entitlement')
    def reverse_entitlement(self, request, pk=None):
        membership = self.get_object()
        serializer = ConsumeEntitlementRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            ledger = MembershipLifecycleService.reverse_entitlement(
                membership=membership,
                entitlement_type=data['entitlement_type'],
                units=data['units'],
                booking_id=data.get('booking_id'),
                reason_text=data.get('reason_text'),
                created_by_user=request.user,
                db_alias=db,
            )
            return Response(MembershipEntitlementLedgerSerializer(ledger).data, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='freeze')
    def freeze(self, request, pk=None):
        membership = self.get_object()
        serializer = FreezeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            freeze = MembershipLifecycleService.apply_freeze(
                membership=membership,
                freeze_from=data['freeze_from'],
                freeze_until=data['freeze_until'],
                reason_text=data.get('reason_text'),
                approved_by_user=request.user,
                db_alias=db,
            )
            return Response(MembershipFreezeSerializer(freeze).data, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='calculate-freeze')
    def calculate_freeze(self, request, pk=None):
        membership = self.get_object()
        serializer = CalculateFreezeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            res = MembershipLifecycleService.calculate_freeze(
                membership=membership,
                freeze_from=data['freeze_from'],
                freeze_until=data['freeze_until'],
                db_alias=db,
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='calculate-extension')
    def calculate_extension(self, request, pk=None):
        membership = self.get_object()
        serializer = CalculateExtensionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data['days']
        db = _get_db(request)

        try:
            res = MembershipLifecycleService.calculate_extension(
                membership=membership,
                days=days,
                db_alias=db,
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='extend')
    def extend(self, request, pk=None):
        membership = self.get_object()
        serializer = ExtendMembershipRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        db = _get_db(request)

        try:
            updated_membership, extension = MembershipLifecycleService.extend_membership(
                membership=membership,
                days=data['days'],
                reason_text=data.get('reason_text'),
                approved_by_user=request.user,
                db_alias=db,
            )
            return Response({
                'membership': MembershipSerializer(updated_membership).data,
                'extension': MembershipExtensionSerializer(extension).data,
                'message': f"Membership validity successfully extended by {data['days']} days.",
            }, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'], url_path='contract')
    def contract(self, request, pk=None):
        membership = self.get_object()
        contract = getattr(membership, 'contract_snapshot', None)
        if not contract:
            db = _get_db(request)
            try:
                pv = membership.package_version
                ents = [
                    {
                        'entitlement_type': e.entitlement_type,
                        'allocated_units': str(e.allocated_units) if e.allocated_units else None,
                        'is_unlimited': e.is_unlimited,
                    }
                    for e in membership.entitlements.all()
                ]
                contract = MembershipContractSnapshot.objects.using(db).create(
                    membership=membership,
                    package=membership.package,
                    package_version=pv,
                    package_price=membership.package_price,
                    package_name_snapshot=membership.package.name if membership.package else 'Standard Plan',
                    purchase_price=Decimal('0.00'),
                    discount_amount=Decimal('0.00'),
                    tax_amount=Decimal('0.00'),
                    final_amount=Decimal('0.00'),
                    currency='INR',
                    duration_value=pv.duration_value if pv else 1,
                    duration_unit=pv.duration_unit if pv else 'MONTH',
                    start_date=membership.start_date,
                    end_date=membership.end_date,
                    entitlements_snapshot=ents,
                    purchase_branch=membership.purchase_branch or membership.home_branch,
                )
            except Exception:
                return Response({'error': 'No contract snapshot on file for this membership.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(MembershipContractSnapshotSerializer(contract).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=['get'], url_path='parq-requirement')
    def parq_requirement(self, request, pk=None):
        alias = _get_db(request)
        membership = self.get_object()

        # Check access: authenticated member owner OR staff with view permission
        user = request.user
        is_owner = (
            user.is_authenticated and
            membership.user_profile and
            (
                membership.user_profile.user_id == user.id or
                (membership.user_profile.user and membership.user_profile.user.email and
                 user.email and membership.user_profile.user.email.lower() == user.email.lower())
            )
        )
        is_staff = user.is_authenticated and (
            getattr(user, 'is_staff_or_admin', False)
            or getattr(user, 'is_staff', False)
            or getattr(user, 'is_superuser', False)
            or getattr(user, 'user_type', None) in ['STAFF', 'ADMIN', 'SUPERADMIN', 'TENANT_ADMIN', 'ORG_ADMIN']
            or any(r in str(getattr(user, 'role', '')).upper() for r in ['ADMIN', 'STAFF', 'MANAGER', 'DESK', 'OWNER', 'ORG'])
            or any(r in str(getattr(user, 'role_code', '')).upper() for r in ['ADMIN', 'STAFF', 'MANAGER', 'DESK', 'OWNER', 'ORG'])
        )
        if not (is_owner or is_staff):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        # Resolve active form if not set
        form = membership.parq_form
        if not form:
            from .models_crm import IntakeForm
            org = membership.home_branch.organization if membership.home_branch else None
            if not org and membership.user_profile and membership.user_profile.preferred_branch:
                org = membership.user_profile.preferred_branch.organization
            if org and membership.program:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], assigned_programs=membership.program)
                    .order_by('-version_number')
                    .first()
                )
            if not form and org:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], is_default_for_all_programs=True)
                    .order_by('-version_number')
                    .first()
                )
            if not form and membership.program:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], assigned_programs=membership.program)
                    .order_by('-version_number')
                    .first()
                )
            if not form:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], is_default_for_all_programs=True)
                    .order_by('-version_number')
                    .first()
                )
            if not form:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status__in=['ACTIVE', 'PUBLISHED'], form_type__in=['PAR_Q', 'PARQ', 'ONBOARDING'])
                    .order_by('-version_number')
                    .first()
                )
            if form and not membership.parq_form:
                membership.parq_form = form
                membership.save(using=alias, update_fields=['parq_form'])

        form_data = None
        if form:
            from .serializers_crm import IntakeFormSerializer
            form_data = IntakeFormSerializer(form).data

        from .models_crm import IntakeSubmission
        user_prof = membership.user_profile
        prior_subs = (
            IntakeSubmission.objects.using(alias)
            .filter(user_profile=user_prof, status='COMPLETED')
            .order_by('-submitted_at')
        ) if user_prof else IntakeSubmission.objects.none()
        
        submission_history = []
        for s_rec in prior_subs:
            s_answers = []
            for a_rec in s_rec.answers.all():
                if a_rec.boolean_value is not None:
                    val_text = 'Yes' if a_rec.boolean_value else 'No'
                elif a_rec.text_value:
                    val_text = a_rec.text_value
                elif a_rec.numeric_value is not None:
                    val_text = str(a_rec.numeric_value)
                elif a_rec.json_value is not None:
                    if isinstance(a_rec.json_value, (list, tuple)):
                        val_text = ', '.join(str(item) for item in a_rec.json_value)
                    elif isinstance(a_rec.json_value, dict):
                        val_text = a_rec.json_value.get('label') or a_rec.json_value.get('value') or a_rec.json_value.get('answer') or ', '.join(f"{k}: {v}" for k, v in a_rec.json_value.items())
                    else:
                        val_text = str(a_rec.json_value)
                elif a_rec.date_value is not None:
                    val_text = a_rec.date_value.isoformat()
                else:
                    val_text = ''

                s_answers.append({
                    'question_id': str(a_rec.question.id) if a_rec.question else '',
                    'question_text': a_rec.question.question_text if a_rec.question else '',
                    'question_type': getattr(a_rec.question, 'question_type', 'TEXT') if a_rec.question else 'TEXT',
                    'category': getattr(a_rec.question, 'category', 'GENERAL') if a_rec.question else 'GENERAL',
                    'is_sensitive': getattr(a_rec.question, 'is_sensitive', False) if a_rec.question else False,
                    'answer': val_text,
                    'boolean_value': a_rec.boolean_value,
                    'numeric_value': float(a_rec.numeric_value) if a_rec.numeric_value is not None else None,
                })
            s_meta = getattr(s_rec, 'metadata', {}) or {}
            submission_history.append({
                'id': str(s_rec.id),
                'form_title': (s_rec.form_snapshot.get('form_title') if isinstance(s_rec.form_snapshot, dict) else None) or (s_rec.intake_form.name if s_rec.intake_form else 'PAR-Q Form'),
                'form_version': (s_rec.form_snapshot.get('version_number') if isinstance(s_rec.form_snapshot, dict) else None) or (s_rec.intake_form.version_number if s_rec.intake_form else 1),
                'submitted_at': s_rec.submitted_at.isoformat() if s_rec.submitted_at else None,
                'signature_date': s_rec.signature_date.isoformat() if s_rec.signature_date else None,
                'signer_identity': s_rec.signer_identity,
                'signature_data': s_rec.signature_data,
                'agreement_accepted': s_rec.agreement_accepted,
                'agreement_title': getattr(s_rec.intake_form, 'agreement_title', '') if s_rec.intake_form else '',
                'agreement_text_snapshot': s_rec.agreement_text_snapshot,
                'is_edit': s_meta.get('is_edit', False),
                'revision_number': s_meta.get('revision_number', 1),
                'answers': s_answers,
            })

        submission_data = None
        if membership.parq_submission:
            sub = membership.parq_submission
            submission_data = {
                'id': str(sub.id),
                'form_title': getattr(sub.intake_form, 'name', '') if sub.intake_form else '',
                'form_version': getattr(sub.intake_form, 'version_number', 1) if sub.intake_form else 1,
                'submitted_at': sub.submitted_at.isoformat() if sub.submitted_at else None,
                'signature_date': sub.signature_date.isoformat() if sub.signature_date else None,
                'signer_identity': sub.signer_identity,
                'signature_data': sub.signature_data,
                'agreement_accepted': sub.agreement_accepted,
                'agreement_title': getattr(sub.intake_form, 'agreement_title', '') if sub.intake_form else '',
                'agreement_text_snapshot': sub.agreement_text_snapshot,
                'form_snapshot': sub.form_snapshot,
            }
        elif submission_history:
            submission_data = submission_history[0]

        return Response({
            'membership_id': str(membership.id),
            'membership_number': membership.membership_number,
            'program_name': membership.program.name if membership.program else (membership.package.program.name if membership.package and membership.package.program else 'General'),
            'package_name': membership.package.name if membership.package else 'Membership',
            'parq_status': membership.parq_status,
            'parq_completed_at': membership.parq_completed_at.isoformat() if membership.parq_completed_at else None,
            'is_configured': form is not None,
            'form': form_data,
            'submission': submission_data,
            'has_existing_submission': len(submission_history) > 0,
            'existing_submission': submission_history[0] if submission_history else None,
            'submission_history': submission_history,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='sign-parq')
    def sign_parq(self, request, pk=None):
        alias = _get_db(request)
        membership = self.get_object()

        # Strict authentication & member ownership check
        user = request.user
        if not user or not user.is_authenticated:
            return Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

        user_profile = membership.user_profile
        is_owner = (
            user_profile and
            (
                user_profile.user_id == user.id or
                (user_profile.user and user_profile.user.email and
                 user.email and user_profile.user.email.lower() == user.email.lower())
            )
        )
        is_staff_assisting = (
            getattr(user, 'is_staff', False)
            or getattr(user, 'is_superuser', False)
            or getattr(user, 'user_type', None) in ['STAFF', 'ADMIN', 'SUPERADMIN']
            or any(r in str(getattr(user, 'role', '')).upper() for r in ['ADMIN', 'STAFF', 'MANAGER', 'DESK'])
        )
        if not is_owner and not is_staff_assisting:
            return Response(
                {'detail': "Only the authenticated member or front desk staff assisting on a studio tablet can complete and submit the PAR-Q signature."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Decision & Idempotency check
        parq_decision = str(request.data.get('parq_decision', '')).upper().strip()

        # OPTION A: REUSE EXISTING PAR-Q
        if parq_decision == 'REUSE':
            from .models_crm import IntakeSubmission
            prior_subs = (
                IntakeSubmission.objects.using(alias)
                .filter(user_profile=user_profile, status='COMPLETED')
                .order_by('-submitted_at')
            )
            existing_sub_id = request.data.get('existing_submission_id')
            existing_sub = prior_subs.filter(id=existing_sub_id).first() if existing_sub_id else prior_subs.first()
            if not existing_sub:
                return Response(
                    {'detail': 'No existing completed PAR-Q submission found to reuse.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Reusing MUST NOT create a duplicate IntakeSubmission record
            with transaction.atomic(using=alias):
                membership = Membership.objects.using(alias).select_for_update().get(id=membership.id)
                membership.parq_status = 'COMPLETED'
                membership.parq_submission = existing_sub
                membership.parq_completed_at = existing_sub.submitted_at
                membership.parq_form = existing_sub.intake_form or form
                membership.save(using=alias, update_fields=['parq_status', 'parq_submission', 'parq_completed_at', 'parq_form', 'updated_at'])

            return Response({
                'success': True,
                'message': f"Existing PAR-Q from {existing_sub.submitted_at.strftime('%b %d, %Y')} reused successfully. Historical record preserved.",
                'membership_id': str(membership.id),
                'membership_number': membership.membership_number,
                'submission_id': str(existing_sub.id),
                'parq_status': 'COMPLETED',
                'is_reused': True,
                'completed_at': existing_sub.submitted_at.isoformat(),
            }, status=status.HTTP_200_OK)

        if membership.parq_status == 'COMPLETED' and not parq_decision:
            return Response(
                {'detail': f"PAR-Q has already been completed and digitally signed for membership {membership.membership_number}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Form resolution
        form = membership.parq_form
        if not form:
            from .models_crm import IntakeForm
            org = membership.home_branch.organization if membership.home_branch else None
            if not org and user_profile and user_profile.preferred_branch:
                org = user_profile.preferred_branch.organization
            if org and membership.program:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], assigned_programs=membership.program)
                    .order_by('-version_number')
                    .first()
                )
            if not form and org:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], is_default_for_all_programs=True)
                    .order_by('-version_number')
                    .first()
                )
            if not form and membership.program:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], assigned_programs=membership.program)
                    .order_by('-version_number')
                    .first()
                )
            if not form:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], is_default_for_all_programs=True)
                    .order_by('-version_number')
                    .first()
                )
            if not form:
                form = (
                    IntakeForm.objects.using(alias)
                    .filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'])
                    .order_by('-version_number')
                    .first()
                )
        if not form:
            return Response(
                {'detail': 'No active PAR-Q form is configured for this program or organization. Please contact gym administration.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Explicit consent verification
        agreement_accepted = bool(request.data.get('agreement_accepted', False))
        if not agreement_accepted:
            return Response(
                {'detail': 'You must explicitly review and accept the Physical Activity Readiness agreement.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Drawn digital signature verification (supports uploaded file or base64)
        uploaded_sig = request.FILES.get('signature') or request.FILES.get('signature_file') or request.FILES.get('signature_data')
        signature_data = request.data.get('signature_data') or request.data.get('signature')
        has_valid_signature = bool(uploaded_sig) or (
            bool(signature_data) and (
                str(signature_data).startswith('data:image/') or
                str(signature_data).startswith('/media/') or
                len(str(signature_data)) >= 100
            )
        )
        if not has_valid_signature:
            return Response(
                {'detail': 'A valid drawn digital signature is required. Typed names or empty signatures are not accepted.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate answers
        raw_answers = request.data.get('answers', [])
        if isinstance(raw_answers, str):
            try:
                import json
                raw_answers = json.loads(raw_answers)
            except Exception:
                pass
        if isinstance(raw_answers, dict):
            raw_answers = [{'question_id': k, 'value': v} for k, v in raw_answers.items()]
        elif not isinstance(raw_answers, list):
            return Response({'detail': 'Answers must be provided as a list or map.'}, status=status.HTTP_400_BAD_REQUEST)

        answers_map = {}
        for a in raw_answers:
            if isinstance(a, dict):
                qid = str(a.get('question_id') or a.get('id') or '')
                if qid:
                    answers_map[qid] = a
                if a.get('order'):
                    answers_map[f"q_{a['order']}"] = a
                    answers_map[str(a['order'])] = a

        from .models_crm import IntakeQuestion, IntakeAnswer, IntakeSubmission
        questions = list(
            IntakeQuestion.objects.using(alias)
            .filter(intake_form=form, status='ACTIVE')
            .prefetch_related('options')
            .order_by('display_order', 'created_at')
        )

        for q in questions:
            if q.is_required:
                ans_entry = answers_map.get(str(q.id))
                if not ans_entry:
                    return Response({'detail': f"Question '{q.question_text}' is required."}, status=status.HTTP_400_BAD_REQUEST)
                val = ans_entry.get('value')
                # Note: legitimate False or 'No' or 0 is valid!
                if val is None or val == '':
                    return Response({'detail': f"Question '{q.question_text}' is required."}, status=status.HTTP_400_BAD_REQUEST)

        # All-or-nothing atomic creation
        with transaction.atomic(using=alias):
            # Lock membership
            membership = Membership.objects.using(alias).select_for_update().get(id=membership.id)
            if membership.parq_status == 'COMPLETED':
                return Response(
                    {'detail': f"PAR-Q has already been completed for membership {membership.membership_number}."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Build immutable form snapshot
            q_snapshots = []
            for q in questions:
                ans_entry = answers_map.get(str(q.id), {})
                val = ans_entry.get('value')
                q_snapshots.append({
                    'id': str(q.id),
                    'question_text': q.question_text,
                    'question_type': q.question_type,
                    'category': getattr(q, 'category', 'GENERAL'),
                    'order': getattr(q, 'display_order', 0),
                    'is_required': q.is_required,
                    'options': [
                        {'label': opt.label, 'value': opt.value, 'order': getattr(opt, 'display_order', 0)}
                        for opt in q.options.all()
                    ],
                    'submitted_answer': val,
                })

            now_dt = timezone.now()
            signer_ip = request.META.get('REMOTE_ADDR') or request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            signer_user_agent = request.META.get('HTTP_USER_AGENT', '')
            signer_identity = getattr(user, 'email', '') or getattr(user, 'username', 'Member')

            from .services_storage import save_compressed_signature
            sig_input = uploaded_sig if uploaded_sig else signature_data
            stored_signature = save_compressed_signature(sig_input, tenant_slug=alias)

            submission = IntakeSubmission.objects.using(alias).create(
                intake_form=form,
                user_profile=user_profile,
                lead=getattr(user_profile, 'source_lead', None),
                membership=membership,
                order=membership.source_order,
                program=membership.program or (membership.package.program if membership.package else None),
                agreement_accepted=True,
                agreement_accepted_at=now_dt,
                agreement_text_snapshot=form.agreement_text or '',
                form_snapshot={
                    'form_id': str(form.id),
                    'form_title': form.name,
                    'version_number': form.version_number,
                    'agreement_title': form.agreement_title,
                    'agreement_text': form.agreement_text,
                    'questions': q_snapshots,
                },
                metadata={
                    'accepted_by_name': getattr(user, 'display_name', '') or signer_identity,
                    'signer_type': 'MEMBER_DIRECT',
                    'channel': 'MEMBER_PORTAL',
                    'membership_number': membership.membership_number,
                    'is_edit': parq_decision == 'EDIT',
                    'submission_type': 'REVISION' if parq_decision == 'EDIT' else ('NEW_ASSESSMENT' if parq_decision == 'NEW' else 'INITIAL'),
                    'parq_decision': parq_decision or 'INITIAL',
                },
                signature_data=stored_signature,
                signature_date=now_dt,
                signer_identity=signer_identity,
                signer_ip=signer_ip,
                signer_user_agent=signer_user_agent,
                status='COMPLETED',
                submitted_at=now_dt,
            )

            for q in questions:
                ans_entry = answers_map.get(str(q.id), {})
                val = ans_entry.get('value')
                txt_val = None
                num_val = None
                bool_val = None
                dt_val = None
                json_val = None

                if q.question_type == 'BOOLEAN':
                    if isinstance(val, bool):
                        bool_val = val
                    elif str(val).lower() in ['true', 'yes', '1']:
                        bool_val = True
                    elif str(val).lower() in ['false', 'no', '0']:
                        bool_val = False
                elif q.question_type == 'NUMBER':
                    try:
                        num_val = Decimal(str(val))
                    except Exception:
                        pass
                elif q.question_type == 'DATE':
                    try:
                        from datetime import datetime
                        dt_val = datetime.strptime(str(val), '%Y-%m-%d').date()
                    except Exception:
                        pass
                elif q.question_type in ['MULTI_CHOICE', 'JSON', 'MULTI_SELECT']:
                    json_val = val if isinstance(val, (list, dict)) else {'val': val}
                else:
                    txt_val = str(val) if val is not None else ''

                IntakeAnswer.objects.using(alias).create(
                    submission=submission,
                    question=q,
                    text_value=txt_val,
                    numeric_value=num_val,
                    boolean_value=bool_val,
                    date_value=dt_val,
                    json_value=json_val or {},
                )

            # Update membership
            membership.parq_status = 'COMPLETED'
            membership.parq_form = form
            membership.parq_submission = submission
            membership.parq_completed_at = now_dt
            membership.save(using=alias, update_fields=['parq_status', 'parq_form', 'parq_submission', 'parq_completed_at', 'updated_at'])

        return Response({
            'success': True,
            'message': 'PAR-Q completed and digitally signed successfully. You may now book classes.',
            'membership_id': str(membership.id),
            'membership_number': membership.membership_number,
            'submission_id': str(submission.id),
            'parq_status': 'COMPLETED',
            'completed_at': now_dt.isoformat(),
        }, status=status.HTTP_201_CREATED)

class MembershipEntitlementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MembershipEntitlementSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipEntitlement.objects.using(db).select_related('membership')
        membership_id = self.request.query_params.get('membership_id')
        if membership_id:
            qs = qs.filter(membership_id=membership_id)
        return qs.order_by('-created_at')


class MembershipEntitlementLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MembershipEntitlementLedgerSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipEntitlementLedger.objects.using(db).select_related('membership_entitlement')
        entitlement_id = self.request.query_params.get('entitlement_id')
        if entitlement_id:
            qs = qs.filter(membership_entitlement_id=entitlement_id)
        return qs.order_by('-created_at')


class MembershipFreezeViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipFreezeSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipFreeze.objects.using(db).select_related('membership')
        membership_id = self.request.query_params.get('membership_id')
        if membership_id:
            qs = qs.filter(membership_id=membership_id)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(approved_by_user=self.request.user)


class MembershipRenewalPolicyViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipRenewalPolicySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipRenewalPolicy.objects.using(db).select_related('package')
        package_id = self.request.query_params.get('package_id')
        if package_id:
            qs = qs.filter(package_id=package_id)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save()


class MembershipChangePolicyViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipChangePolicySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'add_rule': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = MembershipChangePolicy.objects.using(db).prefetch_related('rules')
        if org:
            qs = qs.filter(organization=org)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        org = getattr(self.request.user, 'organization', None)
        serializer.save(organization=org, created_by_user=self.request.user)

    @action(detail=True, methods=['post'], url_path='add-rule')
    def add_rule(self, request, pk=None):
        policy = self.get_object()
        serializer = MembershipChangePolicyRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save(membership_change_policy=policy)
        return Response(MembershipChangePolicyRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


class MembershipChangeRequestViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipChangeRequestSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipChangeRequest.objects.using(db).select_related(
            'membership', 'current_package', 'target_package'
        )
        membership_id = self.request.query_params.get('membership_id')
        if membership_id:
            qs = qs.filter(membership_id=membership_id)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(requested_by_user=self.request.user)


class MembershipBranchHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MembershipBranchHistorySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipBranchHistory.objects.using(db).select_related(
            'membership', 'from_branch', 'to_branch', 'membership__user_profile', 'membership__user_profile__user', 'changed_by_user'
        )
        membership_id = self.request.query_params.get('membership_id')
        if membership_id:
            qs = qs.filter(membership_id=membership_id)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id and branch_id not in ['all', 'ALL']:
            qs = qs.filter(Q(from_branch_id=branch_id) | Q(to_branch_id=branch_id))
        search = self.request.query_params.get('search')
        if search:
            s = search.strip()
            qs = qs.filter(
                Q(membership__membership_number__icontains=s)
                | Q(membership__user_profile__first_name_snapshot__icontains=s)
                | Q(membership__user_profile__last_name_snapshot__icontains=s)
                | Q(membership__user_profile__user__email__icontains=s)
                | Q(reason__icontains=s)
            )
        return qs.order_by('-effective_at', '-created_at')


class MembershipExtensionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Append-only audit ledger of package validity extensions with fee breakdowns.
    """
    serializer_class = MembershipExtensionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'

    def get_queryset(self):
        db = _get_db(self.request)
        qs = MembershipExtension.objects.using(db).select_related(
            'membership', 'membership__user_profile', 'membership__package', 'created_by_user'
        )
        membership_id = self.request.query_params.get('membership_id')
        if membership_id:
            qs = qs.filter(membership_id=membership_id)
        return qs.order_by('-created_at')


class MembershipPolicyConfigViewSet(viewsets.ViewSet):
    """
    Tenant Admin Configurable Settings for Membership Package Extend and Freeze.
    Enforces role-based authority: Strictly restricted to ORG_ADMIN.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'memberships'
    required_permission = 'membership.policy.manage'
    permission_action_map = {
        'list': 'membership.policy.manage',
        'retrieve': 'membership.policy.manage',
        'current': 'membership.policy.manage',
        'update': 'membership.policy.manage',
        'partial_update': 'membership.policy.manage',
        'create': 'membership.policy.manage',
    }

    def _ensure_org_admin(self, request):
        user = request.user
        is_admin = (
            getattr(user, 'is_superuser', False)
            or getattr(user, 'is_org_wide', False)
            or (getattr(user, '_auth_type', None) == 'platform')
            or any(r.code in ('ORG_ADMIN', 'TENANT_ADMIN', 'SUPER_ADMIN') for r in getattr(user, 'roles', []).all() if hasattr(user, 'roles'))
            or getattr(user, 'role', '') in ('ORG_ADMIN', 'TENANT_ADMIN', 'SUPER_ADMIN')
        )
        user_role = getattr(user, 'role', None)
        if user_role and user_role in ('ORG_ADMIN', 'TENANT_ADMIN', 'SUPER_ADMIN'):
            is_admin = True
        return is_admin

    def list(self, request):
        db = _get_db(request)
        org = _get_org(request)
        config = MembershipLifecycleService.get_policy_config(org, db_alias=db)
        return Response(MembershipPolicyConfigSerializer(config).data)

    def retrieve(self, request, pk=None):
        return self.list(request)

    @action(detail=False, methods=['get', 'patch', 'post'], url_path='current')
    def current(self, request):
        db = _get_db(request)
        org = _get_org(request)
        config = MembershipLifecycleService.get_policy_config(org, db_alias=db)

        if request.method in ('PATCH', 'POST'):
            if not self._ensure_org_admin(request):
                return Response(
                    {'detail': 'Only Organization Administrators are authorized to update package extension & freeze settings.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            serializer = MembershipPolicyConfigSerializer(config, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            saved = serializer.save(updated_by_user=request.user)
            return Response(MembershipPolicyConfigSerializer(saved).data, status=status.HTTP_200_OK)

        return Response(MembershipPolicyConfigSerializer(config).data)

    def create(self, request):
        return self.current(request)

    def partial_update(self, request, pk=None):
        return self.current(request)

