"""
Views for CRM Leads pipeline, activity timeline, and stage progression.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
import uuid

from .models import Lead, LeadActivity, LeadStage, LeadStatus
from .serializers import LeadSerializer, LeadActivitySerializer


class LeadViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Sales Pipeline Leads with Activity Timeline and Kanban aggregation.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LeadSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'phone', 'email', 'goal', 'notes']
    ordering_fields = ['created_at', 'score', 'budget', 'last_contact_at']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = Lead.objects.all()
        else:
            qs = Lead.objects.filter(tenant=user.tenant)

        # Query parameter filters
        stage = self.request.query_params.get('stage')
        if stage:
            qs = qs.filter(stage=stage)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        location_id = self.request.query_params.get('location')
        if location_id and location_id != 'all':
            qs = qs.filter(location_id=location_id)

        source = self.request.query_params.get('source')
        if source:
            qs = qs.filter(source=source)

        assigned_to = self.request.query_params.get('assigned_to')
        if assigned_to:
            qs = qs.filter(assigned_to_id=assigned_to)

        return qs.select_related('location', 'assigned_to', 'tenant').prefetch_related('activities__performed_by')

    def perform_create(self, serializer):
        from apps.tenants.models import Tenant, Location
        user = self.request.user
        tenant = user.tenant or Tenant.objects.first()
        location = serializer.validated_data.get('location')
        if not location:
            location = Location.objects.filter(tenant=tenant).first()
        lead_id = serializer.validated_data.get('id') or f"LED-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=lead_id, tenant=tenant, location=location)


    def perform_update(self, serializer):
        old_lead = self.get_object()
        old_stage = old_lead.stage
        lead = serializer.save()

        # If stage changed, log audit activity
        if old_stage != lead.stage:
            LeadActivity.objects.create(
                tenant=lead.tenant,
                lead=lead,
                activity_type='Stage Change',
                summary=f"Pipeline stage changed from '{old_stage}' to '{lead.stage}'.",
                performed_by=self.request.user if self.request.user.is_authenticated else None,
            )

    @action(detail=True, methods=['post'], url_path='activities')
    def log_activity(self, request, pk=None):
        """
        Logs a call, WhatsApp, trial, or custom note for this lead.
        """
        lead = self.get_object()
        activity = LeadActivity.objects.create(
            tenant=lead.tenant,
            lead=lead,
            activity_type=request.data.get('activity_type', 'Note'),
            summary=request.data.get('summary', ''),
            performed_by=request.user if request.user.is_authenticated else None,
        )
        return Response(LeadActivitySerializer(activity).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='pipeline')
    def pipeline_view(self, request):
        """
        Returns leads organized by pipeline stages for Kanban boards.
        """
        qs = self.get_queryset()
        pipeline_data = {}
        for stage_code, stage_label in LeadStage.choices:
            stage_leads = qs.filter(stage=stage_code)
            pipeline_data[stage_label] = LeadSerializer(stage_leads, many=True).data
        return Response(pipeline_data, status=status.HTTP_200_OK)
