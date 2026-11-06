"""Tests for the Firestore token backend (COROS_TOKEN_BACKEND=firestore).

The Firestore client is replaced with an in-memory fake, so these tests run
without google-cloud-firestore installed and without GCP credentials.
"""
import pytest

from coros_mcp.auth import firestore_store, storage


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, db, key):
        self._db, self._key = db, key

    def set(self, data):
        self._db[self._key] = dict(data)

    def get(self):
        return _Snap(self._db.get(self._key))

    def delete(self):
        self._db.pop(self._key, None)


class _FakeClient:
    def __init__(self):
        self.db: dict = {}

    def collection(self, name):
        client = self

        class _Coll:
            def document(self, doc_id):
                return _Doc(client.db, (name, doc_id))

        return _Coll()


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(firestore_store, "_client", lambda: client)
    monkeypatch.setenv("COROS_TOKEN_BACKEND", "firestore")
    monkeypatch.delenv("COROS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("COROS_FIRESTORE_COLLECTION", raising=False)
    return client


@pytest.fixture
def no_local_backends(monkeypatch):
    """Fail loudly if keyring or the encrypted file is touched."""
    def _boom(*_a, **_k):
        raise AssertionError("local credential backend used in firestore mode")

    for name in ("store_credential_encrypted", "get_credential_encrypted", "clear_credential_encrypted",
                 "_keyring_store", "_keyring_get", "_keyring_clear", "is_keyring_available"):
        monkeypatch.setattr(storage, name, _boom)


def test_round_trip_through_storage(fake_client, no_local_backends):
    blob = '{"access_token": "t", "user_id": "u", "region": "eu", "timestamp": 1}'
    assert storage.store_token(blob).success
    assert fake_client.db[("coros-mcp", "auth")]["auth"] == blob

    result = storage.get_token()
    assert result.success
    assert result.token == blob


def test_missing_doc_is_failure(fake_client, no_local_backends):
    result = storage.get_token()
    assert result.success is False
    assert result.token is None


def test_custom_collection(fake_client, monkeypatch):
    monkeypatch.setenv("COROS_FIRESTORE_COLLECTION", "other")
    storage.store_token("x")
    assert ("other", "auth") in fake_client.db


def test_clear(fake_client, no_local_backends):
    storage.store_token("x")
    assert storage.clear_token().success
    assert fake_client.db == {}


def test_backend_error_propagates(monkeypatch):
    """A Firestore outage must not read as "no token" (that would trigger a login)."""
    class _Broken:
        def collection(self, _name):
            raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(firestore_store, "_client", lambda: _Broken())
    monkeypatch.setenv("COROS_TOKEN_BACKEND", "firestore")
    monkeypatch.delenv("COROS_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        storage.get_token()


def test_default_backend_unchanged(monkeypatch):
    """Without the env var, the firestore store is never consulted."""
    def _boom():
        raise AssertionError("firestore used without COROS_TOKEN_BACKEND=firestore")

    monkeypatch.delenv("COROS_TOKEN_BACKEND", raising=False)
    monkeypatch.delenv("COROS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(firestore_store, "_client", _boom)
    monkeypatch.setattr(storage, "get_credential_encrypted",
                        lambda: storage.CredentialResult(success=True, message="", token="local"))
    assert storage.get_token().token == "local"
