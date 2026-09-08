"""Fixed-window rate limiting.

The window is fixed, not sliding. A sliding window is more accurate at the
boundary — a caller can send ``limit`` requests at the end of one window and
``limit`` again at the start of the next, so the true short-term ceiling is twice
the configured number — and that is an acceptable trade for two ``INCR``-class
operations instead of a sorted set per key. The purpose here is to blunt password
guessing and stop one misbehaving client from starving the others, not to meter
billing.

**It fails open.** If Redis is unreachable the limiter allows the request and logs
a warning. Rate limiting is a safety valve, not an authorisation control: failing
closed would mean a Redis blip takes the whole API offline, which is a larger
outage than the one being prevented. Authentication and RBAC never depend on this
module, so an outage here cannot widen anyone's access.

**The in-process fallback counts per worker.** With four uvicorn workers and Redis
down, the effective limit is four times the configured one. That is stated rather
than hidden, because a limiter that quietly counts a quarter of the traffic is
worse than one whose behaviour is written down.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

__all__ = ["FixedWindowLimiter", "Verdict"]

#: Beyond this many distinct local keys the fallback map is cleared wholesale.
#: Bounded memory matters more than perfect accounting in a degraded mode.
_LOCAL_KEY_CEILING = 20_000


@dataclass(frozen=True)
class Verdict:
    """Outcome of one rate-limit check, shaped for the response headers."""

    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    #: True when the count came from the per-process fallback rather than Redis.
    degraded: bool = False


class FixedWindowLimiter:
    """Counts requests per key per fixed window."""

    def __init__(self, window_seconds: int) -> None:
        if window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")
        self.window_seconds = window_seconds
        self._local: Dict[str, Tuple[int, int]] = {}

    # ------------------------------------------------------------------ internals

    def _bucket(self, now: float) -> Tuple[int, int]:
        """Return ``(bucket_index, seconds_until_it_rolls)``."""

        bucket = int(now // self.window_seconds)
        reset_after = int((bucket + 1) * self.window_seconds - now) or 1
        return bucket, reset_after

    def _hit_local(self, key: str, limit: int, bucket: int, reset_after: int) -> Verdict:
        if len(self._local) > _LOCAL_KEY_CEILING:
            self._local.clear()
        seen_bucket, count = self._local.get(key, (bucket, 0))
        count = count + 1 if seen_bucket == bucket else 1
        self._local[key] = (bucket, count)
        return Verdict(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_after=reset_after,
            degraded=True,
        )

    # ---------------------------------------------------------------------- public

    async def hit(self, key: str, limit: int) -> Verdict:
        """Count one request against ``key`` and say whether it may proceed."""

        now = time.time()
        bucket, reset_after = self._bucket(now)
        redis_key = f"ratelimit:{key}:{bucket}"

        try:
            client = get_redis()
            pipe = client.pipeline(transaction=False)
            pipe.incr(redis_key, 1)
            # One extra second so the key outlives the window it belongs to even if
            # the two commands straddle a boundary.
            pipe.expire(redis_key, self.window_seconds + 1)
            result = await pipe.execute()
            count = int(result[0])
        except Exception:  # noqa: BLE001 - see the module docstring: fail open
            logger.warning(
                "rate limiter falling back to in-process counters", exc_info=True
            )
            return self._hit_local(key, limit, bucket, reset_after)

        return Verdict(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_after=reset_after,
        )
