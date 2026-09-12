"""
Management command to migrate legacy avatar_url strings to canonical profile_file_id File records.

Safety rules:
- HTTPS validation only.
- SSRF/private-network protection (blocks loopback, private, link-local, multicast, reserved IPs).
- Redirect validation: validates destination IP on every redirect hop.
- Bounded timeout (10 seconds).
- 5 MB maximum response size.
- Image content magic-byte verification (JPEG, PNG, GIF, WEBP).
- SHA-256 integrity checksum.
- S3 upload via ZataS3StorageService.
- File model row creation using actual schema (no invented fields).
- profile_file_id assignment only after successful object and row creation.
- Cleanup of orphaned S3 objects on partial failure.
- Idempotent: skips users already having profile_file_id.
- avatar_url retained as legacy fallback.
- SYSTEM_JOB audit logging.
"""

import hashlib
import ipaddress
import logging
import socket
import urllib.parse
import uuid
from typing import Optional, Tuple

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.master.models_infra import TenantDataSource
from apps.master.models_tenant import Tenant
from apps.tenant_core.models_infra import File
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.storage import ZataS3StorageService, StorageError
from config.tenant_middleware import _register_tenant_connection

logger = logging.getLogger('apps.tenant_core.avatar_migration')

MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB
REQUEST_TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3

IMAGE_SIGNATURES = [
    (b'\x89PNG\r\n\x1a\n', 'image/png', '.png'),
    (b'\xff\xd8\xff', 'image/jpeg', '.jpg'),
    (b'GIF87a', 'image/gif', '.gif'),
    (b'GIF89a', 'image/gif', '.gif'),
    (b'RIFF', 'image/webp', '.webp'),  # Check WEBP at offset 8
]


def is_private_ip(ip_str: str) -> bool:
    """Check whether an IP string is private, loopback, link-local, multicast, or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_loopback or
            ip.is_private or
            ip.is_link_local or
            ip.is_multicast or
            ip.is_reserved
        )
    except ValueError:
        return True


def validate_url_and_ip(url: str) -> Tuple[str, str]:
    """
    Validates URL scheme and ensures destination IP is not a private/loopback/internal address (SSRF protection).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != 'https':
        raise ValueError(f"Insecure scheme '{parsed.scheme}': only HTTPS URLs are permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no valid hostname.")

    try:
        addr_info = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Failed to resolve hostname '{hostname}': {exc}") from exc

    resolved_ips = {item[4][0] for item in addr_info}
    for ip_str in resolved_ips:
        if is_private_ip(ip_str):
            raise ValueError(f"SSRF violation: hostname '{hostname}' resolves to internal/private IP {ip_str}.")

    return parsed.scheme, hostname


def validate_image_bytes(content: bytes) -> Tuple[str, str]:
    """
    Validates image content using magic byte signatures.
    Returns (mime_type, extension).
    """
    if len(content) < 8:
        raise ValueError("File content is too small to be a valid image.")

    for sig, mime, ext in IMAGE_SIGNATURES:
        if sig == b'RIFF':
            if content.startswith(b'RIFF') and len(content) > 12 and content[8:12] == b'WEBP':
                return mime, ext
        elif content.startswith(sig):
            return mime, ext

    raise ValueError("Content did not match any permitted image signature (PNG, JPEG, GIF, WEBP).")


def fetch_avatar_safely(url: str) -> Tuple[bytes, str, str]:
    """
    Fetches avatar with SSRF protection, bounded size, and redirect validation.
    """
    current_url = url
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS

    for hop in range(MAX_REDIRECTS + 1):
        validate_url_and_ip(current_url)

        response = session.get(
            current_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
            headers={'User-Agent': 'PerformanceOS-AvatarMigrator/1.0'}
        )

        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            redirect_target = response.headers.get('Location')
            if not redirect_target:
                raise ValueError("Redirect header missing Location.")
            current_url = urllib.parse.urljoin(current_url, redirect_target)
            continue

        if response.status_code != 200:
            raise ValueError(f"HTTP fetch failed with status code {response.status_code}")

        # Enforce max content length header if provided
        cl = response.headers.get('Content-Length')
        if cl and int(cl) > MAX_AVATAR_BYTES:
            raise ValueError(f"Avatar Content-Length ({cl} bytes) exceeds limit ({MAX_AVATAR_BYTES} bytes).")

        # Stream bounded bytes
        downloaded = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            downloaded.extend(chunk)
            if len(downloaded) > MAX_AVATAR_BYTES:
                raise ValueError(f"Avatar payload exceeded maximum size ({MAX_AVATAR_BYTES} bytes).")

        content = bytes(downloaded)
        mime_type, ext = validate_image_bytes(content)
        return content, mime_type, ext

    raise ValueError("Too many redirects during avatar ingestion.")


class Command(BaseCommand):
    help = "Idempotently ingests legacy avatar_url strings into canonical File records and assigns users.profile_file_id."

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, help='Specific tenant slug to process')
        parser.add_argument('--all', action='store_true', help='Process all active tenants')
        parser.add_argument('--dry-run', action='store_true', help='Scan and report without writing to S3 or DB')

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        process_all = options.get('all')
        dry_run = options.get('dry_run', False)

        if not process_all and not tenant_slug:
            self.stderr.write(self.style.ERROR("Specify --tenant=<slug> or --all"))
            return

        qs = TenantDataSource.objects.filter(status='ACTIVE')
        if tenant_slug:
            qs = qs.filter(tenant__slug=tenant_slug)

        data_sources = list(qs)
        tenant_targets = []
        if data_sources:
            for ds in data_sources:
                tenant = ds.tenant
                alias = f"tenant_{ds.db_name}"
                _register_tenant_connection(alias, ds.db_name)
                tenant_targets.append((alias, str(tenant.id), tenant.slug, ds.db_name))
        elif tenant_slug and (tenant_slug in getattr(settings, 'DATABASES', {})):
            # Direct database alias support (e.g. test environments)
            tenant_targets.append((tenant_slug, str(uuid.uuid4()), tenant_slug, tenant_slug))
        else:
            self.stdout.write("No active tenant data sources found.")
            return

        storage_service = None
        if not dry_run:
            try:
                storage_service = ZataS3StorageService()
            except Exception as exc:
                logger.warning("Storage service initialization deferred or credentials missing: %s", exc)

        total_users_checked = 0
        total_avatars_migrated = 0
        total_errors = 0

        for alias, tenant_id_str, tenant_slug_val, db_name_val in tenant_targets:
            self.stdout.write(f"\n--- Processing tenant: {tenant_slug_val} (db: {db_name_val}) ---")

            users_to_migrate = TenantUser.objects.using(alias).filter(
                profile_file_id__isnull=True
            ).exclude(avatar_url='')

            count = users_to_migrate.count()
            total_users_checked += count
            self.stdout.write(f"Users with unmigrated avatar_url: {count}")

            for user in users_to_migrate:
                avatar_url = user.avatar_url.strip()
                if not avatar_url:
                    continue

                if dry_run:
                    self.stdout.write(f"[DRY-RUN] Would migrate avatar for user {user.id} ({user.email}): {avatar_url}")
                    continue

                if not storage_service:
                    storage_service = ZataS3StorageService()

                file_id = uuid.uuid4()
                object_key = storage_service.generate_object_key(tenant_id_str, str(file_id))
                s3_uploaded = False

                try:
                    # 1. Safely fetch avatar payload with SSRF protection & validation
                    content, mime_type, ext = fetch_avatar_safely(avatar_url)
                    file_size = len(content)
                    sha256_hash = hashlib.sha256(content).hexdigest()

                    # 2. Upload to S3
                    storage_service.s3_client.put_object(
                        Bucket=storage_service.bucket_name,
                        Key=object_key,
                        Body=content,
                        ContentType=mime_type,
                    )
                    s3_uploaded = True

                    # 3. Create File row in tenant DB inside atomic transaction
                    with transaction.atomic(using=alias):
                        file_record = File.objects.using(alias).create(
                            id=file_id,
                            object_key=object_key,
                            bucket_name=storage_service.bucket_name,
                            original_filename=f"avatar_{user.id}{ext}",
                            mime_type=mime_type,
                            file_size=file_size,
                            checksum=sha256_hash,
                            classification='INTERNAL',
                            entity_type='TenantUser',
                            entity_id=user.id,
                            uploaded_by=None,
                            uploaded_at=timezone.now(),
                        )

                        # 4. Assign profile_file_id only after File row is successfully created
                        user.profile_file_id = file_record
                        user.save(update_fields=['profile_file_id'], using=alias)

                    total_avatars_migrated += 1
                    logger.info(
                        "SYSTEM_JOB: Successfully migrated avatar for tenant=%s user=%s file=%s key=%s",
                        tenant_slug_val, user.id, file_id, object_key
                    )
                    self.stdout.write(self.style.SUCCESS(f"Migrated avatar for user {user.email} -> File {file_id}"))

                except Exception as exc:
                    total_errors += 1
                    logger.error(
                        "SYSTEM_JOB: Failed to migrate avatar for user %s (%s): %s",
                        user.id, avatar_url, exc
                    )
                    self.stderr.write(self.style.ERROR(f"Error migrating {user.email}: {exc}"))

                    # Cleanup S3 object on partial failure
                    if s3_uploaded:
                        storage_service._safe_delete_object(object_key)

        self.stdout.write("\n============================================================")
        self.stdout.write(f"AVATAR MIGRATION SUMMARY (dry_run={dry_run})")
        self.stdout.write(f"Total Users Checked : {total_users_checked}")
        self.stdout.write(f"Avatars Migrated    : {total_avatars_migrated}")
        self.stdout.write(f"Errors Encountered  : {total_errors}")
        self.stdout.write("============================================================")
