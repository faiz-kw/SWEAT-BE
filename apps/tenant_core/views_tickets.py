import uuid
import logging
from django.utils import timezone
from django.db import models
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .context import get_tenant_db_alias
from .models_tickets import SupportTicket
from .serializers_tickets import SupportTicketSerializer
from .permissions import RequireActiveTenantAndOrg

logger = logging.getLogger(__name__)


class TicketPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        from .models_org import Organization
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


class SupportTicketViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for managing Member Support Tickets (Passport requests, billing, inquiries).
    Supports comprehensive search, status/category/priority/branch filtering, and resolution workflows.
    """
    serializer_class = SupportTicketSerializer
    permission_classes = [RequireActiveTenantAndOrg]
    pagination_class = TicketPagination

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = (
            SupportTicket.objects.using(alias)
            .filter(organization=org)
            .select_related('branch', 'user_profile__user', 'assigned_to', 'resolved_by')
            .order_by('-created_at')
        )

        params = self.request.query_params

        # 1. Status Filter
        status_param = params.get('status')
        if status_param and status_param.upper() != 'ALL':
            if ',' in status_param:
                statuses = [s.strip().upper() for s in status_param.split(',')]
                qs = qs.filter(status__in=statuses)
            else:
                qs = qs.filter(status=status_param.upper())

        # 2. Category Filter
        category_param = params.get('category')
        if category_param and category_param.upper() != 'ALL':
            qs = qs.filter(category=category_param.upper())

        # 3. Priority Filter
        priority_param = params.get('priority')
        if priority_param and priority_param.upper() != 'ALL':
            qs = qs.filter(priority=priority_param.upper())

        # 4. Branch Filter
        branch_id = params.get('branch_id')
        if branch_id and branch_id.lower() != 'all':
            qs = qs.filter(branch_id=branch_id)

        # 5. Member / Profile Filter
        member_id = params.get('member_id') or params.get('user_profile_id')
        if member_id:
            qs = qs.filter(user_profile_id=member_id)

        # 6. Search Query (Ticket #, Subject, Member Name, Phone)
        search_query = params.get('search')
        if search_query:
            sq = search_query.strip()
            qs = qs.filter(
                models.Q(ticket_number__icontains=sq) |
                models.Q(subject__icontains=sq) |
                models.Q(user_profile__user__first_name__icontains=sq) |
                models.Q(user_profile__user__last_name__icontains=sq) |
                models.Q(user_profile__user__phone__icontains=sq) |
                models.Q(user_profile__first_name_snapshot__icontains=sq) |
                models.Q(user_profile__last_name_snapshot__icontains=sq)
            )

        return qs

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        date_str = timezone.now().strftime('%Y%m%d')
        unique_tkt_num = f"TKT-{date_str}-{uuid.uuid4().hex[:6].upper()}"

        user_profile = serializer.validated_data.get('user_profile')
        branch = serializer.validated_data.get('branch')
        if not branch and user_profile:
            branch = getattr(user_profile, 'preferred_branch', None) or getattr(getattr(user_profile, 'user', None), 'home_branch', None)

        serializer.save(
            organization=org,
            ticket_number=unique_tkt_num,
            branch=branch,
        )

    def perform_update(self, serializer):
        instance = serializer.instance
        new_status = serializer.validated_data.get('status', instance.status)
        update_kwargs = {}

        if new_status in ('RESOLVED', 'CLOSED') and not instance.resolved_at:
            update_kwargs['resolved_at'] = timezone.now()
            update_kwargs['resolved_by'] = self.request.user if getattr(self.request.user, 'is_authenticated', False) else None
        elif new_status in ('OPEN', 'IN_PROGRESS'):
            update_kwargs['resolved_at'] = None
            update_kwargs['resolved_by'] = None

        serializer.save(**update_kwargs)

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve_ticket(self, request, pk=None):
        """Mark a ticket as resolved with resolution notes."""
        alias = _get_db(request)
        ticket = self.get_object()
        notes = request.data.get('resolution_notes', '')

        ticket.status = 'RESOLVED'
        ticket.resolution_notes = notes or ticket.resolution_notes
        ticket.resolved_at = timezone.now()
        ticket.resolved_by = request.user if getattr(request.user, 'is_authenticated', False) else None
        ticket.save(using=alias, update_fields=['status', 'resolution_notes', 'resolved_at', 'resolved_by', 'updated_at'])

        serializer = self.get_serializer(ticket)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='metrics')
    def metrics(self, request):
        """Summary metrics for the ticket dashboard."""
        alias = _get_db(request)
        org = _get_org(request)
        qs = SupportTicket.objects.using(alias).filter(organization=org)

        now = timezone.now()
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total_open = qs.filter(status='OPEN').count()
        total_in_progress = qs.filter(status='IN_PROGRESS').count()
        resolved_today = qs.filter(status='RESOLVED', resolved_at__gte=start_of_today).count()
        passport_requests = qs.filter(category='PASSPORT_PURCHASE', status__in=['OPEN', 'IN_PROGRESS']).count()

        return Response({
            'total_open': total_open,
            'total_in_progress': total_in_progress,
            'resolved_today': resolved_today,
            'passport_requests_pending': passport_requests,
            'total_tickets': qs.count(),
        }, status=status.HTTP_200_OK)
