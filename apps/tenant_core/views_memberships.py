"""
apps/tenant_core/views_memberships.py — ViewSets for Layer 2 Module I: Memberships & Entitlements
"""

import uuid
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError

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
)
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
)
from .services_memberships import MembershipLifecycleService


def _get_db(request):
    return get_tenant_db_alias() or 'default'


class MembershipViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'activate': 'core.settings.edit',
        'consume_entitlement': 'core.settings.edit',
        'reverse_entitlement': 'core.settings.edit',
        'freeze': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = Membership.objects.using(db).select_related(
            'user_profile', 'package', 'package_version', 'home_branch', 'purchase_branch'
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

    @action(detail=True, methods=['get'], url_path='contract')
    def contract(self, request, pk=None):
        membership = self.get_object()
        contract = getattr(membership, 'contract_snapshot', None)
        if not contract:
            return Response({'error': 'No contract snapshot on file for this membership.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(MembershipContractSnapshotSerializer(contract).data, status=status.HTTP_200_OK)


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
