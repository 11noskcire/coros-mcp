"""Tests for the serve-http shared-secret middleware and entrypoint."""
import json

import httpx
import pytest

import coros_mcp.server as server_mod
from coros_mcp.http_auth import SharedSecretMiddleware

TOKEN = "s3cret-" + "x" * 32


async def _echo_app(scope, receive, send):
    """Downstream app that reports the route path Starlette would see."""
    root = scope.get("root_path", "")
    route_path = scope["path"][len(root):] if root else scope["path"]
    body = json.dumps({"route_path": route_path, "root_path": root}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def _client(app=None):
    app = app or SharedSecretMiddleware(_echo_app, token=TOKEN)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_valid_bearer():
    async with _client() as c:
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["route_path"] == "/mcp"


async def test_bearer_scheme_case_insensitive():
    async with _client() as c:
        r = await c.post("/mcp", headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 200


async def test_valid_path_secret():
    async with _client() as c:
        r = await c.post(f"/{TOKEN}/mcp")
    assert r.status_code == 200
    assert r.json() == {"route_path": "/mcp", "root_path": f"/{TOKEN}"}


@pytest.mark.parametrize("headers,path", [
    ({"Authorization": "Bearer wrong"}, "/mcp"),
    ({"Authorization": f"Basic {TOKEN}"}, "/mcp"),
    ({}, "/wrong/mcp"),
    ({}, f"/{TOKEN[:-1]}/mcp"),
    ({}, f"/{TOKEN}"),  # secret alone, no route after it
])
async def test_wrong_secret(headers, path):
    async with _client() as c:
        r = await c.post(path, headers=headers)
    assert r.status_code == 401
    assert r.content == b""


async def test_missing_secret():
    async with _client() as c:
        r = await c.post("/mcp")
    assert r.status_code == 401
    assert r.content == b""
    assert "www-authenticate" not in r.headers


@pytest.mark.parametrize("path", ["/healthz", "/health"])
async def test_health_is_public(path):
    async with _client() as c:
        r = await c.get(path)
    assert r.status_code == 200


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        SharedSecretMiddleware(_echo_app, token="")


# --- integration with the real FastMCP app ---------------------------------

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.mark.parametrize("path,extra_headers,expected", [
    ("/mcp", {"Authorization": f"Bearer {TOKEN}"}, 200),
    (f"/{TOKEN}/mcp", {}, 200),
    ("/mcp", {}, 401),
    ("/healthz", {}, 200),
])
async def test_real_app(path, extra_headers, expected):
    from starlette.middleware import Middleware

    app = server_mod.mcp.http_app(
        stateless_http=True,
        middleware=[Middleware(SharedSecretMiddleware, token=TOKEN)],
    )
    async with app.router.lifespan_context(app), _client(app) as c:
        if path == "/healthz":
            r = await c.get(path)
        else:
            r = await c.post(path, json=_INIT, headers={**_MCP_HEADERS, **extra_headers})
    assert r.status_code == expected
    if expected == 200 and path != "/healthz":
        assert "coros-mcp" in r.text  # serverInfo.name in the initialize result


# --- entrypoint --------------------------------------------------------------

def test_serve_http_refuses_without_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    called = []
    monkeypatch.setattr(server_mod.mcp, "run", lambda *a, **k: called.append(k))
    with pytest.raises(SystemExit):
        server_mod.main_http()
    assert called == []


def test_serve_http_run_kwargs(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PORT", "9999")
    called = []
    monkeypatch.setattr(server_mod.mcp, "run", lambda *a, **k: called.append(k))
    server_mod.main_http()
    (kwargs,) = called
    assert kwargs["transport"] == "http"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999
    assert kwargs["stateless_http"] is True
    assert kwargs["middleware"][0].cls is SharedSecretMiddleware
