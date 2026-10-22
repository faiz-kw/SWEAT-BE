"""
Access Control Service: Token Generation, Cryptographic Hashing, and Verification.

Implements the TimeWatch/Dahua turnstile access integration specifications:
- 128-bit CSPRNG entropy encoded in Base62 (exactly 22 characters).
- Fits within Dahua NetSDK's 32-character szCardNo buffer.
- Standard NIST SHA-256 lowercase hexadecimal hashing for CRM and Edge cache alignment.
"""

import hashlib
import secrets
from typing import Dict, Any, Optional
from datetime import datetime
from django.utils import timezone

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
