"""
apps/tenant_core/services_storage.py — File Storage and Image Compression Utilities.
Handles storing uploaded/drawn signature images on the file system instead of directly in DB.
"""

import os
import uuid
import base64
import io
import logging
from PIL import Image
from django.conf import settings

logger = logging.getLogger(__name__)
logging.getLogger('PIL').setLevel(logging.WARNING)


def save_compressed_signature(signature_data, tenant_slug: str = "default") -> str:
    """
    Takes an UploadedFile, a base64 Data URI, or a raw base64 string,
    compresses it using Pillow, saves it to disk under MEDIA_ROOT/signatures/,
    and returns the relative file path.
    If signature_data is already a file path or URL, returns it unmodified.
    """
    if not signature_data:
        return ""

    raw_bytes = None

    # Check if it's an uploaded file object (e.g. from request.FILES)
    if hasattr(signature_data, 'read'):
        try:
            signature_data.seek(0)
        except Exception:
            pass
        raw_bytes = signature_data.read()
    elif isinstance(signature_data, str):
        sig_str = signature_data.strip()
        # If it's already a saved file path or URL, return it
        if sig_str.startswith('/media/') or sig_str.startswith('http://') or sig_str.startswith('https://') or not sig_str.startswith('data:image/'):
            return sig_str

        # Extract base64 payload
        if ';base64,' in sig_str:
            _, b64_payload = sig_str.split(';base64,', 1)
        else:
            b64_payload = sig_str
        try:
            raw_bytes = base64.b64decode(b64_payload)
        except Exception as b64_err:
            logger.warning("Failed to decode base64 signature: %s", b64_err)
            return sig_str
    else:
        return ""

    if not raw_bytes:
        return ""

    try:
        img = Image.open(io.BytesIO(raw_bytes))

        # Max dimension constraint to reduce memory/file size (e.g. max 800x400)
        max_size = (800, 400)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        # Convert mode cleanly
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            img = img.convert('RGBA')
        else:
            img = img.convert('RGB')

        # Target storage directory
        media_root = getattr(settings, 'MEDIA_ROOT', os.path.join(settings.BASE_DIR, 'media'))
        sig_dir = os.path.join(media_root, 'signatures')
        os.makedirs(sig_dir, exist_ok=True)

        filename = f"parq_sig_{uuid.uuid4().hex[:16]}.png"
        filepath = os.path.join(sig_dir, filename)

        # Save with PNG optimization
        img.save(filepath, format='PNG', optimize=True)

        media_url = getattr(settings, 'MEDIA_URL', '/media/')
        if not media_url.endswith('/'):
            media_url += '/'

        relative_path = f"{media_url}signatures/{filename}"
        logger.info("Successfully compressed and saved signature image: %s (path=%s)", filename, relative_path, extra={'correlation_id': '-'})
        return relative_path

    except Exception as exc:
        logger.warning("Failed to compress and save signature image to disk: %s", exc, extra={'correlation_id': '-'})
        return str(signature_data) if isinstance(signature_data, str) else ""
