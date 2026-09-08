"""Redis client for the API process.

Redis has three jobs in this platform: the rate-limit counters below, pub/sub
fan-out for the WebSocket layer (Phase 9), and short-lived caching of provider
responses that are expensive to fetch and stale within minutes anyway.

The connection is built lazily and shared. Timeouts are short and explicit,
because the failure that matters is not Redis being down — that is handled by the
callers, which degrade rather than fail — but Redis being *slow*: a client with no
socket timeout turns one unhealthy dependency into every request hanging.
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["get_redis", "ping_redis", "close_redis"]

#: Two seconds is far longer than a local INCR needs and far shorter than a user
#: will wait. A rate limiter is not worth a slow request.
_SOCKET_TIMEOUT_SECONDS = 2.0

_client: Optional["aioredis.Redis"] = None


def get_redis() -> "aioredis.Redis":
    """The shared async client, created on first use.

    Creating it does not connect; the first command does. That matters at startup —
    the API must be able to boot and report itself unhealthy rather than crash-loop
    because Redis happened to start second.
    """

    global _client
    if _client is None:
        settings = get_settings()
        _client = aioredis.from_url(
            settings.redis_dsn,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
    return _client


async def ping_redis() -> bool:
    """Whether Redis answers. Used by ``GET /health/ready``; never raises."""

    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001 - unreachable, auth failure, timeout alike
        logger.warning("redis ping failed", exc_info=True)
        return False


async def close_redis() -> None:
    """Release the pool on shutdown so reload cycles do not leak sockets."""

    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001 - shutting down regardless
            logger.debug("redis close failed", exc_info=True)
        _client = None
