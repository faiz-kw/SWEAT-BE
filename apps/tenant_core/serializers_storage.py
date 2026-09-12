"""
Serializers for Tenant File Subsystem (Sprint 7 Phase 7D).
Provides validation for presigned upload requests, confirm upload requests,
and safe serialization of File metadata.
"""

import os
import re
from typing import Any, Dict

from django.conf import settings
from rest_framework import serializers

from .models_infra import File
from .storage import (
    ALLOWED_CLASSIFICATIONS,
    ALLOWED_ENTITY_TYPES,
    FILENAME_TRAVERSAL_REGEX,
)

HEX_CHECKSUM_REGEX = re.compile(r'^[a-fA-F0-9]{32,64}$')


class PresignedUploadRequestSerializer(serializers.Serializer):
    """Validates parameters for generating a presigned S3 PUT URL."""
    original_filename = serializers.CharField(max_length=500, required=True)
    mime_type = serializers.CharField(max_length=200, required=True)
    file_size = serializers.IntegerField(min_value=1, required=True)
    entity_type = serializers.CharField(max_length=100, required=True)
    entity_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    classification = serializers.ChoiceField(
        choices=File.CLASSIFICATION,
        default='INTERNAL',
        required=False,
    )
    checksum = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
        default='',
    )
    checksum_algorithm = serializers.ChoiceField(
        choices=['MD5', 'SHA-256'],
        default='MD5',
        required=False,
    )

    def validate_original_filename(self, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise serializers.ValidationError("original_filename cannot be empty.")
        if FILENAME_TRAVERSAL_REGEX.search(trimmed) or '/' in trimmed or '\\' in trimmed:
            raise serializers.ValidationError("original_filename contains invalid directory traversal characters.")
        clean_name = os.path.basename(trimmed)
        if not clean_name:
            raise serializers.ValidationError("original_filename is invalid.")
        return clean_name

    def validate_mime_type(self, value: str) -> str:
        trimmed = value.strip()
        if not trimmed or '/' not in trimmed:
            raise serializers.ValidationError("mime_type must be a valid MIME type format (e.g. 'application/pdf').")
        return trimmed

    def validate_file_size(self, value: int) -> int:
        max_size = getattr(settings, 'ZATA_S3_MAX_FILE_SIZE', 50 * 1024 * 1024)
        if value > max_size:
            raise serializers.ValidationError(
                f"Declared file_size ({value} bytes) exceeds maximum allowed size ({max_size} bytes)."
            )
        return value

    def validate_entity_type(self, value: str) -> str:
        trimmed = value.strip()
        if trimmed not in ALLOWED_ENTITY_TYPES:
            raise serializers.ValidationError(
                f"Invalid entity_type '{trimmed}'. Allowed types: {sorted(ALLOWED_ENTITY_TYPES)}"
            )
        return trimmed

    def validate_classification(self, value: str) -> str:
        upper = value.strip().upper()
        if upper not in ALLOWED_CLASSIFICATIONS:
            raise serializers.ValidationError(
                f"Invalid classification '{value}'. Allowed: {sorted(ALLOWED_CLASSIFICATIONS)}"
            )
        return upper

    def validate_checksum(self, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not HEX_CHECKSUM_REGEX.match(trimmed):
            raise serializers.ValidationError("checksum must be a valid hexadecimal hash string.")
        return trimmed


class ConfirmUploadRequestSerializer(serializers.Serializer):
    """Validates parameters for confirming a completed S3 upload."""
    file_id = serializers.UUIDField(required=True)
    checksum = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
        default='',
    )
    checksum_algorithm = serializers.ChoiceField(
        choices=['MD5', 'SHA-256'],
        default='MD5',
        required=False,
    )

    def validate_checksum(self, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not HEX_CHECKSUM_REGEX.match(trimmed):
            raise serializers.ValidationError("checksum must be a valid hexadecimal hash string.")
        return trimmed


class FileMetadataSerializer(serializers.ModelSerializer):
    """
    Safe public metadata representation for tenant Files.
    Never exposes internal S3 credentials, bucket name, database aliases, or infrastructure paths.
    """
    uploaded_by_id = serializers.UUIDField(source='uploaded_by.id', read_only=True, allow_null=True)
    uploaded_by_email = serializers.EmailField(source='uploaded_by.email', read_only=True, allow_null=True)

    class Meta:
        model = File
        fields = [
            'id',
            'original_filename',
            'mime_type',
            'file_size',
            'checksum',
            'classification',
            'entity_type',
            'entity_id',
            'uploaded_by_id',
            'uploaded_by_email',
            'uploaded_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields
