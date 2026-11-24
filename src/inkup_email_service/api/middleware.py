from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging_config import request_id_var

logger = logging.getLogger("inkup.http")


class RequestContextMiddleware:
    """Attach a request id, structured access log, and response header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._request_id(scope)
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_code = 500
        method = scope.get("method", "")
        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "http_request_failed",
                extra={"method": method, "path": path, "status_code": status_code},
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1_000, 2)
            log = logger.debug if path.startswith("/health") else logger.info
            log(
                "http_request",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)

    @staticmethod
    def _request_id(scope: Scope) -> str:
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                candidate = value.decode("latin-1").strip()
                if candidate and len(candidate) <= 128:
                    return candidate
        return uuid.uuid4().hex
