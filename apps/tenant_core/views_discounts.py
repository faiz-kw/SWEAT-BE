"""
apps/tenant_core/views_discounts.py — ViewSets for Layer 2 Module H: Discounts & Dynamic Offers
"""

import uuid
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from apps.tenant_core.context import get_tenant_db_alias

from .models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRuleCondition,
    DiscountRuleAction,
    DiscountRedemption,
)
from .models_workforce import UserProfile
from .models_catalog import Package
from .models_org import Branch
from .models_commerce import Order

from apps.tenant_core.services_reliability import record_business_audit
from .serializers_discounts import (
    DiscountCampaignSerializer,
    DiscountCodeSerializer,
    DiscountEligibilityRuleSerializer,
    DiscountRuleConditionSerializer,
    DiscountRuleActionSerializer,
    DiscountRedemptionSerializer,
    CouponValidationRequestSerializer,
    MemberOffersRequestSerializer,
)
from .services_discounts import DiscountCouponEngineService


def _get_db(request):
    return get_tenant_db_alias() or 'default'


class DiscountCampaignViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountCampaignSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'generate_code': 'core.settings.edit',
        'validate_coupon': 'core.settings.view',
        'evaluate_offers': 'core.settings.view',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = DiscountCampaign.objects.using(db).all()
        if org:
            qs = qs.filter(organization=org)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        org = getattr(self.request.user, 'organization', None)
        campaign = serializer.save(organization=org)
        db = _get_db(self.request)
        record_business_audit(
            organization=org,
            actor_user=self.request.user,
            module='crm',
            action_code='CAMPAIGN_CREATED',
            entity_type='DiscountCampaign',
            entity_id=campaign.id,
            event_description=f"Created discount campaign '{campaign.name}'",
            after_data={'name': campaign.name, 'discount_type': campaign.discount_type, 'discount_value': str(campaign.discount_value)},
            db_alias=db,
        )

    def perform_update(self, serializer):
        db = _get_db(self.request)
        old_obj = self.get_object()
        old_data = {'name': old_obj.name, 'status': old_obj.status, 'discount_value': str(old_obj.discount_value)}
        campaign = serializer.save()
        org = getattr(self.request.user, 'organization', None) or campaign.organization
        record_business_audit(
            organization=org,
            actor_user=self.request.user,
            module='crm',
            action_code='CAMPAIGN_UPDATED',
            entity_type='DiscountCampaign',
            entity_id=campaign.id,
            event_description=f"Updated discount campaign '{campaign.name}'",
            before_data=old_data,
            after_data={'name': campaign.name, 'status': campaign.status, 'discount_value': str(campaign.discount_value)},
            db_alias=db,
        )

    @action(detail=True, methods=['post'], url_path='generate-code')
    def generate_code(self, request, pk=None):
        campaign = self.get_object()
        db = _get_db(request)
        code_str = request.data.get('code')
        if not code_str:
            code_str = f"{campaign.name[:4].upper()}-{uuid.uuid4().hex[:6].upper()}"
        branch_id = request.data.get('branch_id')
        package_id = request.data.get('package_id')

        code = DiscountCode.objects.using(db).create(
            campaign=campaign,
            code=code_str.strip().upper(),
            branch_id=branch_id,
            package_id=package_id,
            status='ACTIVE',
        )
        org = getattr(request.user, 'organization', None) or campaign.organization
        record_business_audit(
            organization=org,
            actor_user=request.user,
            module='crm',
            action_code='COUPON_CREATED',
            entity_type='DiscountCode',
            entity_id=code.id,
            event_description=f"Generated coupon code '{code.code}' for campaign '{campaign.name}'",
            after_data={'code': code.code, 'campaign_id': str(campaign.id), 'branch_id': str(branch_id) if branch_id else None},
            db_alias=db,
        )
        return Response(DiscountCodeSerializer(code).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='validate-coupon')
    def validate_coupon(self, request):
        serializer = CouponValidationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        db = _get_db(request)
        try:
            user_profile = UserProfile.objects.using(db).get(id=data['user_profile_id'])
        except UserProfile.DoesNotExist:
            return Response({'error': 'UserProfile not found'}, status=status.HTTP_404_NOT_FOUND)

        branch = None
        if data.get('branch_id'):
            branch = Branch.objects.using(db).filter(id=data['branch_id']).first()

        package = None
        if data.get('package_id'):
            package = Package.objects.using(db).filter(id=data['package_id']).first()

        result = DiscountCouponEngineService.validate_coupon(
            code_str=data['code'],
            user_profile=user_profile,
            order_subtotal=data['order_subtotal'],
            branch=branch,
            package=package,
        )
        return Response(result, status=status.HTTP_200_OK if result['is_valid'] else status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='evaluate-offers')
    def evaluate_offers(self, request):
        serializer = MemberOffersRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        db = _get_db(request)
        try:
            user_profile = UserProfile.objects.using(db).get(id=data['user_profile_id'])
        except UserProfile.DoesNotExist:
            return Response({'error': 'UserProfile not found'}, status=status.HTTP_404_NOT_FOUND)

        branch = None
        if data.get('branch_id'):
            branch = Branch.objects.using(db).filter(id=data['branch_id']).first()

        target_package = None
        if data.get('target_package_id'):
            target_package = Package.objects.using(db).filter(id=data['target_package_id']).first()

        context = {
            'current_package_id': data.get('current_package_id'),
            'sessions_consumed': data.get('sessions_consumed', 0),
            'sessions_remaining': data.get('sessions_remaining', 0),
            'session_usage_percentage': data.get('usage_percentage', 0),
            'package_age_days': data.get('package_age_days', 0),
        }

        offers = DiscountCouponEngineService.evaluate_member_offers(
            user_profile=user_profile,
            context=context,
            branch=branch,
            target_package=target_package,
        )
        return Response({'offers': offers, 'count': len(offers)}, status=status.HTTP_200_OK)


class DiscountCodeViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountCodeSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'toggle_status': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = DiscountCode.objects.using(db).select_related('campaign', 'branch', 'package')
        if org:
            qs = qs.filter(campaign__organization=org)
        campaign_id = self.request.query_params.get('campaign_id')
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        return qs.order_by('code')

    def perform_create(self, serializer):
        code = serializer.save()
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None) or code.campaign.organization
        record_business_audit(
            organization=org,
            actor_user=self.request.user,
            module='crm',
            action_code='COUPON_CREATED',
            entity_type='DiscountCode',
            entity_id=code.id,
            event_description=f"Created coupon code '{code.code}'",
            after_data={'code': code.code, 'campaign_id': str(code.campaign_id), 'status': code.status},
            db_alias=db,
        )

    def perform_update(self, serializer):
        old_obj = self.get_object()
        old_status = old_obj.status
        code = serializer.save()
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None) or code.campaign.organization
        record_business_audit(
            organization=org,
            actor_user=self.request.user,
            module='crm',
            action_code='COUPON_UPDATED',
            entity_type='DiscountCode',
            entity_id=code.id,
            event_description=f"Updated coupon code '{code.code}'",
            before_data={'status': old_status},
            after_data={'status': code.status},
            db_alias=db,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        db = _get_db(request)
        if DiscountRedemption.objects.using(db).filter(discount_code=instance).exists():
            return Response(
                {'error': 'Cannot delete a coupon code with redemption history. Deactivate it instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        code_str = instance.code
        camp_id = str(instance.campaign_id)
        code_id = instance.id
        org = getattr(request.user, 'organization', None) or instance.campaign.organization
        response = super().destroy(request, *args, **kwargs)
        record_business_audit(
            organization=org,
            actor_user=request.user,
            module='crm',
            action_code='COUPON_DELETED',
            entity_type='DiscountCode',
            entity_id=code_id,
            event_description=f"Deleted coupon code '{code_str}'",
            before_data={'code': code_str, 'campaign_id': camp_id},
            db_alias=db,
        )
        return response

    @action(detail=True, methods=['post'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        code = self.get_object()
        db = _get_db(request)
        target_status = request.data.get('status')
        if target_status:
            target_status = target_status.upper()
            if target_status not in ['ACTIVE', 'INACTIVE', 'EXPIRED']:
                return Response({'error': f"Invalid status '{target_status}'"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_status = 'INACTIVE' if code.status == 'ACTIVE' else 'ACTIVE'

        old_status = code.status
        code.status = target_status
        code.save(using=db)

        org = getattr(request.user, 'organization', None) or code.campaign.organization
        record_business_audit(
            organization=org,
            actor_user=request.user,
            module='crm',
            action_code='COUPON_STATUS_CHANGED',
            entity_type='DiscountCode',
            entity_id=code.id,
            event_description=f"Updated coupon code '{code.code}' status from {old_status} to {target_status}",
            before_data={'status': old_status},
            after_data={'status': target_status},
            db_alias=db,
        )
        return Response(DiscountCodeSerializer(code).data, status=status.HTTP_200_OK)


class DiscountEligibilityRuleViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountEligibilityRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'add_condition': 'core.settings.edit',
        'add_action': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = DiscountEligibilityRule.objects.using(db).prefetch_related('conditions', 'actions')
        if org:
            qs = qs.filter(organization=org)
        return qs.order_by('priority', '-created_at')

    def perform_create(self, serializer):
        org = getattr(self.request.user, 'organization', None)
        serializer.save(organization=org, created_by_user=self.request.user)

    @action(detail=True, methods=['post'], url_path='add-condition')
    def add_condition(self, request, pk=None):
        rule = self.get_object()
        serializer = DiscountRuleConditionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        condition = serializer.save(discount_eligibility_rule=rule)
        return Response(DiscountRuleConditionSerializer(condition).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='add-action')
    def add_action(self, request, pk=None):
        rule = self.get_object()
        serializer = DiscountRuleActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        act = serializer.save(discount_eligibility_rule=rule)
        return Response(DiscountRuleActionSerializer(act).data, status=status.HTTP_201_CREATED)


class DiscountRedemptionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DiscountRedemptionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'redeem': 'core.settings.edit',
    }

    def get_queryset(self):
        db = _get_db(self.request)
        org = getattr(self.request.user, 'organization', None)
        qs = DiscountRedemption.objects.using(db).select_related('discount_code', 'campaign', 'user_profile', 'order')
        if org:
            qs = qs.filter(campaign__organization=org)
        campaign_id = self.request.query_params.get('campaign_id')
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        order_id = self.request.query_params.get('order_id')
        if order_id:
            qs = qs.filter(order_id=order_id)
        return qs.order_by('-redeemed_at')

    @action(detail=False, methods=['post'], url_path='redeem')
    def redeem(self, request):
        db = _get_db(request)
        code_str = request.data.get('code')
        order_id = request.data.get('order_id')
        user_profile_id = request.data.get('user_profile_id')

        if not code_str or not order_id or not user_profile_id:
            return Response(
                {'error': 'code, order_id, and user_profile_id are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order = Order.objects.using(db).get(id=order_id)
            user_profile = UserProfile.objects.using(db).get(id=user_profile_id)
        except (Order.DoesNotExist, UserProfile.DoesNotExist) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)

        try:
            redemption = DiscountCouponEngineService.redeem_coupon(
                order=order,
                code_str=code_str,
                user_profile=user_profile,
                created_by_user=request.user,
            )
            return Response(DiscountRedemptionSerializer(redemption).data, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            return Response({'error': str(exc.message if hasattr(exc, 'message') else exc)}, status=status.HTTP_400_BAD_REQUEST)
