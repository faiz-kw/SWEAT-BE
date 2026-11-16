import uuid
import logging
from typing import Tuple, Optional
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)
SIGNER_SALT = 'sweat_wa_action_token'


def generate_trial_action_token(trial_id: uuid.UUID, action: str, tenant_id: Optional[uuid.UUID] = None) -> str:
    """
    Generates a cryptographically signed, tamper-proof token for one-tap WhatsApp actions.
    Actions supported: 'CONFIRM', 'RESCHEDULE', 'PASS', 'REBOOK'
    Optionally binds tenant_id to enable zero-lookup deterministic multi-tenant routing.
    """
    signer = TimestampSigner(salt=SIGNER_SALT)
    if tenant_id:
        payload = f"{str(trial_id)}:{action}:{str(tenant_id)}"
    else:
        payload = f"{str(trial_id)}:{action}"
    return signer.sign(payload)


def verify_trial_action_token(token: str, max_age_seconds: int = 86400 * 7) -> Tuple[uuid.UUID, str, Optional[uuid.UUID]]:
    """
    Verifies and unpacks a trial action token.
    Returns (trial_id, action, tenant_id).
    Raises BadSignature or SignatureExpired on validation failure.
    """
    signer = TimestampSigner(salt=SIGNER_SALT)
    unsigned = signer.unsign(token, max_age=max_age_seconds)
    parts = unsigned.split(':')
    trial_id = uuid.UUID(parts[0])
    action = parts[1]
    tenant_id = uuid.UUID(parts[2]) if len(parts) > 2 else None
    return trial_id, action, tenant_id

