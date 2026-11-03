"""
views_import_engine.py

API ViewSet for Universal Excel Upload Engine.
Provides non-destructive detection, pre-flight preview simulation,
and atomic batch commit.
"""

import logging
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .context import get_tenant_db_alias
from .models_org import Organization
from .services_import_engine import UniversalImportEngine, EntityType

logger = logging.getLogger(__name__)


def _get_db(request) -> str:
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if org:
        return org
    user = getattr(request, 'user', None)
    if user and hasattr(user, 'organization') and user.organization:
        return user.organization
    alias = _get_db(request)
    return Organization.objects.using(alias).filter(status='ACTIVE').first()


class UniversalImportViewSet(viewsets.ViewSet):
    """
    Universal Excel / CSV Import Controller.
    Endpoints:
    - POST /excel-imports/detect/
    - POST /excel-imports/preview/
    - POST /excel-imports/confirm/
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'], url_path='detect')
    def detect(self, request):
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        if not uploaded_file:
            return Response(
                {'error': 'No file provided. Upload an Excel (.xlsx) or CSV file under "file".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            import openpyxl
            import io
            content = uploaded_file.read()
            uploaded_file.seek(0)
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            from .services_import_engine import EntityClassifier
            classification = EntityClassifier.classify_workbook(wb)
            return Response(classification, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to classify uploaded workbook: %s", exc, exc_info=True)
            return Response(
                {'error': f'Failed to process file: {str(exc)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=['post'], url_path='preview')
    def preview(self, request):
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        if not uploaded_file:
            return Response(
                {'error': 'No file provided. Upload an Excel (.xlsx) or CSV file under "file".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_entity = request.data.get('target_entity') or None
        target_month = request.data.get('target_month') or request.data.get('month') or None
        alias = _get_db(request)
        org = _get_org(request)

        try:
            preview_result = UniversalImportEngine.process_file_preview(
                uploaded_file,
                target_entity=target_entity,
                target_month=target_month,
                org=org,
                db_alias=alias,
            )
            return Response(preview_result, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Preview simulation failed: %s", exc, exc_info=True)
            return Response(
                {'error': f'Preview failed: {str(exc)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=['post'], url_path='confirm')
    def confirm(self, request):
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        if not uploaded_file:
            return Response(
                {'error': 'No file provided. Upload an Excel (.xlsx) or CSV file under "file".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entity_type = request.data.get('entity_type')
        if not entity_type:
            return Response(
                {'error': 'entity_type is required (e.g., "WOD_VIDEO", "CLASS_SCHEDULE").'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        options = request.data.get('options') or {}
        if isinstance(options, str):
            import json
            try:
                options = json.loads(options)
            except Exception:
                options = {}
        target_month = request.data.get('target_month') or request.data.get('month')
        if target_month and 'target_month' not in options:
            options['target_month'] = target_month
        alias = _get_db(request)
        org = _get_org(request)
        user = getattr(request, 'user', None)

        curated_rows = request.data.get('curated_rows') or request.data.get('rows')
        if isinstance(curated_rows, str):
            import json
            try:
                curated_rows = json.loads(curated_rows)
            except Exception:
                curated_rows = None

        try:
            if curated_rows and isinstance(curated_rows, list) and len(curated_rows) > 0:
                result = UniversalImportEngine.commit_curated_rows(
                    curated_rows,
                    entity_type=entity_type,
                    org=org,
                    db_alias=alias,
                    target_month=target_month,
                    user=user,
                    uploaded_file=uploaded_file,
                )
            else:
                result = UniversalImportEngine.execute_confirmed_import(
                    uploaded_file,
                    entity_type=entity_type,
                    options=options,
                    org=org,
                    db_alias=alias,
                    user=user,
                )
            return Response(result, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Confirmed import failed: %s", exc, exc_info=True)
            return Response(
                {'error': f'Import execution failed: {str(exc)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

