"""FastAPI application factory and process-level wiring.

``app.main:app`` is what uvicorn, the Docker healthcheck and the test client all
load, so this module is deliberately thin: configuration, middleware order,
exception shape, health probes, and the v1 router. No business logic.

**Middleware order is a decision, not an accident.** Starlette treats the
last-added middleware as the outermost, so the nesting below is
CORS → request context → rate limit → routes. CORS has to sit outside the rate
limiter, because a 429 without ``Access-Control-Allow-Origin`` reaches the browser
as an opaque CORS failure and the operator sees "network error" instead of "you are
being throttled". Request context sits outside the limiter so that a throttled
request still gets a correlation id in its response and its log line.

**Two health endpoints, because they answer different questions.** ``/health`` is
liveness: is this process running? It touches nothing, which is why the Compose
healthcheck uses it — an API that is fine but waiting for Postgres should not be
killed and restarted into the same wait. ``/health/ready`` is readiness: can this
process actually serve traffic? It checks the database, PostGIS specifically,
Redis, and which external sources this deployment resolved — and returns 503 when
the database is unusable or a selected data source is not connected.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import FixedWindowLimiter
from app.core.redis import close_redis, ping_redis
from app.db.session import async_engine, check_database_health
from app.providers.registry import (
    close_providers,
    provider_report,
    unconnected_integrations,
)

logger = logging.getLogger(__name__)

#: Bumped as phases land. Reported by ``/health`` so a demo machine can be checked
#: against what is actually deployed.
APP_VERSION = "0.1.0"

_DESCRIPTION = """
Logistics resilience and accessibility intelligence for the North Eastern Region.

Routes are scored on reliability, risk, ETA, distance and cost — not on distance
alone — and every recommendation exposes the factors behind it.

**Data provenance is part of the payload.** Every dataset carries a provenance
label (`REAL`, `SYNTHETIC`, `SIMULATED`, `PREDICTED`, `DERIVED`) and the time it was
observed. Restricted government feeds are represented by provider interfaces that
are not connected; nothing here should be read as a claim of access to them.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down.

    Nothing here connects to anything. The API must boot when Postgres or Redis is
    still starting — Compose brings them up together — and report itself unready
    rather than crash-loop. The startup line names the configured providers because
    "which weather source is this demo using" is the first question asked of a
    number on screen.
    """

    settings = get_settings()
    configure_logging()
    logger.info(
        "starting %s %s (env=%s debug=%s)",
        settings.PROJECT_NAME,
        APP_VERSION,
        settings.ENV,
        settings.DEBUG,
        extra={
            "providers": {
                "weather": settings.WEATHER_PROVIDER,
                "hazard": settings.HAZARD_PROVIDER,
                "osm": settings.OSM_PROVIDER,
                "incident": settings.INCIDENT_PROVIDER,
                "routing": settings.ROUTING_PROVIDER,
                "llm": settings.LLM_PROVIDER if settings.LLM_ENABLED else "disabled",
            }
        },
    )
    try:
        yield
    finally:
        await close_providers()
        await close_redis()
        await async_engine.dispose()
        logger.info("shutdown complete")


def _envelope(request: Request, detail: Any, **extra: Any) -> Dict[str, Any]:
    """The one error shape every failure uses.

    ``detail`` matches FastAPI's own key so existing clients keep working, and
    ``request_id`` lets a user paste one string into a bug report that finds the
    exact log lines.
    """

    body: Dict[str, Any] = {
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
    }
    body.update(extra)
    return body


def create_app() -> FastAPI:
    """Build the application. Called once at import, and per-test by fixtures."""

    settings = get_settings()
    configure_logging()

    # Interactive docs are a demo asset, not a production one: in production they
    # advertise every endpoint and schema to anyone who can reach the port.
    docs_enabled = not settings.is_production

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=APP_VERSION,
        description=_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # ------------------------------------------------------------------ middleware
    # Added innermost first: the last one added wraps all the others.

    if settings.RATE_LIMIT_ENABLED:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=FixedWindowLimiter(settings.RATE_LIMIT_WINDOW_SECONDS),
            default_limit=settings.RATE_LIMIT_DEFAULT,
            auth_limit=settings.RATE_LIMIT_AUTH,
            auth_prefixes=(f"{settings.API_V1_PREFIX}/auth",),
            exempt_paths=("/health", "/health/ready", "/", "/docs", "/openapi.json"),
        )

    app.add_middleware(RequestContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Accept-Language"],
        # So a browser client can read the correlation id and its own budget.
        expose_headers=[
            "X-Request-ID",
            "X-Response-Time-Ms",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        ],
        max_age=600,
    )

    # ------------------------------------------------------------ exception shape

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Headers are preserved because WWW-Authenticate on a 401 and Retry-After on
        # a 423 are part of the answer, not decoration.
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 with the offending fields — but never the values.

        Pydantic's ``errors()`` includes the rejected ``input``. On
        ``POST /auth/login`` that input is the password, which would then be echoed
        to the client and written into any log that captures response bodies. Only
        location, message and type survive.
        """

        safe = [
            {"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                request, "request validation failed", errors=jsonable_encoder(safe)
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Log the detail, return none of it.

        A stack trace or a database message in the response body tells a caller
        about table names and library versions. The request id is the bridge: the
        client reports that, the log holds the rest.
        """

        logger.exception("unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(request, "internal server error"),
        )

    # ----------------------------------------------------------------- health, root

    @app.get("/health", tags=["health"], summary="Liveness — is the process up?")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "service": settings.PROJECT_NAME,
            "version": APP_VERSION,
            "env": settings.ENV,
        }

    @app.get(
        "/health/ready",
        tags=["health"],
        summary="Readiness — can it serve traffic?",
        responses={
            503: {
                "description": (
                    "The database is unreachable, lacks PostGIS, or a selected "
                    "data source is not connected"
                )
            }
        },
    )
    async def readiness() -> JSONResponse:
        """Check the database, PostGIS, Redis, and what this deployment is wired to.

        PostGIS is reported explicitly because a plain ``postgres`` image started by
        mistake passes every other check and then fails on the first spatial query.
        Redis being down is reported but does not make the service unready: the rate
        limiter degrades to per-process counters and the API keeps serving.

        The provider block comes from
        :func:`~app.providers.registry.provider_report`, which resolves every
        adapter and makes no network call — safe to hit on every probe, and it
        answers even when every external source is down. Selecting a source that is
        not connected *does* make the deployment unready: ``WEATHER_PROVIDER=imd``
        raises on first use, and a container that reports itself ready and then
        fails every request is worse than one that refuses to join the load
        balancer. The copilot is the one exception — it ships disabled by design and
        no core feature calls it, so an unconfigured LLM is a default, not a fault.
        """

        database: Dict[str, Any]
        try:
            database = dict(await check_database_health())
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.warning("readiness: database check failed: %s", exc)
            database = {"connected": False, "postgis": None, "error": str(exc)[:200]}

        redis_ok = await ping_redis()
        providers = provider_report()
        missing = [
            f"{p['kind']}:{p['implementation']}"
            for p in providers
            if not p["configured"] and p["kind"] != "llm"
        ]
        ready = (
            bool(database.get("connected"))
            and database.get("postgis") is not None
            and not missing
        )
        if missing:
            logger.warning("readiness: selected but not connected: %s", ", ".join(missing))

        return JSONResponse(
            status_code=(
                status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={
                "status": "ready" if ready else "not_ready",
                "version": APP_VERSION,
                "database": database,
                "redis": {"connected": redis_ok, "required": False},
                "providers": providers,
                "providers_not_connected": missing,
                # The integration points that exist as interfaces and nothing more.
                # Published here so "which government sources is this connected to?"
                # has an answer the deployment itself gives.
                "awaiting_authorisation": unconnected_integrations(),
            },
        )

    @app.get("/", tags=["health"], summary="Where to go next", include_in_schema=False)
    async def root() -> Dict[str, Any]:
        return {
            "service": settings.PROJECT_NAME,
            "version": APP_VERSION,
            "api": settings.API_V1_PREFIX,
            "docs": "/docs" if docs_enabled else None,
            "health": "/health",
        }

    # ------------------------------------------------------------------ api routers

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


#: Module-level instance for ``uvicorn app.main:app`` — the entry point named in
#: the Dockerfile, docker-compose.yml and the Makefile.
app = create_app()
