"""Writer for the audit trail.

The spec requires an audit entry for road status changes, incident verification,
route overrides, shipment modification, alert creation and permission changes.
Authentication outcomes and refused requests are recorded too, because those are
what someone reviewing an incident looks for first.

Two guarantees, and they are deliberately different.

**On a change, the audit row is part of the change.** ``record`` only adds and
flushes; the caller commits. The row and the road status it describes therefore
land in one transaction, so there is no window in which the status changed and the
trail does not say who changed it.

**On a refusal, the audit row is best effort.** ``record_and_commit`` commits by
itself and swallows its own failures, because a failed insert must not convert a
clean 401 or 403 into a 500 — that would both lose the refusal *and* tell the
caller something went wrong internally. Failures are logged locally instead.

Two things never enter a row: the plaintext password and the token. ``old_value``
and ``new_value`` carry the fields that changed, and for a password change that is
a boolean, not a hash.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction
from app.db.base import utcnow
from app.models.system import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)

__all__ = ["record", "record_and_commit", "client_ip", "request_id_of"]

#: Column widths from ``app.models.system.AuditLog``. Truncating here keeps a long
#: user agent or a long path from failing the insert and losing the entry.
_MAX_LABEL = 160
_MAX_ENTITY_LABEL = 255
_MAX_ENTITY_TYPE = 80
_MAX_AGENT = 400
_MAX_PATH = 400
_MAX_IP = 64


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def request_id_of(request: Optional[Request]) -> Optional[str]:
    """The correlation id assigned by the request-context middleware."""

    if request is None:
        return None
    return getattr(request.state, "request_id", None)


def client_ip(request: Optional[Request]) -> Optional[str]:
    """The peer address of the connection, and only that.

    ``X-Forwarded-For`` is *not* used here even though a deployment behind a
    reverse proxy would need it, because any client can send that header. Trusting
    it without a configured list of trusted proxies would let an attacker choose
    what the audit log says about them, which is worse than recording the proxy's
    address. The raw header is preserved in ``metadata_json.forwarded_for``, marked
    as untrusted, so a real deployment can still reconstruct the chain.
    """

    if request is None or request.client is None:
        return None
    return _clip(request.client.host, _MAX_IP)


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    request: Optional[Request] = None,
    actor: Optional[User] = None,
    actor_label: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    entity_label: Optional[str] = None,
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    succeeded: bool = True,
    ai_tool_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    """Add one audit row to the session and flush it. The caller commits.

    ``actor`` is optional so a failed login for an unknown identifier is still
    recorded — with ``actor_label`` holding what was typed. ``actor_roles`` is
    captured at write time because roles change and history should not.
    """

    payload: Dict[str, Any] = dict(metadata or {})
    if request is not None:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Recorded, not trusted. See client_ip().
            payload["forwarded_for_untrusted"] = _clip(forwarded, 200)

    entry = AuditLog(
        occurred_at=utcnow(),
        action=action,
        actor_id=actor.id if actor is not None else None,
        actor_label=_clip(
            actor_label if actor_label is not None else getattr(actor, "email", None),
            _MAX_LABEL,
        ),
        actor_roles=(
            [str(role) for role in actor.role_names] if actor is not None else None
        ),
        entity_type=_clip(entity_type, _MAX_ENTITY_TYPE),
        entity_id=entity_id,
        entity_label=_clip(entity_label, _MAX_ENTITY_LABEL),
        old_value=old_value,
        new_value=new_value,
        reason=reason,
        succeeded=succeeded,
        ip_address=client_ip(request),
        user_agent=_clip(
            request.headers.get("user-agent") if request is not None else None,
            _MAX_AGENT,
        ),
        http_method=request.method if request is not None else None,
        http_path=_clip(str(request.url.path) if request is not None else None, _MAX_PATH),
        request_id=request_id_of(request),
        ai_tool_name=ai_tool_name,
        metadata_json=payload or None,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_and_commit(session: AsyncSession, **kwargs: Any) -> None:
    """Write one audit row in its own transaction, never raising.

    For rejection paths, where the request is about to end in a 401, 403 or 429 and
    there is no other pending work to commit. Any failure is logged and dropped:
    losing an audit row is bad, but failing the refusal is worse.
    """

    try:
        await record(session, **kwargs)
        await session.commit()
    except Exception:  # noqa: BLE001 - the refusal must survive a broken audit write
        logger.warning("audit write failed; the refusal itself still stands", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - session already unusable
            logger.warning("audit rollback failed", exc_info=True)
