"""
Vobiz Dial callbackUrl receiver.

Vobiz posts real-time B-leg events (answer, connected, digits, hangup) to the Dial
callbackUrl. Each event describes one agent leg, not the whole call, so this view only
logs. Closing a CallSession stays with /telephony/hangup/ (A-leg end) and
/telephony/dial-fallback/ (final Dial result).
"""

import logging

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.tenant_core.serializers_telephony import VobizDialEventSerializer

logger = logging.getLogger(__name__)

_EMPTY_XML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


@method_decorator(csrf_exempt, name="dispatch")
class VobizDialEventView(APIView):
    """
    POST /api/v1/telephony/dial-events/

    Public: Vobiz calls it server-to-server without credentials, like the other Vobiz
    webhooks. It does no DB reads or writes, so a forged request can only add a log line.
    It has its own throttle scope so carrier callbacks don't use up the shared anon quota.
    Vobiz signature verification does not exist for any webhook yet (tracked separately).
    """

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "telephony_dial_events"

    def post(self, request: Request) -> HttpResponse:
        data = request.data.copy()
        session_id = request.query_params.get("session_id")
        if session_id:
            data["session_id"] = session_id

        serializer = VobizDialEventSerializer(data=data)
        if not serializer.is_valid():
            logger.warning("Vobiz dial event ignored: invalid fields %s", sorted(serializer.errors))
            return HttpResponse(_EMPTY_XML, content_type="application/xml; charset=utf-8")

        event = serializer.validated_data
        logger.info(
            "Vobiz dial event: session=%s CallUUID=%s DialAction=%s BLegStatus=%s "
            "BLegUUID=%s BLegDuration=%s HangupCause=%s",
            event.get("session_id"),
            event.get("CallUUID", ""),
            event.get("DialAction", ""),
            event.get("DialBLegStatus", ""),
            event.get("DialBLegUUID", ""),
            event.get("DialBLegDuration", ""),
            event.get("DialBLegHangupCause") or event.get("HangupCause", ""),
        )
        return HttpResponse(_EMPTY_XML, content_type="application/xml; charset=utf-8")
