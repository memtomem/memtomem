"""Request-body limit for the multipart upload endpoint."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_REQUEST_LIMIT = 201 * 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Fail upload requests at 201 MiB before multipart parsing completes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/upload":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > UPLOAD_REQUEST_LIMIT:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > UPLOAD_REQUEST_LIMIT:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"Upload request too large"}'
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
