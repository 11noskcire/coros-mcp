"""Firestore-backed credential storage for stateless deployments (Cloud Run).

Enabled with COROS_TOKEN_BACKEND=firestore; requires the ``cloud`` extra
(``pip install .[cloud]``). The serialized StoredAuth JSON is kept as an
opaque string in a single document (<COROS_FIRESTORE_COLLECTION>/auth).

No machine-bound encryption here: a container has no stable machine identity,
and access to the document is controlled by Firestore IAM instead.

Backend errors are raised, not folded into a failed CredentialResult: a
transient read failure must not look like "no token stored", because that
triggers a fresh COROS login — which invalidates the still-valid token.
"""

import functools
import os
import time
from typing import Any

from coros_mcp.auth.keyring_store import CredentialResult

COLLECTION_ENV = "COROS_FIRESTORE_COLLECTION"
DEFAULT_COLLECTION = "coros-mcp"
DOC_ID = "auth"
FIELD = "auth"


@functools.lru_cache(maxsize=1)
def _client() -> Any:
    try:
        from google.cloud import firestore
    except ImportError as e:
        raise ImportError(
            "COROS_TOKEN_BACKEND=firestore requires google-cloud-firestore: pip install 'coros-mcp[cloud]'"
        ) from e
    return firestore.Client()


def _doc() -> Any:
    collection = os.environ.get(COLLECTION_ENV) or DEFAULT_COLLECTION
    return _client().collection(collection).document(DOC_ID)


def store_credential_firestore(token: str) -> CredentialResult:
    if not token or not token.strip():
        return CredentialResult(success=False, message="Token cannot be empty")
    _doc().set({FIELD: token.strip(), "updated_at": int(time.time() * 1000)})
    return CredentialResult(success=True, message="Token stored in Firestore")


def get_credential_firestore() -> CredentialResult:
    snap = _doc().get()
    if not snap.exists:
        return CredentialResult(success=False, message="No token stored in Firestore")
    token = (snap.to_dict() or {}).get(FIELD)
    if not token:
        return CredentialResult(success=False, message="No token stored in Firestore")
    return CredentialResult(success=True, message="Token retrieved from Firestore", token=token)


def clear_credential_firestore() -> CredentialResult:
    _doc().delete()
    return CredentialResult(success=True, message="Token removed from Firestore")
