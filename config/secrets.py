"""
config/secrets.py — Enterprise Secret Resolution Architecture.

Provides:
- SecretResolver: Unified, secure secret resolution for database passwords, integration keys, and tokens.
- SecretResolutionError: Redacted exception for secret retrieval failures (zero credential leakage).
- Support for:
  * vault://path/to/secret#key (HashiCorp Vault or configured Vault backend)
  * aws-secretsmanager://secret-id (AWS Secrets Manager)
  * env://ENV_VAR_NAME (Environment variables)
- Deterministic test seam for integration tests without mocking or hardcoding.
"""

import os
import re
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Sensitive key patterns that must be redacted recursively
SENSITIVE_PATTERNS = re.compile(
    r'(password|secret|token|credential|api_key|access_key|private_key)',
    re.IGNORECASE
)


class SecretResolutionError(Exception):
    """Raised when a secret reference cannot be securely resolved."""
    pass


class SecretResolver:
    """
    Unified Secret Resolver for Master and Tenant runtime secrets.
    Ensures plaintext credentials are never stored in databases, source code, or logs.
    """
    # Deterministic test seam store: reference -> secret_value
    _test_secret_store: Dict[str, str] = {}

    @classmethod
    def register_test_secret(cls, reference: str, value: str) -> None:
        """Register a secret for deterministic testing (in-memory test seam)."""
        cls._test_secret_store[reference] = value

    @classmethod
    def clear_test_secrets(cls) -> None:
        """Clear test secret store."""
        cls._test_secret_store.clear()

    @classmethod
    def resolve(cls, secret_ref: Optional[str]) -> str:
        """
        Resolve a secret reference securely.

        Supported reference URIs:
        - env://VAR_NAME: Reads from process environment variable
        - vault://path/to/secret or vault://path/to/secret#key: Resolves from Vault
        - aws-secretsmanager://secret-id: Resolves from AWS Secrets Manager

        Rules:
        - Empty or None secret_ref raises SecretResolutionError.
        - Fails closed on any error.
        - Never returns hardcoded defaults or localhost fallbacks.
        - Credentials are never logged.
        """
        if not secret_ref or not isinstance(secret_ref, str) or not secret_ref.strip():
            raise SecretResolutionError("Secret reference is empty or missing.")

        ref = secret_ref.strip()

        # 1. Check deterministic test seam first
        if ref in cls._test_secret_store:
            return cls._test_secret_store[ref]

        # 2. env:// reference
        if ref.startswith('env://'):
            var_name = ref[len('env://'):].strip()
            if not var_name:
                raise SecretResolutionError(f"Malformed environment secret reference: '{ref}'.")
            val = os.environ.get(var_name)
            if val is None or val == '':
                raise SecretResolutionError(f"Environment secret '{var_name}' is not set or empty.")
            return val

        # 3. vault:// reference
        if ref.startswith('vault://'):
            vault_path = ref[len('vault://'):].strip()
            # If hvac is installed and configured in settings, query Vault
            val = cls._resolve_from_vault(vault_path)
            if val:
                return val
            raise SecretResolutionError(f"Vault secret reference '{vault_path}' could not be resolved.")

        # 4. aws-secretsmanager:// or arn:aws:secretsmanager:
        if ref.startswith('aws-secretsmanager://') or ref.startswith('arn:aws:secretsmanager:'):
            val = cls._resolve_from_aws_sm(ref)
            if val:
                return val
            raise SecretResolutionError(f"AWS Secrets Manager reference '{ref}' could not be resolved.")

        # If secret_ref is an unrecognized scheme, fail closed
        raise SecretResolutionError(
            f"Unsupported secret reference scheme for '{cls.redact_reference(ref)}'. "
            "Supported schemes: env://, vault://, aws-secretsmanager://"
        )

    @classmethod
    def _resolve_from_vault(cls, path: str) -> Optional[str]:
        """Query HashiCorp Vault if configured."""
        vault_url = os.environ.get('VAULT_ADDR')
        vault_token = os.environ.get('VAULT_TOKEN')
        if not vault_url or not vault_token:
            return None

        try:
            import hvac
            client = hvac.Client(url=vault_url, token=vault_token)
            if not client.is_authenticated():
                logger.error("Vault client failed authentication.")
                return None

            # Split path and key if formatted as path#key
            if '#' in path:
                secret_path, secret_key = path.split('#', 1)
            else:
                secret_path, secret_key = path, 'password'

            # Mount point default 'secret'
            mount = 'secret'
            if '/' in secret_path:
                parts = secret_path.split('/', 1)
                mount, secret_path = parts[0], parts[1]

            read_response = client.secrets.kv.v2.read_secret_version(
                path=secret_path,
                mount_point=mount,
            )
            data = read_response.get('data', {}).get('data', {})
            return data.get(secret_key)
        except Exception as e:
            logger.error("Error retrieving secret from Vault: %s", type(e).__name__)
            return None

    @classmethod
    def _resolve_from_aws_sm(cls, ref: str) -> Optional[str]:
        """Query AWS Secrets Manager if configured."""
        secret_id = ref
        if secret_id.startswith('aws-secretsmanager://'):
            secret_id = secret_id[len('aws-secretsmanager://'):]

        try:
            import boto3
            client = boto3.client('secretsmanager')
            resp = client.get_secret_value(SecretId=secret_id)
            return resp.get('SecretString')
        except Exception as e:
            logger.error("Error retrieving secret from AWS SM: %s", type(e).__name__)
            return None

    @staticmethod
    def redact_reference(ref: str) -> str:
        """Safe masking of secret reference string for logging."""
        if not ref:
            return ''
        if len(ref) <= 8:
            return '***'
        return f"{ref[:4]}...{ref[-4:]}"
