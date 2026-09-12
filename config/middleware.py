"""
config/middleware.py — Platform-level middleware.

CorrelationIDMiddleware:
  Generates a unique correlation/request ID per HTTP request.
  Attaches it to:
    - request.correlation_id  -> available throughout the request lifecycle
    - X-Correlation-ID response header -> returned to API clients for tracing
  Must be placed BEFORE TenantDatabaseMiddleware in MIDDLEWARE setting.
"""

import uuid
import logging

logger = logging.getLogger(__name__)


class CorrelationIDMiddleware:
    """
    Injects a per-request correlation ID for tracing and audit trail.

    ID source priority:
      1. X-Correlation-ID request header (if caller provides one)
      2. X-Request-ID request header (common alternative)
      3. Generated UUID4

    The correlation ID is:
      - Stored on request.correlation_id
      - Echoed in X-Correlation-ID response header
      - Available in audit trail emission and structured logs
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. Resolve correlation_id: used for distributed request tracing
        incoming_corr = request.META.get('HTTP_X_CORRELATION_ID')
        if incoming_corr:
            try:
                correlation_id = str(uuid.UUID(incoming_corr))
            except (ValueError, AttributeError):
                correlation_id = str(uuid.uuid4())
                logger.debug(
                    'CorrelationIDMiddleware: Incoming X-Correlation-ID was not a valid UUID; '
                    'generated new ID: %s', correlation_id
                )
        else:
            correlation_id = str(uuid.uuid4())

        # 2. Resolve request_id: per-HTTP-request unique transaction ID
        incoming_req = request.META.get('HTTP_X_REQUEST_ID')
        if incoming_req:
            try:
                request_id = str(uuid.UUID(incoming_req))
            except (ValueError, AttributeError):
                request_id = str(uuid.uuid4())
                logger.debug(
                    'CorrelationIDMiddleware: Incoming X-Request-ID was not a valid UUID; '
                    'generated new ID: %s', request_id
                )
        else:
            request_id = str(uuid.uuid4())

        request.correlation_id = correlation_id
        request.request_id = request_id

        response = self.get_response(request)

        # Echo back tracing headers
        response['X-Correlation-ID'] = correlation_id
        response['X-Request-ID'] = request_id
        return response
