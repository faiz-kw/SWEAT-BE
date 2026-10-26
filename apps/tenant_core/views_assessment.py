"""
Assessment & Body Composition API Views.
Placeholder endpoints to support assessment routing in urls.py and urls_mobile.py.
"""

from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated


class AssessmentSessionViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        return Response([])

    def retrieve(self, request, pk=None):
        return Response({"id": str(pk), "status": "PENDING"})

    def create(self, request):
        return Response({"status": "CREATED"}, status=status.HTTP_201_CREATED)

    def capture_photos(self, request, pk=None):
        return Response({"status": "CAPTURED", "session_id": str(pk)})

    def approve_and_send(self, request, pk=None):
        return Response({"status": "APPROVED", "session_id": str(pk)})


class AssessmentEligibilityMobileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"eligible": True, "message": "Assessment entitlement active"})


class StudioScreenPairingView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response({"paired": True, "code": "SWEAT-SCREEN-01"})
