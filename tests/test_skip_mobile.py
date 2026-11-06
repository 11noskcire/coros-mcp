"""Tests for web-only mode (COROS_SKIP_MOBILE=1).

A mobile login (or replay of the stored mobile login payload) signs the user
out of the COROS phone app, so in this mode nothing may touch the mobile API.
"""
import asyncio

import pytest

import coros_mcp.server as server_mod
from coros_mcp import coros_api
from coros_mcp.cache import sync


def _boom(*_a, **_k):
    raise AssertionError("mobile API must not be used with COROS_SKIP_MOBILE=1")


async def _aboom(*_a, **_k):
    _boom()


@pytest.fixture(autouse=True)
def skip_mobile(monkeypatch):
    monkeypatch.setenv("COROS_SKIP_MOBILE", "1")
    monkeypatch.setenv("COROS_EMAIL", "a@b.c")
    monkeypatch.setenv("COROS_PASSWORD", "pw")
    monkeypatch.setattr(coros_api, "_mobile_login", _aboom)


def _auth(**kw):
    return coros_api.StoredAuth(access_token="t", user_id="u", region="eu", timestamp=0, **kw)


async def test_get_sleep_data_returns_clear_error_without_login(monkeypatch):
    monkeypatch.setattr(server_mod, "_get_auth", _aboom)
    monkeypatch.setattr(coros_api, "try_auto_login", _aboom)
    result = await server_mod.get_sleep_data()
    assert result["records"] == []
    assert "COROS_SKIP_MOBILE" in result["error"]
    assert "phone app" in result["error"]


async def test_fetch_sleep_raises_and_never_logs_in(monkeypatch):
    with pytest.raises(coros_api.MobileDisabledError):
        await coros_api.fetch_sleep(_auth(mobile_login_payload={"x": 1}), "20260101", "20260102")


async def test_mobile_refresh_and_ensure_are_noops():
    auth = _auth(mobile_login_payload={"x": 1})
    assert await coros_api._refresh_mobile_token(auth) is False
    assert await coros_api._ensure_mobile_token(auth) is False


async def test_login_forces_skip_mobile(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": "0000", "data": {"accessToken": "web", "userId": "u"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(coros_api.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(coros_api, "_save_auth", lambda _a: None)
    auth = await coros_api.login("a@b.c", "pw", "eu", skip_mobile=False)
    assert auth.access_token == "web"
    assert auth.mobile_access_token is None


async def test_run_with_auth_does_not_relogin_on_mobile_disabled(monkeypatch):
    monkeypatch.setattr(coros_api, "try_auto_login", _aboom)

    async def fn(_auth):
        raise coros_api.MobileDisabledError()

    with pytest.raises(coros_api.MobileDisabledError):
        await server_mod._run_with_auth(fn, _auth())


def test_sync_all_skips_sleep(monkeypatch):
    async def _empty(*_a, **_k):
        return []

    async def _no_pages(*_a, **_k):
        return None

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(coros_api, "fetch_daily_records", _empty)
    monkeypatch.setattr(coros_api, "fetch_sleep", _aboom)
    monkeypatch.setattr(sync, "_fetch_all_activity_pages", _no_pages)
    monkeypatch.setattr(sync, "get_activities", lambda *_a: [])
    monkeypatch.setattr(sync, "init_db", lambda: None)
    monkeypatch.setattr(sync, "cache_status", lambda: {})
    for name in ("get_max_daily_date", "get_max_sleep_date", "get_max_activity_date"):
        monkeypatch.setattr(sync, name, lambda: None)
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    stats = asyncio.run(sync.sync_all(_auth(), "20260101", end_day="20260102"))
    assert stats["sleep"] == 0
    assert stats["errors"] == []


async def test_check_auth_reports_disabled(monkeypatch):
    monkeypatch.setattr(coros_api, "get_stored_auth", lambda: _auth())
    result = await server_mod.check_coros_auth()
    assert result["mobile_token_status"] == "disabled (COROS_SKIP_MOBILE=1)"
