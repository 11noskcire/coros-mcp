"""Unified credential storage: env var → keyring → encrypted file.

COROS_TOKEN_BACKEND=firestore replaces keyring + encrypted file with a single
Firestore document (stateless deployments such as Cloud Run).
"""

import os

from coros_mcp.auth import firestore_store
from coros_mcp.auth.encrypted_store import (
    clear_credential_encrypted,
    get_credential_encrypted,
    store_credential_encrypted,
)
from coros_mcp.auth.keyring_store import CredentialResult, is_keyring_available
from coros_mcp.auth.keyring_store import clear_credential as _keyring_clear
from coros_mcp.auth.keyring_store import get_credential as _keyring_get
from coros_mcp.auth.keyring_store import store_credential as _keyring_store

ENV_VAR = "COROS_ACCESS_TOKEN"
BACKEND_ENV_VAR = "COROS_TOKEN_BACKEND"


def _firestore_enabled() -> bool:
    return os.environ.get(BACKEND_ENV_VAR, "").strip().lower() == "firestore"


def store_token(token: str) -> CredentialResult:
    """Store the Coros access token.

    Always writes the encrypted file (reliable fallback), also writes
    to keyring if available.
    """
    if _firestore_enabled():
        return firestore_store.store_credential_firestore(token)
    result = store_credential_encrypted(token)
    if is_keyring_available():
        kr = _keyring_store(token)
        if kr.success:
            return CredentialResult(
                success=True,
                message="Token stored in keyring and encrypted file",
            )
    return result


def get_token() -> CredentialResult:
    """Retrieve the Coros access token.

    Priority: env var → encrypted file → keyring.
    Encrypted file is checked first because keyring may hang in subprocess
    environments (e.g. MCP server launched by Claude Code / OpenClaw) where
    macOS Keychain requires interactive unlock.
    """
    env = os.environ.get(ENV_VAR)
    if env:
        return CredentialResult(success=True, message="Token from env var", token=env)

    if _firestore_enabled():
        return firestore_store.get_credential_firestore()

    result = get_credential_encrypted()
    if result.success:
        return result

    if is_keyring_available():
        return _keyring_get()

    return result


def clear_token() -> CredentialResult:
    """Remove stored token from all backends."""
    if _firestore_enabled():
        return firestore_store.clear_credential_firestore()
    results = []
    if is_keyring_available():
        results.append(_keyring_clear())
    results.append(clear_credential_encrypted())
    if any(r.success for r in results):
        return CredentialResult(success=True, message="Token cleared")
    return CredentialResult(success=False, message="No token to clear")
