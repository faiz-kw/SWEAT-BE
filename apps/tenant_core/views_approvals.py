from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from .models_approvals import ApprovalRequest, ApprovalAction
from .serializers_approvals import ApprovalRequestSerializer, ApprovalActionSerializer
from .services_approvals import AdminApprovalService


class ApprovalRequestViewSet(viewsets.ModelViewSet):
    serializer_class = ApprovalRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = ApprovalRequest.objects.select_related('organization', 'requested_by_user').prefetch_related('actions').all()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        req_type = self.request.query_params.get('request_type')
        if req_type:
            qs = qs.filter(request_type=req_type)
        return qs

    def perform_create(self, serializer):
        user = self.request.user if hasattr(self.request.user, 'tenantuser') else None
        if user:
            serializer.save(requested_by_user=user, organization=user.organization)
        else:
            serializer.save()

    @action(detail=True, methods=['post'], url_path='act')
    def act(self, request, pk=None):
        approval_req = self.get_object()
        action_val = request.data.get('action')
        comment = request.data.get('comment')
        allow_self = request.data.get('allow_self_approval', False)

        if not action_val:
            return Response({'detail': 'action is required (APPROVED or REJECTED).'}, status=status.HTTP_400_BAD_REQUEST)

        approver = request.user if hasattr(request.user, 'tenantuser') else None
        if not approver:
            # Fallback to requested user if system test
            approver = approval_req.requested_by_user

        try:
            action_record = AdminApprovalService.process_action(
                approval_request=approval_req,
                approver_user=approver,
                action=action_val,
                comment=comment,
                allow_self_approval=allow_self,
            )
            serializer = ApprovalActionSerializer(action_record)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ApprovalActionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ApprovalActionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ApprovalAction.objects.select_related('approval_request', 'approver_user').all()
