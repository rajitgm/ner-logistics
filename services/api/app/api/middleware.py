"""HTTP middleware: request correlation and rate limiting.

Ordering matters and is set in ``app.main``. Correlation is installed outermost so
that every log line and error response — including one produced by the rate limiter
refusing a request — carries the same id.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Iterable, Sequence

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import request_id_ctx
from app.core.rate_limit import FixedWindowLimiter

logger = logging.getLogger(__name__)

__all__ = ["RequestContextMiddleware", "RateLimitMiddleware"]

#: A supplied correlation id is echoed only if it looks like one. Anything else is
#: replaced: a header containing a newline would otherwise let a caller write their
#: own lines into the log, and a 4 KB header would bloat every record.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

#: Requests slower than this are logged even when they succeed. Two seconds is
#: already a bad experience on a map that is meant to feel live.
_SLOW_REQUEST_MS = 2000.0


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, and log what deserves logging.

    Uvicorn already prints an access line per request, so this logs only the two
    cases that line does not explain: anything that took too long, and anything that
    failed. Successful fast requests stay quiet.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex

        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # Logged here as well as in the exception handler because a failure in
            # another middleware never reaches the handler.
            logger.exception(
                "unhandled error after %.1f ms: %s %s",
                elapsed_ms,
                request.method,
                request.url.path,
            )
            raise
        finally:
            request_id_ctx.reset(token)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"

        if response.status_code >= 500 or elapsed_ms > _SLOW_REQUEST_MS:
            logger.warning(
                "%s %s -> %s in %.1f ms",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                extra={"user_id": getattr(request.state, "user_id", None)},
            )
        elif response.status_code >= 400:
            logger.info(
                "%s %s -> %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={"user_id": getattr(request.state, "user_id", None)},
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-client limiting, with a tighter budget for ``/auth``.

    Keyed by peer address, because the middleware runs before authentication — the
    caller has no identity yet, which is precisely the case worth limiting. Two
    consequences, both real:

    * Several users behind one NAT or one office gateway share a budget. With
      ``RATE_LIMIT_DEFAULT`` at 120 per minute that is comfortable for a dashboard
      and a handful of officers; it is not sized for a public deployment.
    * A deployment behind a reverse proxy sees the proxy's address for everyone.
      Fixing that needs a configured list of trusted proxies before
      ``X-Forwarded-For`` can be believed, which is a deployment decision rather
      than a default. Until then the header is ignored here as well.

    Login gets ``RATE_LIMIT_AUTH`` (10 per minute by default) because that is the
    endpoint an attacker hammers, and it composes with the per-account lockout:
    the limiter slows a spray across many accounts, the lockout stops a run at one.
    """

    def __init__(
        self,
        app: object,
        *,
        limiter: FixedWindowLimiter,
        default_limit: int,
        auth_limit: int,
        auth_prefixes: Sequence[str],
        exempt_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = limiter
        self._default_limit = default_limit
        self._auth_limit = auth_limit
        self._auth_prefixes = tuple(auth_prefixes)
        self._exempt = frozenset(exempt_paths)

    def _classify(self, path: str) -> tuple[str, int]:
        if any(path.startswith(prefix) for prefix in self._auth_prefixes):
            return "auth", self._auth_limit
        return "default", self._default_limit

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        # Preflight requests carry no credentials and no payload; counting them
        # would let a browser's own CORS behaviour exhaust a user's budget.
        if request.method == "OPTIONS" or path in self._exempt:
            return await call_next(request)

        bucket, limit = self._classify(path)
        client = request.client.host if request.client else "unknown"
        verdict = await self._limiter.hit(f"{bucket}:{client}", limit)

        if not verdict.allowed:
            logger.warning(
                "rate limit exceeded: %s on %s (limit %s/%ss%s)",
                client,
                path,
                limit,
                self._limiter.window_seconds,
                ", degraded counters" if verdict.degraded else "",
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "too many requests; slow down and retry",
                    "request_id": getattr(request.state, "request_id", None),
                },
                headers={
                    "Retry-After": str(verdict.reset_after),
                    "X-RateLimit-Limit": str(verdict.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(verdict.reset_after),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(verdict.limit)
        response.headers["X-RateLimit-Remaining"] = str(verdict.remaining)
        response.headers["X-RateLimit-Reset"] = str(verdict.reset_after)
        return response
