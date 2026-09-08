"""``/auth`` — login, refresh, logout, identity and password change.

The lockout and the response codes are the two things worth reading before
changing anything here.

**Lockout composes with rate limiting rather than replacing it.** The limiter in
``app.api.middleware`` slows one address spraying many accounts;
``LOGIN_MAX_FAILURES`` stops one account being ground through a dictionary from
anywhere. Neither alone is enough.

**Order of checks is chosen to avoid an enumeration oracle.** The password is
verified *first*. A caller who supplies the wrong password always gets the same
401, whether the account is unknown, deactivated or locked. Only a caller who
already knows the password learns that the account is locked (423) or deactivated
(401 with a specific message) — which they could establish anyway, and which an
officer locked out mid-incident genuinely needs to be told. An unknown identifier
still costs one bcrypt verification against a throwaway hash, so the response time
does not distinguish "no such user" from "wrong password".

**Logout is server-side.** JWTs cannot be un-issued, so ending a session means
bumping ``token_version`` — one ``UPDATE`` that invalidates every token for that
user. It therefore signs the account out everywhere, deliberately: a logout that
leaves a stolen token working is not a logout.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from typing import List, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.core import security
from app.core.config import get_settings
from app.core.enums import AuditAction, Permission
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    PasswordPolicyError,
    TokenError,
    TokenExpiredError,
)
from app.db.base import utcnow
from app.db.session import get_db
from app.models.user import Role, User
from app.schemas.auth import (
    LoginRequest,
    MessageOut,
    PasswordChangeRequest,
    RefreshRequest,
    RoleOut,
    TokenPair,
    UserOut,
)
from app.services import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])

_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def _invalid_credentials() -> HTTPException:
    """The one response for every wrong-credentials case."""

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect identifier or password",
        headers=_AUTH_HEADERS,
    )


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """A bcrypt hash of a random string nobody holds.

    Verified against when the identifier is unknown, so the endpoint spends the
    same ~250 ms it would spend on a real account. Computed once, on the first such
    attempt, rather than at import time — paying for it at startup would slow every
    boot for a case that may never occur. It is not a secret: the plaintext is
    discarded and no account uses it.
    """

    return security.hash_password(security.generate_password(24))


async def _find_user(session: AsyncSession, identifier: str) -> Optional[User]:
    """Look a user up by email or username.

    The email comparison is case-insensitive because nobody remembers whether they
    signed up as ``B.Sarma@`` or ``b.sarma@``; the username is compared exactly
    because it is issued by an administrator. ``lower(email)`` does not use the
    plain index on ``email`` — at this table's size that is irrelevant, and the fix,
    if it ever matters, is a functional index rather than a different query.
    """

    ident = identifier.strip()
    if not ident:
        return None
    return await session.scalar(
        select(User).where(
            sa.or_(
                sa.func.lower(User.email) == ident.lower(),
                User.username == ident,
            )
        )
    )


def _token_pair(user: User) -> TokenPair:
    """Mint an access/refresh pair for a user whose roles are already loaded."""

    settings = get_settings()
    roles = [str(role) for role in user.role_names]
    return TokenPair(
        access_token=security.create_access_token(user.id, user.token_version, roles),
        refresh_token=security.create_refresh_token(user.id, user.token_version, roles),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        issued_at=utcnow(),
    )


async def _register_failure(
    session: AsyncSession, request: Request, user: User
) -> None:
    """Count a failed attempt, lock the account if it has run out of them, audit.

    On reaching the threshold the counter is reset *and* ``locked_until`` is set, so
    the account gets a fresh allowance when the window expires instead of re-locking
    on the very next mistake.
    """

    settings = get_settings()
    user.failed_login_count = (user.failed_login_count or 0) + 1
    locked = user.failed_login_count >= settings.LOGIN_MAX_FAILURES
    if locked:
        user.locked_until = utcnow() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
        user.failed_login_count = 0

    await audit.record(
        session,
        action=AuditAction.LOGIN_FAILURE,
        request=request,
        actor=user,
        succeeded=False,
        entity_type="users",
        entity_id=user.id,
        entity_label=user.email,
        metadata={
            "reason": "bad_password",
            "failures_in_window": user.failed_login_count,
            "locked": locked,
            "lockout_minutes": settings.LOGIN_LOCKOUT_MINUTES if locked else None,
        },
    )
    # Committed here because the caller is about to raise, and an exception would
    # otherwise roll the counter back with it — turning the lockout into a no-op.
    await session.commit()


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Exchange credentials for an access and refresh token",
    responses={
        401: {"description": "Unknown identifier, wrong password, or inactive account"},
        423: {"description": "Correct password, but the account is locked"},
        429: {"description": "Too many attempts from this address"},
    },
)
async def login(
    request: Request,
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenPair:
    """Authenticate and issue tokens."""

    settings = get_settings()
    user = await _find_user(session, payload.identifier)

    if user is None:
        security.verify_password(payload.password, _decoy_hash())
        await audit.record_and_commit(
            session,
            action=AuditAction.LOGIN_FAILURE,
            request=request,
            actor_label=payload.identifier,
            succeeded=False,
            metadata={"reason": "unknown_identifier"},
        )
        raise _invalid_credentials()

    if not security.verify_password(payload.password, user.hashed_password):
        await _register_failure(session, request, user)
        raise _invalid_credentials()

    # From here the caller has proved they hold the password, so specific
    # diagnostics are no longer an enumeration risk.
    if user.is_locked:
        retry_after = max(
            1, int((user.locked_until - utcnow()).total_seconds())  # type: ignore[operator]
        )
        await audit.record_and_commit(
            session,
            action=AuditAction.LOGIN_FAILURE,
            request=request,
            actor=user,
            succeeded=False,
            entity_type="users",
            entity_id=user.id,
            entity_label=user.email,
            metadata={"reason": "locked", "retry_after_seconds": retry_after},
        )
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "too many failed attempts; this account is locked for "
                f"{settings.LOGIN_LOCKOUT_MINUTES} minutes"
            ),
            headers={"Retry-After": str(retry_after)},
        )

    if not user.is_active:
        await audit.record_and_commit(
            session,
            action=AuditAction.LOGIN_FAILURE,
            request=request,
            actor=user,
            succeeded=False,
            entity_type="users",
            entity_id=user.id,
            entity_label=user.email,
            metadata={"reason": "inactive"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="this account has been deactivated; contact an administrator",
            headers=_AUTH_HEADERS,
        )

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()

    if security.needs_rehash(user.hashed_password):
        # Raising the cost factor for the whole user base gradually, without a reset
        # campaign. A password that predates the current minimum length cannot be
        # re-hashed, and that must not fail an otherwise valid login.
        try:
            user.hashed_password = security.hash_password(payload.password)
        except PasswordPolicyError:
            logger.info("skipped rehash for %s: password below current policy", user.id)

    tokens = _token_pair(user)
    await audit.record(
        session,
        action=AuditAction.LOGIN_SUCCESS,
        request=request,
        actor=user,
        entity_type="users",
        entity_id=user.id,
        entity_label=user.email,
        metadata={"roles": [str(r) for r in user.role_names]},
    )
    await session.commit()
    return tokens


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Exchange a refresh token for a new pair",
    responses={401: {"description": "Refresh token expired, invalid or revoked"}},
)
async def refresh_tokens(
    request: Request,
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenPair:
    """Issue a new access token, and a new refresh token alongside it.

    Roles are re-read from the database, so a role removed five minutes ago is gone
    from the next access token rather than surviving until the refresh token
    expires.

    To be precise about what this does *not* do: the presented refresh token stays
    valid until its own expiry. Detecting a replayed one requires storing issued
    ``jti`` values, which is a later phase. Until then the revocation mechanism is
    ``token_version`` — logout, a password change, or an administrator bumping it
    invalidates every outstanding token for that user at once.
    """

    try:
        claims = security.decode_token(
            payload.refresh_token, expected_type=TOKEN_TYPE_REFRESH
        )
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token has expired; sign in again",
            headers=_AUTH_HEADERS,
        ) from exc
    except TokenError as exc:
        logger.info("refresh rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token is not valid",
            headers=_AUTH_HEADERS,
        ) from exc

    user = await session.scalar(select(User).where(User.id == claims.subject))
    if (
        user is None
        or not user.is_active
        or user.is_locked
        or claims.token_version != user.token_version
    ):
        # One message for all four: which of them applies is in the log, not in the
        # response.
        logger.info(
            "refresh denied for subject %s (found=%s)", claims.subject, user is not None
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credentials are no longer valid; sign in again",
            headers=_AUTH_HEADERS,
        )

    tokens = _token_pair(user)
    await audit.record(
        session,
        action=AuditAction.TOKEN_REFRESH,
        request=request,
        actor=user,
        entity_type="users",
        entity_id=user.id,
        entity_label=user.email,
    )
    await session.commit()
    return tokens


@router.post(
    "/logout",
    response_model=MessageOut,
    summary="End every session for the calling account",
)
async def logout(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Invalidate all tokens held by the caller.

    Signs the account out on every device, including this one. That is the point:
    the reason someone logs out of a shared terminal at a district office is that
    they do not know what else is holding a token.
    """

    user.token_version += 1
    await audit.record(
        session,
        action=AuditAction.LOGOUT,
        request=request,
        actor=user,
        entity_type="users",
        entity_id=user.id,
        entity_label=user.email,
        new_value={"token_version": user.token_version},
    )
    await session.commit()
    return MessageOut(detail="signed out; all tokens for this account are now invalid")


@router.get(
    "/me",
    response_model=UserOut,
    summary="The calling account, its roles and its effective permissions",
)
async def read_me(user: User = Depends(get_current_user)) -> UserOut:
    """Identity plus effective access.

    The frontend uses ``permissions`` to decide which controls to render. That is
    presentation only — every endpoint re-checks server-side, so a client that
    ignores this list gains nothing but a 403.
    """

    return UserOut.from_user(user)


@router.get(
    "/roles",
    response_model=List[RoleOut],
    summary="Every role and the permissions it grants",
)
async def list_roles(
    session: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.USER_READ)),  # noqa: B008
) -> List[RoleOut]:
    """List roles for the user-administration screen.

    Permissions are reported from the code matrix, not from
    ``roles.permissions_snapshot``: the snapshot is a display mirror the seeder
    refreshes, and a screen that shows what the API will actually allow is worth
    more than one that shows what a stale row claims.
    """

    roles = (await session.scalars(select(Role).order_by(Role.name))).all()
    return [RoleOut.from_role(role) for role in roles]


@router.post(
    "/change-password",
    response_model=MessageOut,
    summary="Change the calling account's password",
    responses={401: {"description": "The current password was wrong"}},
)
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Replace the password, then invalidate every existing token.

    The current password is required even though the caller is authenticated: a
    token left open on an unattended terminal should not be enough to take the
    account over. Bumping ``token_version`` means the caller must sign in again with
    the new password — and so must anyone else holding a token for this account.

    The audit row records that a password changed, never the old or new value.
    """

    if not security.verify_password(payload.current_password, user.hashed_password):
        await audit.record_and_commit(
            session,
            action=AuditAction.ENTITY_UPDATE,
            request=request,
            actor=user,
            succeeded=False,
            entity_type="users",
            entity_id=user.id,
            entity_label=user.email,
            metadata={"field": "password", "reason": "current_password_mismatch"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="the current password is incorrect",
            headers=_AUTH_HEADERS,
        )

    try:
        user.hashed_password = security.hash_password(payload.new_password)
    except PasswordPolicyError as exc:
        # The schema enforces the same bounds, so reaching this means policy and
        # schema disagree — a 422 is still the honest answer to the caller.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    user.token_version += 1
    await audit.record(
        session,
        action=AuditAction.ENTITY_UPDATE,
        request=request,
        actor=user,
        entity_type="users",
        entity_id=user.id,
        entity_label=user.email,
        new_value={"password_changed": True, "token_version": user.token_version},
    )
    await session.commit()
    return MessageOut(
        detail="password changed; sign in again with the new password"
    )
