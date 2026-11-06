"""Shared-secret authentication for the HTTP transport (coros-mcp serve-http).

A request is let through when it carries the secret either as
``Authorization: Bearer <secret>`` or as the first path segment
(``/<secret>/mcp``) — the latter for MCP clients such as Claude's custom
connector UI that cannot set request headers. Everything else gets a bare 401.

For the path form, the secret segment is moved into the ASGI ``root_path``
(the path itself stays complete, per the ASGI spec), so Starlette routes
``/<secret>/mcp`` to ``/mcp`` and any generated URLs keep the prefix.

Pure ASGI middleware (not BaseHTTPMiddleware) so streamed responses pass
through untouched.
"""

import hmac
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

PUBLIC_PATHS = frozenset({"/healthz", "/health"})


def _matches(candidate: str, secret: bytes) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8", "surrogateescape"), secret)


class SharedSecretMiddleware:
    def __init__(self, app: ASGIApp, token: str, public_paths: Iterable[str] = PUBLIC_PATHS) -> None:
        if not token:
            raise ValueError("SharedSecretMiddleware requires a non-empty token")
        self.app = app
        self._secret = token.encode("utf-8")
        self._public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in self._public_paths or self._bearer_ok(scope):
            await self.app(scope, receive, send)
            return

        segment, sep, _ = path[1:].partition("/")
        if sep and _matches(segment, self._secret):
            scope = dict(scope)
            scope["root_path"] = scope.get("root_path", "") + "/" + segment
            await self.app(scope, receive, send)
            return

        await self._reject(scope, send)

    def _bearer_ok(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                scheme, _, credential = value.decode("latin-1").partition(" ")
                return scheme.lower() == "bearer" and _matches(credential.strip(), self._secret)
        return False

    @staticmethod
    async def _reject(scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})
