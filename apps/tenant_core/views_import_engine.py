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

    @action(detail=False, methods=['post'], url_path='simulate-occurrences')
    def simulate_occurrences(self, request):
        """
        Intermediate CRM Preview Step:
        Simulates concrete calendar occurrences from extracted schedule rules.
        Automatically resolves template defaults for 'NA' values (capacity, duration, room, etc.)
        and returns searchable occurrences with branch and template mappings.
        """
        alias = _get_db(request)
        org = _get_org(request)
        target_month = request.data.get('target_month') or request.data.get('month')

        curated_rules = request.data.get('curated_rules') or request.data.get('rules')
        if isinstance(curated_rules, str):
            import json
            try:
                curated_rules = json.loads(curated_rules)
            except Exception:
                curated_rules = None

        if not curated_rules:
            # Fallback: if rules not provided, try to extract from uploaded file
            uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
            if uploaded_file:
                preview_result = UniversalImportEngine.process_file_preview(
                    uploaded_file,
                    target_entity='CLASS_SCHEDULE',
                    target_month=target_month,
                    org=org,
                    db_alias=alias,
                )
                curated_rules = preview_result.get('rows', [])

        if not curated_rules:
            return Response(
                {'error': 'No schedule rules provided to simulate occurrences.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sim_result = UniversalImportEngine.simulate_class_occurrences(
                rules=curated_rules,
                target_month=target_month,
                org=org,
                db_alias=alias,
            )
            return Response(sim_result, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to simulate occurrences: %s", exc, exc_info=True)
            return Response(
                {'error': f'Occurrences simulation failed: {str(exc)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )


    @action(detail=False, methods=['post'], url_path='simulate-trainer-shifts')
    def simulate_trainer_shifts(self, request):
        """
        Intermediate CRM Preview Step for Trainer Shift Rosters:
        Simulates concrete trainer shift assignments from extracted roster rows.
        Resolves trainer profiles, branches, shift hours, and prevents duplicates.
        """
        alias = _get_db(request)
        org = _get_org(request)
        target_month = request.data.get('target_month') or request.data.get('month')

        curated_rows = request.data.get('curated_rows') or request.data.get('rows')
        if isinstance(curated_rows, str):
            import json
            try:
                curated_rows = json.loads(curated_rows)
            except Exception:
                curated_rows = None

        if not curated_rows:
            uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
            if uploaded_file:
                preview_result = UniversalImportEngine.process_file_preview(
                    uploaded_file,
                    target_entity='TRAINER_ROSTER',
                    target_month=target_month,
                    org=org,
                    db_alias=alias,
                )
                curated_rows = preview_result.get('rows', [])

        if not curated_rows:
            return Response(
                {'error': 'No trainer roster data provided for simulation.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sim_result = UniversalImportEngine.simulate_trainer_shifts(
            rows=curated_rows,
            target_month=target_month,
            org=org,
            db_alias=alias,
        )

        return Response(sim_result, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='simulate-workout-videos')
    def simulate_workout_videos(self, request):
        """
        Intermediate CRM Preview Step for Class Workout Videos:
        Simulates and normalizes daily workout video items before publishing.
        Resolves YouTube embeds, category templates, muscle tags, and detects
        matching calendar class occurrences for automatic linking.
        """
        alias = _get_db(request)
        org = _get_org(request)
        target_month = request.data.get('target_month') or request.data.get('month')

        curated_rows = request.data.get('curated_rows') or request.data.get('rows')
        if isinstance(curated_rows, str):
            import json
            try:
                curated_rows = json.loads(curated_rows)
            except Exception:
                curated_rows = None

        if not curated_rows:
            uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
            if uploaded_file:
                preview_result = UniversalImportEngine.process_file_preview(
                    uploaded_file,
                    target_entity='WOD_VIDEO',
                    target_month=target_month,
                    org=org,
                    db_alias=alias,
                )
                curated_rows = preview_result.get('rows', [])

        if not curated_rows:
            return Response(
                {'error': 'No workout video rows provided to simulate.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = UniversalImportEngine.simulate_workout_videos(
                rows=curated_rows,
                target_month=target_month,
                org=org,
                db_alias=alias,
            )
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Failed to simulate workout videos: %s", e, exc_info=True)
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='confirm')
    def confirm(self, request):
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        curated_occurrences = request.data.get('curated_occurrences') or request.data.get('occurrences')
        if isinstance(curated_occurrences, str):
            import json
            try:
                curated_occurrences = json.loads(curated_occurrences)
            except Exception:
                curated_occurrences = None

        curated_trainer_shifts = request.data.get('curated_trainer_shifts') or request.data.get('shifts')
        if isinstance(curated_trainer_shifts, str):
            import json
            try:
                curated_trainer_shifts = json.loads(curated_trainer_shifts)
            except Exception:
                curated_trainer_shifts = None

        curated_workout_videos = request.data.get('curated_workout_videos') or request.data.get('videos')
        if isinstance(curated_workout_videos, str):
            import json
            try:
                curated_workout_videos = json.loads(curated_workout_videos)
            except Exception:
                curated_workout_videos = None

        curated_rows = request.data.get('curated_rows') or request.data.get('rows')
        if isinstance(curated_rows, str):
            import json
            try:
                curated_rows = json.loads(curated_rows)
            except Exception:
                curated_rows = None

        if not uploaded_file and not curated_occurrences and not curated_rows and not curated_trainer_shifts and not curated_workout_videos:
            return Response(
                {'error': 'No file or curated data provided for import confirmation.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entity_type = request.data.get('entity_type') or (
            'CLASS_SCHEDULE' if curated_occurrences else (
                'TRAINER_ROSTER' if curated_trainer_shifts else (
                    'WOD_VIDEO' if curated_workout_videos else None
                )
            )
        )
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

        try:
            if curated_workout_videos and isinstance(curated_workout_videos, list) and len(curated_workout_videos) > 0:
                result = UniversalImportEngine.commit_curated_workout_videos(
                    videos=curated_workout_videos,
                    org=org,
                    db_alias=alias,
                    user=user,
                    target_month=target_month,
                )
            elif curated_trainer_shifts and isinstance(curated_trainer_shifts, list) and len(curated_trainer_shifts) > 0:
                result = UniversalImportEngine.commit_curated_trainer_shifts(
                    shifts=curated_trainer_shifts,
                    org=org,
                    db_alias=alias,
                    user=user,
                    target_month=target_month,
                )
            elif curated_occurrences and isinstance(curated_occurrences, list) and len(curated_occurrences) > 0:
                result = UniversalImportEngine.commit_curated_occurrences(
                    occurrences=curated_occurrences,
                    org=org,
                    db_alias=alias,
                    user=user,
                )
            elif curated_rows and isinstance(curated_rows, list) and len(curated_rows) > 0:
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

