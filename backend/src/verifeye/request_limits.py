"""Streaming request-size limits for endpoints that accept file uploads."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any


ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class _RequestBodyTooLarge(BaseException):
    """Internal sentinel that must pass through framework exception handlers."""


class UploadRequestBodyLimitMiddleware:
    """Reject oversized upload envelopes before multipart parsing can spool them."""

    _event_path = re.compile(r"/api/cameras/\d+/events\Z")

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _applies(self, scope: dict[str, Any]) -> bool:
        if scope["type"] != "http" or scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        return path == "/api/enroll" or bool(self._event_path.fullmatch(path))

    def _declared_too_large(self, scope: dict[str, Any]) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                return int(value) > self.max_bytes
            except (TypeError, ValueError):
                return False
        return False

    async def _send_too_large(self, send: Send) -> None:
        body = b'{"detail":"Upload request body is too large."}'
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _contains_limit_signal(error: BaseExceptionGroup) -> bool:
        for nested in error.exceptions:
            if isinstance(nested, _RequestBodyTooLarge):
                return True
            if isinstance(nested, BaseExceptionGroup) and UploadRequestBodyLimitMiddleware._contains_limit_signal(nested):
                return True
        return False

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return
        if self._declared_too_large(scope):
            await self._send_too_large(send)
            return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._send_too_large(send)
        except BaseExceptionGroup as exc:
            # Starlette's BaseHTTPMiddleware transports receive errors through an
            # AnyIO task group. Its companion "no response" error is a result
            # of this sentinel, so the envelope limit still owns the response.
            if not self._contains_limit_signal(exc):
                raise
            await self._send_too_large(send)
