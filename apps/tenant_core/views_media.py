"""
apps/tenant_core/views_media.py — Protected delivery of uploaded media under /media/.

Access is granted when either:
1. the URL carries a valid, unexpired signature issued for that exact file, or
2. the caller is authenticated and passes the ownership check for the file.
"""
import mimetypes

from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services_media import (
    ASSESSMENT_MEDIA_PREFIX,
    is_valid_media_signature,
    resolve_media_file,
    user_can_read_assessment_file,
)


class ProtectedMediaView(APIView):
    """
    GET /media/<path>
    """
    # AllowAny only so <img src> requests (which cannot send a bearer token) can use a signed URL.
    # Every request is still checked in get(): signature or authenticated ownership.
    permission_classes = [AllowAny]

    def get(self, request, path):
        full_path = resolve_media_file(path)
        if full_path is None:
            raise Http404

        if not is_valid_media_signature(request.query_params.get('sig'), path):
            user = request.user
            if not user or not user.is_authenticated:
                return Response(
                    {'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED
                )
            if path.startswith(ASSESSMENT_MEDIA_PREFIX) and not user_can_read_assessment_file(user, path):
                raise Http404

        response = FileResponse(
            open(full_path, 'rb'),
            content_type=mimetypes.guess_type(full_path)[0] or 'application/octet-stream',
        )
        response['X-Content-Type-Options'] = 'nosniff'
        response['Cache-Control'] = 'private, max-age=300'
        return response
