from decimal import Decimal
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from .models_referrals import (
    ReferralProgram,
    ReferralIdentifier,
    Referral,
    ReferralQualificationRule,
    ReferralBenefitRule,
)
from .models_rewards import (
    RewardAccount,
    RewardLedger,
    OrderRewardRedemption,
)
from .models_crm import UserProfile
from .models_users import TenantUser
from .models_commerce import Order
from .serializers_rewards import (
    ReferralProgramSerializer,
    ReferralIdentifierSerializer,
    ReferralSerializer,
    ReferralQualificationRuleSerializer,
    ReferralBenefitRuleSerializer,
    RewardAccountSerializer,
    RewardLedgerSerializer,
    OrderRewardRedemptionSerializer,
)
from .services_rewards import ReferralRewardService


class ReferralProgramViewSet(viewsets.ModelViewSet):
    serializer_class = ReferralProgramSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ReferralProgram.objects.all()


class ReferralIdentifierViewSet(viewsets.ModelViewSet):
    serializer_class = ReferralIdentifierSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = ReferralIdentifier.objects.select_related('owner_user', 'referral_program').all()
        user_id = self.request.query_params.get('owner_user_id')
        if user_id:
            qs = qs.filter(owner_user_id=user_id)
        return qs

    @action(detail=False, methods=['post'], url_path='generate')
    def generate(self, request):
        program_id = request.data.get('program_id')
        owner_user_id = request.data.get('owner_user_id')
        identifier_type = request.data.get('identifier_type', 'MEMBER_REFERRAL_CODE')
        custom_code = request.data.get('custom_code')

        if not program_id or not owner_user_id:
            return Response(
                {'detail': 'program_id and owner_user_id are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        program = get_object_or_404(ReferralProgram, id=program_id)
        owner_user = get_object_or_404(TenantUser, id=owner_user_id)

        try:
            identifier = ReferralRewardService.generate_referral_identifier(
                owner_user=owner_user,
                program=program,
                identifier_type=identifier_type,
                custom_code=custom_code,
            )
            serializer = self.get_serializer(identifier)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ReferralViewSet(viewsets.ModelViewSet):
    serializer_class = ReferralSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Referral.objects.select_related('referral_program', 'referrer_user', 'referred_user').all()
        prog_id = self.request.query_params.get('program_id')
        if prog_id:
            qs = qs.filter(referral_program_id=prog_id)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    @action(detail=False, methods=['post'], url_path='register')
    def register(self, request):
        code = request.data.get('identifier_value')
        referred_user_id = request.data.get('referred_user_id')
        referred_email = request.data.get('referred_email')
        referred_phone = request.data.get('referred_phone')
        source = request.data.get('source', 'WEB')

        if not code:
            return Response({'detail': 'identifier_value is required.'}, status=status.HTTP_400_BAD_REQUEST)

        referred_user = None
        if referred_user_id:
            referred_user = get_object_or_404(TenantUser, id=referred_user_id)

        try:
            referral = ReferralRewardService.register_referral(
                identifier_value=code,
                referred_user=referred_user,
                referred_email=referred_email,
                referred_phone=referred_phone,
                source=source,
            )
            serializer = self.get_serializer(referral)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='qualify')
    def qualify(self, request, pk=None):
        referral = self.get_object()
        event_type = request.data.get('event_type', 'FIRST_PURCHASE')
        order_id = request.data.get('order_id')
        order = get_object_or_404(Order, id=order_id) if order_id else None

        referral = ReferralRewardService.qualify_and_reward_referral(
            referral=referral,
            event_type=event_type,
            order=order,
        )
        serializer = self.get_serializer(referral)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ReferralQualificationRuleViewSet(viewsets.ModelViewSet):
    serializer_class = ReferralQualificationRuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ReferralQualificationRule.objects.all()


class ReferralBenefitRuleViewSet(viewsets.ModelViewSet):
    serializer_class = ReferralBenefitRuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ReferralBenefitRule.objects.all()


class RewardAccountViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RewardAccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = RewardAccount.objects.select_related('user_profile').all()
        user_profile_id = self.request.query_params.get('user_profile_id')
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        return qs


class RewardLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RewardLedgerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = RewardLedger.objects.select_related('reward_account', 'user_profile').all()
        user_profile_id = self.request.query_params.get('user_profile_id')
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        return qs

    @action(detail=False, methods=['post'], url_path='earn')
    def earn(self, request):
        user_profile_id = request.data.get('user_profile_id')
        reward_type = request.data.get('reward_type', 'POINTS')
        quantity_str = request.data.get('quantity')
        reason_code = request.data.get('reason_code', 'MANUAL_GRANT')
        reason = request.data.get('reason')

        if not user_profile_id or not quantity_str:
            return Response({'detail': 'user_profile_id and quantity are required.'}, status=status.HTTP_400_BAD_REQUEST)

        user_profile = get_object_or_404(UserProfile, id=user_profile_id)
        quantity = Decimal(str(quantity_str))

        try:
            actor = request.user if hasattr(request.user, 'tenantuser') else None
            ledger = ReferralRewardService.earn_rewards(
                user_profile=user_profile,
                reward_type=reward_type,
                quantity=quantity,
                reason_code=reason_code,
                reason=reason,
                created_by_user=actor,
            )
            serializer = self.get_serializer(ledger)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='redeem')
    def redeem(self, request):
        user_profile_id = request.data.get('user_profile_id')
        reward_type = request.data.get('reward_type', 'POINTS')
        quantity_str = request.data.get('quantity')
        reason_code = request.data.get('reason_code', 'MANUAL_REDEMPTION')
        reason = request.data.get('reason')

        if not user_profile_id or not quantity_str:
            return Response({'detail': 'user_profile_id and quantity are required.'}, status=status.HTTP_400_BAD_REQUEST)

        user_profile = get_object_or_404(UserProfile, id=user_profile_id)
        quantity = Decimal(str(quantity_str))

        try:
            actor = request.user if hasattr(request.user, 'tenantuser') else None
            ledger, redemption = ReferralRewardService.redeem_rewards(
                user_profile=user_profile,
                reward_type=reward_type,
                quantity=quantity,
                reason_code=reason_code,
                reason=reason,
                created_by_user=actor,
            )
            serializer = self.get_serializer(ledger)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class OrderRewardRedemptionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderRewardRedemptionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return OrderRewardRedemption.objects.all()
