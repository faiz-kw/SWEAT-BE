"""
Webhook Receivers and API endpoints for the Integration Layer.
Processes inbound Facebook Lead Ads and Door Turnstile QR access scans.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from drf_spectacular.utils import extend_schema, OpenApiTypes
import uuid

from .adapters.facebook_ads_adapter import FacebookLeadAdsAdapter
from .adapters.access_control_adapter import AccessControlAdapter
from apps.crm.models import Lead, LeadStage, LeadStatus, LeadActivity
from apps.members.models import Member, Attendance
from apps.operations.models import Booking, BookingStatus
from apps.tenants.models import Tenant, Location

class FacebookLeadWebhookView(APIView):
    """
    Public webhook receiver for Facebook Lead Ads.
    Normalizes lead data and inserts into CRM pipeline.
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Facebook Lead Ads Ingestion Webhook",
        description="Public endpoint receiving leads from Meta Lead Ads campaigns.",
        responses={201: OpenApiTypes.OBJECT}
    )
    def post(self, request):
        adapter = FacebookLeadAdsAdapter()
        parsed_lead = adapter.parse_lead_webhook(request.data)

        tenant = Tenant.objects.first()
        if not tenant:
            return Response({"error": "No tenant configured."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        location = Location.objects.filter(tenant=tenant).first()

        lead_id = f"LED-FB-{uuid.uuid4().hex[:6].upper()}"
        lead = Lead.objects.create(
            id=lead_id,
            tenant=tenant,
            location=location,
            name=parsed_lead['name'],
            phone=parsed_lead['phone'],
            email=parsed_lead.get('email', ''),
            source=parsed_lead.get('source', 'Meta Ads'),
            interested_service=parsed_lead.get('interested_service', 'Strength Training'),
            goal=parsed_lead.get('goal', 'General Fitness'),
            notes=parsed_lead.get('notes', ''),
            stage=LeadStage.NEW,
            status=LeadStatus.OPEN,
            score=75,
        )

        LeadActivity.objects.create(
            tenant=tenant,
            lead=lead,
            activity_type='Note',
            summary=f"Inbound lead captured via Facebook Lead Ads."
        )


        return Response({
            'success': True,
            'lead_id': lead.id,
            'name': lead.name,
            'stage': lead.stage,
        }, status=status.HTTP_201_CREATED)


class AccessControlScanWebhookView(APIView):
    """
    Webhook receiver for Physical Door Turnstiles & Access Hardware.
    Validates scanned time-boxed QR code, opens door, and logs attendance.
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Door Access Turnstile Scan Webhook",
        description="Receives QR scan from physical door hardware to unlock gate and log member attendance.",
        responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request):
        qr_token = request.data.get('qr_token')
        device_id = request.data.get('device_id', 'DOOR-INDIRANAGAR-01')
        location_id = request.data.get('location_id', 'LOC-002')

        adapter = AccessControlAdapter()
        val_result = adapter.validate_door_scan(qr_token, device_id=device_id)

        if not val_result['access_granted']:
            return Response({
                'unlock': False,
                'status': 'DENIED',
                'reason': val_result.get('reason', 'Access denied'),
            }, status=status.HTTP_403_FORBIDDEN)

        member_id = val_result['member_id']
        booking_id = val_result.get('booking_id')

        member = Member.objects.filter(id=member_id).first()
        if not member:
            return Response({'unlock': False, 'status': 'DENIED', 'reason': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)

        loc = Location.objects.filter(id=location_id, tenant=member.tenant).first() or member.location

        # 1. Log Member Attendance
        attendance = Attendance.objects.create(
            tenant=member.tenant,
            member=member,
            location=loc,
            method='QR Code'
        )

        # 2. If booking ID is provided, mark Booking as Attended
        if booking_id:
            Booking.objects.filter(id=booking_id, tenant=member.tenant).update(status=BookingStatus.ATTENDED)

        return Response({
            'unlock': True,
            'status': 'GRANTED',
            'member_id': member.id,
            'member_name': member.name,
            'location': loc.name,
            'attendance_id': attendance.id,
            'message': f"Welcome {member.name}! Door unlocked.",
        }, status=status.HTTP_200_OK)


class IntegrationStatusView(APIView):
    """
    Returns configured status of all 6 Integration Layer adapters.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            'integration_layer': 'Active',
            'adapters': {
                'razorpay': {'name': 'Razorpay Payments', 'status': 'Configured', 'purpose': 'Billing & Checkout'},
                'gupshup': {'name': 'Gupshup WhatsApp', 'status': 'Configured', 'purpose': 'Booking & CRM Messaging'},
                'telecmi': {'name': 'TeleCMI Telephony', 'status': 'Configured', 'purpose': 'Lead Calling & AI Voice'},
                'ses': {'name': 'Amazon SES', 'status': 'Configured', 'purpose': 'Transactional Email'},
                'facebook_ads': {'name': 'Facebook Lead Ads', 'status': 'Active', 'purpose': 'Inbound Lead Webhooks'},
                'access_control': {'name': 'Door Access Control', 'status': 'Active', 'purpose': 'Turnstile Hardware QR Scan'},
            }
        }, status=status.HTTP_200_OK)
