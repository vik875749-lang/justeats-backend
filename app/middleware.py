import time

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger(__name__)


class LoggingMiddleware:
    """Pure-ASGI logging middleware — avoids BaseHTTPMiddleware's background-task
    issues that cause asyncpg connection errors in tests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "request",
                method=scope.get("method", "UNKNOWN"),
                path=scope.get("path", "/"),
                status_code=status_code,
                duration_ms=duration_ms,
            )


# How it works:

# When a request comes in, the middleware notes the start time.
# It lets the request proceed to the route handler (or the next middleware).
# After the response is generated, it calculates how long the request took.
# It logs information such as the HTTP method, path, status code, and duration using structlog.