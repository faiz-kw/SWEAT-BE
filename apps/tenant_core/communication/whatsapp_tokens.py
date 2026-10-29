import uuid
import logging
from typing import Tuple
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)
SIGNER_SALT = 'sweat_wa_action_token'


def generate_trial_action_token(trial_id: uuid.UUID, action: str) -> str:
    """
    Generates a cryptographically signed, tamper-proof token for one-tap WhatsApp actions.
    Actions supported: 'CONFIRM', 'RESCHEDULE', 'PASS', 'REBOOK'
    """
    signer = TimestampSigner(salt=SIGNER_SALT)
    payload = f"{str(trial_id)}:{action}"
    return signer.sign(payload)


def verify_trial_action_token(token: str, max_age_seconds: int = 86400 * 7) -> Tuple[uuid.UUID, str]:
    """
    Verifies and unpacks a trial action token.
    Returns (trial_id, action).
    Raises BadSignature or SignatureExpired on validation failure.
    """
    signer = TimestampSigner(salt=SIGNER_SALT)
    unsigned = signer.unsign(token, max_age=max_age_seconds)
    trial_id_str, action = unsigned.split(':', 1)
    return uuid.UUID(trial_id_str), action

