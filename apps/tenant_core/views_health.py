"""
apps/tenant_core/views_health.py - Platform Liveness & Readiness Probes.

Architectural Guarantees:
- Liveness (/health/): Verifies Django HTTP server process is running and accepting traffic.
  Zero external or database dependencies.
- Readiness (/ready/): Verifies primary PostgreSQL Master DB and Redis cache/broker connectivity.
  Fails with HTTP 503 if infrastructure is unreachable.
- Zero dependency on third-party APIs (Meta, Razorpay, etc.) for core platform liveness/readiness.
- Publicly accessible by orchestrators, load balancers, and monitoring agents without authentication.
"""
import time
import logging
from django.db import connections
from django.core.cache import cache
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

logger = logging.getLogger(__name__)


class HealthLivenessView(APIView):
    """
    Liveness probe.
    Returns HTTP 200 if Django HTTP process is alive.
    Does NOT query DB, cache, or external APIs.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'status': 'UP',
            'service': 'performance-os-backend',
            'timestamp': timezone.now().isoformat(),
        }, status=status.HTTP_200_OK)


class HealthReadinessView(APIView):
    """
    Readiness probe.
    Verifies database and Redis broker/cache connectivity.
    Returns HTTP 200 when ready to accept user requests, HTTP 503 otherwise.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        all_ok = True

        # 1. Database Connectivity (Master DB)
        t0 = time.time()
        try:
            conn = connections['default']
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
            db_latency_ms = max(1, int((time.time() - t0) * 1000))
            checks['database'] = {'status': 'HEALTHY', 'latency_ms': db_latency_ms}
        except Exception as exc:
            all_ok = False
            logger.error("Readiness check database failure: %s", exc)
            checks['database'] = {'status': 'UNHEALTHY', 'error': str(exc)[:100]}

        # 2. Redis Cache / Broker Connectivity
        t0 = time.time()
        try:
            cache_key = '_readiness_probe'
            cache.set(cache_key, '1', timeout=5)
            val = cache.get(cache_key)
            if val != '1':
                raise ValueError("Cache readback failed")
            redis_latency_ms = max(1, int((time.time() - t0) * 1000))
            checks['redis'] = {'status': 'HEALTHY', 'latency_ms': redis_latency_ms}
        except Exception as exc:
            all_ok = False
            logger.warning("Readiness check redis failure: %s", exc)
            checks['redis'] = {'status': 'UNHEALTHY', 'error': str(exc)[:100]}

        http_status = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response({
            'status': 'READY' if all_ok else 'NOT_READY',
            'checks': checks,
            'timestamp': timezone.now().isoformat(),
        }, status=http_status)
