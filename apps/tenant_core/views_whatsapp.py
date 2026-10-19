"""
apps/tenant_core/views_whatsapp.py — WhatsApp Automation Policy & Testing ViewSets.
"""

import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from config.routers import get_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_govern import WhatsAppAutomationPolicy
from apps.tenant_core.serializers_communication import (
    WhatsAppAutomationPolicySerializer,
    WhatsAppTestSendSerializer,
)
from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
from apps.tenant_core.communication.service import CommunicationService
from apps.tenant_core.services_reliability import record_business_audit
from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission

logger = logging.getLogger(__name__)


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


class WhatsAppAutomationPolicyViewSet(viewsets.ViewSet):
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.settings.view'
    permission_action_map = {
        'list': 'crm.settings.view',
        'retrieve': 'crm.settings.view',
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
        'test_send': 'crm.settings.edit',
        'trigger_reminders': 'crm.settings.edit',
    }

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        policy = WhatsAppAutomationService.get_policy(org, db_alias=alias)
        return Response(WhatsAppAutomationPolicySerializer(policy).data)

    def retrieve(self, request, pk=None):
        return self.list(request)

    def create(self, request):
        return self._save(request)

    def update(self, request, pk=None):
        return self._save(request)

    def partial_update(self, request, pk=None):
        return self._save(request)

    def patch(self, request, *args, **kwargs):
        self.action = 'partial_update'
        return self._save(request)

    def put(self, request, *args, **kwargs):
        self.action = 'update'
        return self._save(request)


    def _save(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        policy = WhatsAppAutomationService.get_policy(org, db_alias=alias)
        serializer = WhatsAppAutomationPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_policy = serializer.save()

        record_business_audit(
            organization=org,
            module='crm',
            action_code='WHATSAPP_AUTOMATION_POLICY_UPDATED',
            entity_type='WhatsAppAutomationPolicy',
            entity_id=updated_policy.id,
            actor_user=getattr(request, 'user', None),
            metadata={
                'whatsapp_enabled': updated_policy.whatsapp_enabled,
                'booking_confirmation': updated_policy.booking_confirmation_enabled,
                'reminder_offsets': updated_policy.booking_reminder_offsets_minutes,
            },
            db_alias=alias,
        )
        return Response(WhatsAppAutomationPolicySerializer(updated_policy).data)

    @action(detail=False, methods=['post'], url_path='test-send')
    def test_send(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = WhatsAppTestSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recipient = serializer.validated_data['recipient']
        template_name = serializer.validated_data.get('template_name')
        sample_variables = serializer.validated_data.get('sample_variables') or ["Demo User", "CrossFit", "Today at 7:00 PM", "Coach Sam"]

        policy = WhatsAppAutomationService.get_policy(org, db_alias=alias)
        chosen_template = template_name or policy.booking_confirmation_template_name or 'sweat_class_booking_confirm'

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=None,
                template_reference=chosen_template,
                subject="SWEAT WhatsApp Integration Test",
                body=f"Test message for template {chosen_template} with variables {sample_variables}",
                context_data=sample_variables,
                purpose='TRANSACTIONAL',
                trigger_type='MANUAL',
            )

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_TEST_DISPATCHED',
                entity_type='CommunicationMessage',
                entity_id=msg.id,
                actor_user=getattr(request, 'user', None),
                metadata={'recipient': recipient[:3] + '***', 'template': chosen_template, 'status': msg.status},
                db_alias=alias,
            )

            return Response({
                'success': msg.status in ('SENT', 'SUBMITTED', 'QUEUED'),
                'message_id': str(msg.id),
                'status': msg.status,
                'template': chosen_template,
                'provider_message_id': msg.provider_message_id,
                'failure_reason': msg.failure_reason,
            })
        except Exception as exc:
            logger.error("WhatsApp test send failed: %s", exc)
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='trigger-reminders')
    def trigger_reminders(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        dispatched = WhatsAppAutomationService.process_scheduled_reminders(db_alias=alias)
        return Response({
            'status': 'SUCCESS',
            'dispatched_count': len(dispatched),
            'messages': [str(m.id) for m in dispatched],
        })
