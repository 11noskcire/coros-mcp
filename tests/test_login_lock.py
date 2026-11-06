"""Tests for serialized auto-login: concurrent callers must cause one login.

Every COROS login invalidates the previous token, so two overlapping logins
would leave one request holding a dead token.
"""
import asyncio
import time

import pytest

import coros_mcp.server as server_mod
from coros_mcp import coros_api


@pytest.fixture
def env(monkeypatch):
    """In-memory token store + a fake login that counts calls."""
    store: dict = {}
    calls: list[int] = []

    monkeypatch.setenv("COROS_EMAIL", "a@b.c")
    monkeypatch.setenv("COROS_PASSWORD", "pw")
    monkeypatch.delenv("COROS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(coros_api, "_load_auth", lambda: store.get("auth"))
    monkeypatch.setattr(coros_api, "_save_auth", lambda a: store.__setitem__("auth", a))
    monkeypatch.setattr(coros_api, "_login_lock", asyncio.Lock())

    async def fake_login(email, password, region="eu", *, skip_mobile=True):
        calls.append(1)
        await asyncio.sleep(0.05)  # widen the race window
        auth = coros_api.StoredAuth(
            access_token=f"tok{len(calls)}", user_id="u", region=region,
            timestamp=_now_ms(),
        )
        coros_api._save_auth(auth)
        return auth

    monkeypatch.setattr(coros_api, "login", fake_login)
    return store, calls


def _now_ms() -> int:
    return int(time.time() * 1000)


async def test_concurrent_auto_login_logs_in_once(env):
    _store, calls = env
    a, b = await asyncio.gather(coros_api.try_auto_login(), coros_api.try_auto_login())
    assert len(calls) == 1
    assert a is not None and b is not None
    assert a.access_token == b.access_token == "tok1"


async def test_valid_stored_token_needs_no_login(env):
    store, calls = env
    store["auth"] = coros_api.StoredAuth(access_token="old", user_id="u", region="eu", timestamp=_now_ms())
    auth = await coros_api.try_auto_login()
    assert auth.access_token == "old"
    assert calls == []


async def test_expired_stored_token_triggers_login(env):
    store, calls = env
    expired = _now_ms() - coros_api.TOKEN_TTL_MS - 1
    store["auth"] = coros_api.StoredAuth(access_token="old", user_id="u", region="eu", timestamp=expired)
    auth = await coros_api.try_auto_login()
    assert auth.access_token == "tok1"
    assert len(calls) == 1


async def test_concurrent_rejections_log_in_once(env):
    """Two requests holding the same rejected token → one re-login, both retry with it."""
    store, calls = env
    stale = coros_api.StoredAuth(access_token="stale", user_id="u", region="eu", timestamp=_now_ms())
    store["auth"] = stale
    used: list[str] = []

    async def fn(auth):
        used.append(auth.access_token)
        if auth.access_token == "stale":
            await asyncio.sleep(0.01)
            raise coros_api.CorosAPIError("1019", "Access token is invalid")
        return auth.access_token

    results = await asyncio.gather(
        server_mod._run_with_auth(fn, stale),
        server_mod._run_with_auth(fn, stale),
    )
    assert len(calls) == 1
    assert results == ["tok1", "tok1"]


async def test_firestore_mode_no_login_on_non_auth_error(env, monkeypatch):
    _store, calls = env
    monkeypatch.setattr(server_mod, "_LOGIN_ONLY_ON_AUTH_ERROR", True)
    auth = coros_api.StoredAuth(access_token="t", user_id="u", region="eu", timestamp=_now_ms())

    async def fn(_auth):
        raise RuntimeError("network blip")

    with pytest.raises(RuntimeError):
        await server_mod._run_with_auth(fn, auth)
    assert calls == []


async def test_default_mode_still_retries_any_error(env, monkeypatch):
    """Local stdio behaviour unchanged: read tools re-login after any failure."""
    _store, calls = env
    monkeypatch.setattr(server_mod, "_LOGIN_ONLY_ON_AUTH_ERROR", False)
    auth = coros_api.StoredAuth(access_token="t", user_id="u", region="eu", timestamp=_now_ms())
    attempts: list[str] = []

    async def fn(a):
        attempts.append(a.access_token)
        if len(attempts) == 1:
            raise RuntimeError("network blip")
        return "ok"

    assert await server_mod._run_with_auth(fn, auth) == "ok"
    assert len(calls) == 1


async def test_failed_auto_login_logs_reason(env, monkeypatch, caplog):
    """A failed login must leave its cause in the logs, not only "Not authenticated"."""
    async def failing_login(*_a, **_k):
        raise coros_api.CorosAPIError("1030", "Coros login error: wrong password (result=1030)")

    monkeypatch.setattr(coros_api, "login", failing_login)
    with caplog.at_level("WARNING", logger="coros_mcp.coros_api"):
        assert await coros_api.try_auto_login() is None
    assert "CorosAPIError" in caplog.text
    assert "result=1030" in caplog.text
    assert "pw" not in caplog.text.replace("password", "")  # the password value is never logged
