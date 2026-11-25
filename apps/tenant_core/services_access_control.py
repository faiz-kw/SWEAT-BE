"""
Access Control Service: Token Generation, Cryptographic Hashing, and Verification.

Implements the TimeWatch/Dahua turnstile access integration specifications:
- 128-bit CSPRNG entropy encoded in Base62 (exactly 22 characters).
- Fits within Dahua NetSDK's 32-character szCardNo buffer.
- Standard NIST SHA-256 lowercase hexadecimal hashing for CRM and Edge cache alignment.
"""

import hashlib
import logging
import secrets
import threading
from typing import Dict, Any, Optional
from datetime import datetime
from django.utils import timezone

logger = logging.getLogger(__name__)

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE62_BASE = 62


def generate_compact_qr_token(seed_bytes: Optional[bytes] = None) -> str:
    """
    Generates a cryptographically secure 128-bit token encoded in Base62.
    Entropy: 16 bytes = 128 bits.
    Length: exactly 22 characters (62^22 > 2^128).
    Math: ceil(128 / log2(62)) = 22 characters.
    If seed_bytes is provided (16 bytes), produces a deterministic token.
    """
    if seed_bytes is None:
        seed_bytes = secrets.token_bytes(16)
    num = int.from_bytes(seed_bytes[:16], "big")
    chars = []
    while num > 0:
        num, rem = divmod(num, BASE62_BASE)
        chars.append(BASE62_ALPHABET[rem])
    # Left-pad to ensure fixed 22-character length
    while len(chars) < 22:
        chars.append(BASE62_ALPHABET[0])
    return "".join(reversed(chars))


def generate_booking_qr_token(booking_id: str, secret_key: str) -> str:
    """
    Produces a deterministic, cryptographically secure 22-character Base62 token
    bound to a specific booking ID and application secret key via HMAC-SHA256.
    Ensures that re-requesting or refreshing the pass for the same booking always yields
    the identical QR token while preventing token tampering.
    """
    import hmac
    seed = hmac.new(secret_key.encode("utf-8"), str(booking_id).encode("utf-8"), hashlib.sha256).digest()[:16]
    return generate_compact_qr_token(seed)


def hash_qr_token(token: str) -> str:
    """
    Computes a 64-character lowercase hexadecimal SHA-256 hash of the QR token.
    Preimage has 128 bits of entropy; preimage cannot be brute-forced or rainbow-tabled.
    Identical across Python (backend) and C# .NET (Edge Service).
    """
    cleaned = token.strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest().lower()


def verify_qr_token(token: str, expected_hash: str) -> bool:
    """
    Verifies a scanned token against an expected SHA-256 hash using constant-time comparison.
    """
    if not token or not expected_hash:
        return False
    computed = hash_qr_token(token)
    return secrets.compare_digest(computed, expected_hash.strip().lower())


def generate_trial_qr_token(trial_id: str, secret_key: str) -> str:
    """
    Produces a deterministic 22-character Base62 token bound to a TrialBooking ID.
    Domain-separated from Booking UUIDs with a 'TRIAL:' prefix.
    """
    return generate_booking_qr_token(f"TRIAL:{trial_id}", secret_key)


def get_access_pass_qr_token(access_pass, secret_key: str) -> Optional[str]:
    """Reconstructs the deterministic 22-char QR token for a Booking or TrialBooking AccessPass."""
    if getattr(access_pass, 'booking_id', None):
        return generate_booking_qr_token(str(access_pass.booking_id), secret_key)
    if getattr(access_pass, 'trial_booking_id', None):
        return generate_trial_qr_token(str(access_pass.trial_booking_id), secret_key)
    return None


def push_qr_to_studio_terminals(
    pass_id: str,
    display_name: str,
    qr_token: str,
    branch=None,
    category_code: str = '',
    db_alias: str = None,
) -> None:
    """
    Registers a QR token as a credential on matching active TurnstileDevice terminals.

    Queries TurnstileDevice synchronously on the caller's thread/connection (so TenantRouter
    isolation holds) and performs RPC2 calls asynchronously on a daemon thread.
    """
    from .context import get_tenant_db_alias
    from .models_devices import TurnstileDevice
    from .dahua_rpc import register_card as _register_card

    try:
        alias = (
            db_alias
            or getattr(getattr(branch, '_state', None), 'db', None)
            or get_tenant_db_alias()
        )
        if not alias:
            logger.debug('push_qr_to_studio_terminals: no tenant DB alias; push skipped.')
            return

        qs = TurnstileDevice.objects.using(alias).filter(is_active=True)
        if branch:
            qs = qs.filter(branch=branch)

        matching_devices = [
            (d.name, d.base_url)
            for d in qs
            if d.matches_category(category_code)
        ]
    except Exception as exc:
        logger.error('push_qr_to_studio_terminals lookup error: %s', exc)
        return

    if not matching_devices:
        logger.debug(
            'push_qr_to_studio_terminals: no matching active TurnstileDevice '
            'for branch=%s category=%s. Push skipped.',
            getattr(branch, 'name', 'unknown'), category_code or '(all)',
        )
        return

    user_id_for_terminal = 'P' + pass_id.replace('-', '')[:7]

    def _do_push():
        try:
            for dev_name, base_url in matching_devices:
                ok = _register_card(
                    base_url=base_url,
                    user_id=user_id_for_terminal,
                    display_name=display_name,
                    card_no=qr_token,
                )
                logger.info(
                    'Terminal push %s card=%s... -> %s',
                    dev_name, qr_token[:8], 'OK' if ok else 'FAIL',
                )
        except Exception as exc:
            logger.error('push_qr_to_studio_terminals thread error: %s', exc)

    thread = threading.Thread(
        target=_do_push, daemon=True, name=f'qr-push-{pass_id[:8]}'
    )
    thread.start()


def purge_qr_from_branch_terminals(
    qr_token: str,
    branch_id=None,
    db_alias: str = None,
) -> None:
    """Synchronously deletes a single QR credential from all active branch TurnstileDevices."""
    from .context import get_tenant_db_alias
    from .models_devices import TurnstileDevice
    from .dahua_rpc import delete_card as _delete_card

    if not qr_token:
        return
    try:
        alias = db_alias or get_tenant_db_alias()
        if not alias:
            return
        qs = TurnstileDevice.objects.using(alias).filter(is_active=True)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        for dev in qs:
            _delete_card(dev.base_url, qr_token)
    except Exception as exc:
        logger.warning('Terminal purge skipped for branch=%s: %s', branch_id, exc)
