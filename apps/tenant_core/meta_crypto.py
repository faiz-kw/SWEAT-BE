"""
apps/tenant_core/meta_crypto.py — Cryptographic token protection for Meta Access Tokens.

Zero plaintext token leakage in databases, logs, or API responses.
Uses authenticated Fernet (AES-128-CBC + HMAC-SHA256) encryption.
"""
import base64
import hashlib
import logging
from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _get_fernet_key() -> bytes:
    key_material = getattr(settings, 'META_TOKEN_ENCRYPTION_KEY', None) or settings.SECRET_KEY
    if not key_material:
        raise ValueError("Cannot derive token encryption key: SECRET_KEY is not set.")
    # Derive deterministic 32-byte urlsafe base64 key
    digest = hashlib.sha256(str(key_material).encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_token(raw_token: str) -> str:
    """Encrypt a plaintext OAuth token at rest."""
    if not raw_token or not isinstance(raw_token, str):
        return ''
    fernet = Fernet(_get_fernet_key())
    return fernet.encrypt(raw_token.strip().encode('utf-8')).decode('utf-8')


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt an encrypted OAuth token for making authenticated Graph API requests."""
    if not encrypted_token or not isinstance(encrypted_token, str):
        return ''
    try:
        fernet = Fernet(_get_fernet_key())
        return fernet.decrypt(encrypted_token.strip().encode('utf-8')).decode('utf-8')
    except (InvalidToken, Exception) as exc:
        logger.error("Failed to decrypt Meta access token: %s", type(exc).__name__)
        return ''


def mask_token(token: str) -> str:
    """Mask a token for safe UI display (e.g. EAAB...7x9Q)."""
    if not token or not isinstance(token, str):
        return ''
    token = token.strip()
    if len(token) <= 8:
        return '****'
    return f"{token[:4]}...{token[-4:]}"
