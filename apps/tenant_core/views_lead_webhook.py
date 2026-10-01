"""
apps/tenant_core/views_lead_webhook.py — Public Webhook for External Lead Capture.

Allows external websites (like the public SWEAT FIT frontend) to submit leads
directly into the tenant's CRM without requiring a staff JWT token.
"""

import json
import logging
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.services_crm import CRMLeadService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class PublicLeadCaptureView(APIView):
    """
    Public webhook receiver for website lead capture forms.
    Endpoint: /api/v1/webhooks/leads/<tenant_public_id>/
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_public_id: str):
        # 1. Resolve Tenant in Master DB
        tenant = self._resolve_tenant(tenant_public_id)
        if not tenant:
            return Response({'error': 'Invalid tenant identifier'}, status=status.HTTP_404_NOT_FOUND)

        # Parse request body
        try:
            if request.content_type == 'application/json' or request.body.startswith(b'{'):
                payload = json.loads(request.body.decode('utf-8'))
            else:
                payload = request.POST.dict()
        except Exception:
            payload = request.POST.dict() or {}

        # 2. Switch into Tenant's Dedicated Database Context
        with tenant_database_context(tenant.id) as db_alias:
            org = Organization.objects.using(db_alias).filter(status='ACTIVE').order_by('-created_at').first()
            if not org:
                return Response({'error': 'Tenant organization not found'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Extract lead details — all fields from the website form
            first_name = payload.get('first_name') or payload.get('firstName', '')
            last_name = payload.get('last_name') or payload.get('lastName', '')
            email = payload.get('email') or payload.get('emailAddress', '')
            phone = payload.get('phone') or payload.get('contactNumber', '')
            source_code = payload.get('lead_source', 'WEBSITE')

            # Optional enrichment fields
            gender = payload.get('gender') or None
            date_of_birth = payload.get('date_of_birth') or payload.get('birthday') or None
            fitness_goal = payload.get('fitness_goal') or payload.get('goal') or None
            area = payload.get('area') or payload.get('location_area') or None
            country = payload.get('country') or 'India'
            branch_name = payload.get('branch_name') or None
            program_name = payload.get('interested_in') or payload.get('program_name') or None
            pincode = payload.get('pincode') or None
            state = payload.get('state') or None

            # Build area string combining area + state + pincode if available
            area_parts = [p for p in [area, state, pincode] if p]
            full_area = ', '.join(area_parts) if area_parts else None

            if not first_name and not last_name:
                return Response({'error': 'Name is required'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                from apps.tenant_core.models_org import Branch
                from apps.tenant_core.models_crm import LeadSource
                from apps.tenant_core.models_catalog import Program

                # Resolve branch: by name from form, fallback to first active branch
                branch = None
                if branch_name:
                    branch = Branch.objects.using(db_alias).filter(
                        organization=org,
                        name__iexact=branch_name,
                        status='ACTIVE'
                    ).first()
                if not branch:
                    branch = Branch.objects.using(db_alias).filter(
                        organization=org, status='ACTIVE'
                    ).first()

                # Resolve program: by name from form, fallback to None (optional field)
                program = None
                if program_name and branch:
                    program = Program.objects.using(db_alias).filter(
                        name__iexact=program_name,
                        status='ACTIVE'
                    ).first()

                # Get or create the LeadSource by code
                lead_source, _ = LeadSource.objects.using(db_alias).get_or_create(
                    organization=org,
                    code=source_code,
                    defaults={'name': 'Website', 'source_type': 'WEBSITE', 'status': 'ACTIVE'}
                )

                # Build extra_fields — only include non-None values
                extra_fields = {}
                if gender:
                    extra_fields['gender'] = gender
                if date_of_birth:
                    extra_fields['date_of_birth'] = date_of_birth
                if fitness_goal:
                    extra_fields['fitness_goal'] = fitness_goal
                if full_area:
                    extra_fields['area'] = full_area
                if country:
                    extra_fields['country'] = country
                if program:
                    extra_fields['interested_program'] = program

                lead = CRMLeadService.create_lead(
                    organization=org,
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    email=email,
                    branch=branch,
                    lead_source=lead_source,
                    assigned_sales_user=None,  # Auto-assigned by CRM policy
                    actor_user=None,            # System-created via website
                    extra_fields=extra_fields,
                    db_alias=db_alias,
                )

                return Response({
                    'status': 'success',
                    'lead_id': str(lead.id),
                }, status=status.HTTP_201_CREATED)

            except Exception as e:
                logger.exception("Error capturing public lead: %s", e)
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def _resolve_tenant(self, public_id: str) -> Tenant | None:
        """
        Resolves tenant by UUID or slug in Master DB.
        """
        tenant = None
        try:
            tenant = Tenant.objects.using('default').filter(id=public_id).first()
        except Exception:
            pass

        if not tenant:
            tenant = Tenant.objects.using('default').filter(
                Q(slug=public_id) | Q(code=public_id)
            ).first()

        if tenant and tenant.is_accessible:
            return tenant
        return None
