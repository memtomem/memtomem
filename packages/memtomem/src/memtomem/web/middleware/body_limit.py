"""Request-body limits applied before multipart/JSON parsing."""

from __future__ import annotations

import json

from starlette._utils import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_REQUEST_LIMIT = 201 * 1024 * 1024
JSON_REQUEST_LIMIT = 4 * 1024 * 1024
_MULTIPART_PATHS = frozenset({"/api/upload", "/api/export/import"})


class _BodyTooLarge(Exception):
    pass


def _budget(path: str) -> tuple[int, str] | None:
    """The limit and refusal detail one spelling of the request path selects, if any."""
    if path in _MULTIPART_PATHS:
        return UPLOAD_REQUEST_LIMIT, "Upload request too large"
    if path.startswith("/api/"):
        return JSON_REQUEST_LIMIT, "JSON request too large"
    return None


class UploadBodyLimitMiddleware:
    """Fail oversized API bodies before framework parsers allocate them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        # Classify the path the router matches as well as ``scope["path"]``: under a ``Mount``
        # (or a server ``root_path``) the latter keeps the prefix, so on it alone an ``/api/*``
        # request under a prefix such as ``/mm`` read as a non-API path and got no limit
        # (#3607). ``get_route_path`` is the function Starlette's and FastAPI's routers call.
        # When both spellings select a limit, the tighter one applies, so reading the route
        # path never loosens what the full path limited before — under a mount whose own
        # prefix starts with ``/api/``, an upload route keeps the JSON limit.
        selected = [
            budget
            for budget in (_budget(get_route_path(scope)), _budget(scope.get("path", "")))
            if budget is not None
        ]
        if not selected:
            await self.app(scope, receive, send)
            return
        limit, detail = min(selected, key=lambda budget: budget[0])

        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    await self._reject(send, detail)
                    return
            except ValueError:
                await self._reject(send, detail)
                return

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send, detail)

    @staticmethod
    async def _reject(send: Send, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
