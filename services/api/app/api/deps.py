"""FastAPI dependencies: authentication, authorisation and jurisdiction scope.

Every protected endpoint answers three separate questions, and this module is
where each one is answered.

**Who is calling?** ``get_current_user`` verifies the bearer token, then *re-reads
the user row*. The token is proof of a past login, not a cache of current access:
an account deactivated, locked or re-scoped a minute ago must stop working
immediately, and comparing ``claims.token_version`` against the stored
``token_version`` is what makes revocation one ``UPDATE`` instead of a blacklist.

**May they do this?** ``require_permission`` checks ``app.core.rbac`` — the code
matrix — against the roles read from the database. The ``roles`` claim inside the
token is used for logging only. If it were trusted, anyone able to mint a token
could also grant themselves a role.

**May they do it *here*?** ``enforce_scope`` turns the caller's role scope plus
their state/district assignment into a SQL predicate. A district officer in Kamrup
and one in Lunglei hold identical permissions; what differs is the rows they may
see. Permission checks alone would let either read the other's incidents.

The failure codes are deliberate: 401 when the *identity* is unusable (no token,
expired, revoked, deactivated, locked), 403 when the identity is fine but the
capability or the jurisdiction is not. Anything that reveals whether a particular
account exists stays out of the response body.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.enums import AuditAction, Permission
from app.core.rbac import ScopeLevel
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TokenClaims,
    TokenError,
    TokenExpiredError,
    decode_token,
)
from app.db.session import get_db
from app.models.user import User
from app.services import audit

logger = logging.getLogger(__name__)

__all__ = [
    "AccessScope",
    "bearer_scheme",
    "enforce_scope",
    "get_current_user",
    "get_token_claims",
    "require_any_permission",
    "require_permission",
]

#: Sent with every 401 so a compliant client knows to re-authenticate rather than
#: retry the same token.
_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}

#: ``auto_error=False`` because FastAPI's built-in 403-on-missing-credentials is
#: the wrong status: a request with no token at all has failed authentication, not
#: authorisation. Raising it here also lets the ``WWW-Authenticate`` header through.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerToken",
    description=(
        "Access token from POST /api/v1/auth/login. Paste the token itself; the "
        "'Bearer ' prefix is added for you."
    ),
)


def _unauthenticated(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_AUTH_HEADERS,
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def get_token_claims(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> TokenClaims:
    """Verify the bearer token and return its claims.

    ``expected_type`` is pinned to the access type so a refresh token — which
    lives for days — cannot be presented to a data endpoint. Expiry is reported
    distinctly from every other failure because a client that knows it is merely
    expired can refresh silently, while any other failure means stop and log in.
    """

    if credentials is None or not credentials.credentials:
        raise _unauthenticated("authentication credentials were not supplied")
    if credentials.scheme.lower() != "bearer":
        raise _unauthenticated("authorization scheme must be Bearer")

    try:
        return decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
    except TokenExpiredError as exc:
        raise _unauthenticated("access token has expired") from exc
    except TokenError as exc:
        # One message outward for bad signature, wrong audience, wrong type and
        # malformed input alike; the distinction is in the log, not the response.
        logger.info("token rejected: %s", exc)
        raise _unauthenticated("access token is not valid") from exc


async def get_current_user(
    request: Request,
    claims: TokenClaims = Depends(get_token_claims),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the token to a live, usable user row.

    Five ways this returns 401, all of them about the identity rather than the
    action: unknown subject, deactivated account, account inside a lockout window,
    stale ``token_version``, and (upstream) an unverifiable token. A deleted user
    gets the same "no longer valid" wording as a revoked one, so the endpoint is
    not an oracle for which ids exist.
    """

    user = await session.scalar(select(User).where(User.id == claims.subject))
    if user is None:
        raise _unauthenticated("credentials are no longer valid")
    if not user.is_active:
        raise _unauthenticated("this account has been deactivated")
    if user.is_locked:
        raise _unauthenticated("this account is temporarily locked")
    if claims.token_version != user.token_version:
        # Password change, forced sign-out, or a role revocation that bumped the
        # counter. The token is well-formed and unexpired; it is simply void.
        raise _unauthenticated("credentials have been revoked; sign in again")

    # Read by the logging middleware and by audit writes further down the request.
    request.state.user_id = str(user.id)
    request.state.user_roles = [str(role) for role in user.role_names]
    return user


async def _deny(
    request: Request,
    session: AsyncSession,
    user: User,
    missing: Sequence[Permission],
    mode: str,
) -> None:
    """Record a refusal, then let the caller raise.

    Denied attempts are written to the audit trail because they are the first
    thing a reviewer looks for. The write is best-effort — ``record_and_commit``
    swallows its own failures — since a broken audit insert must not turn a clean
    403 into a 500 and thereby *grant* information about the failure.
    """

    await audit.record_and_commit(
        session,
        action=AuditAction.ACCESS_DENIED,
        request=request,
        actor=user,
        succeeded=False,
        entity_type="permission",
        entity_label=",".join(str(p) for p in missing)[:255],
        metadata={
            "required": [str(p) for p in missing],
            "match": mode,
            "roles_held": [str(r) for r in user.role_names],
        },
    )


def require_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[User]]:
    """Dependency factory requiring *every* listed permission.

    Roles come from the database row, never from the token's ``roles`` claim, so a
    permission removed from a role takes effect on the next request rather than on
    the next login. The response names the missing capability: the caller can
    discover it by trial anyway, and naming it makes a support call five minutes
    instead of an hour.
    """

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_db),
    ) -> User:
        held = user.role_names
        missing = [p for p in permissions if not rbac.has_permission(held, p)]
        if missing:
            await _deny(request, session, user, missing, "all")
            raise _forbidden(
                "this account lacks the required permission: "
                + ", ".join(str(p) for p in missing)
            )
        return user

    return dependency


def require_any_permission(
    *permissions: Permission,
) -> Callable[..., Awaitable[User]]:
    """Dependency factory requiring *at least one* of the listed permissions.

    Used where two roles reach the same endpoint by different routes — a driver
    reading their own consignment holds ``shipment:read``, a dispatcher reading all
    of them holds it too but with a wider scope, while an analyst may arrive with
    ``analytics:read`` only. The scope check, not the permission check, is what
    keeps them apart afterwards.
    """

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_db),
    ) -> User:
        held = user.role_names
        if not any(rbac.has_permission(held, p) for p in permissions):
            await _deny(request, session, user, permissions, "any")
            raise _forbidden(
                "this account lacks any of the required permissions: "
                + ", ".join(str(p) for p in permissions)
            )
        return user

    return dependency


@dataclass(frozen=True)
class AccessScope:
    """The jurisdiction one request is confined to.

    Derived from the widest scope across the caller's roles plus their state and
    district assignment. ``User.is_superuser`` is deliberately *not* consulted:
    it is a break-glass flag for the bootstrap account, which already holds ADMIN
    and therefore GLOBAL scope, and honouring it here would create a second,
    quieter path to unrestricted data.

    Frozen so a service layer cannot widen the scope it was handed.
    """

    level: ScopeLevel
    user_id: uuid.UUID
    state_id: Optional[uuid.UUID] = None
    district_id: Optional[uuid.UUID] = None

    @classmethod
    def for_user(cls, user: User) -> "AccessScope":
        return cls(
            level=rbac.widest_scope(user.role_names),
            user_id=user.id,
            state_id=user.state_id,
            district_id=user.district_id,
        )

    @property
    def is_global(self) -> bool:
        return self.level is ScopeLevel.GLOBAL

    # ------------------------------------------------------------ row-level checks

    def allows_owner(self, owner_id: Optional[uuid.UUID]) -> bool:
        """Whether the caller may act on a row owned by ``owner_id``."""

        if self.level is ScopeLevel.SELF:
            return owner_id is not None and owner_id == self.user_id
        return True

    def allows_state(self, state_id: Optional[uuid.UUID]) -> bool:
        """Whether a state-level object is inside the caller's jurisdiction."""

        if self.level is ScopeLevel.GLOBAL:
            return True
        if self.level is ScopeLevel.STATE:
            return state_id is not None and state_id == self.state_id
        # A district officer's state is implied by their district; a self-scoped
        # caller has no jurisdiction over state-level objects at all.
        return False

    def allows_district(
        self,
        district_id: Optional[uuid.UUID],
        state_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """Whether a district-level object is inside the caller's jurisdiction.

        ``state_id`` is accepted so a state officer can be checked without a
        second query; omitting it for a state-scoped caller fails closed.
        """

        if self.level is ScopeLevel.GLOBAL:
            return True
        if self.level is ScopeLevel.STATE:
            return state_id is not None and state_id == self.state_id
        if self.level is ScopeLevel.DISTRICT:
            return district_id is not None and district_id == self.district_id
        return False

    # -------------------------------------------------------------- query narrowing

    def condition(
        self,
        *,
        district_column: Any = None,
        state_column: Any = None,
        owner_column: Any = None,
    ) -> Any:
        """A boolean SQL clause that narrows a query to this scope.

        ::

            stmt = select(Incident).where(
                scope.condition(
                    district_column=Incident.district_id,
                    state_column=Incident.state_id,
                    owner_column=Incident.reported_by_id,
                )
            )

        **It fails closed.** If the level needs a column the caller did not supply,
        or the caller's own assignment is missing — a state officer with no
        ``state_id`` — the result is ``FALSE`` and the query returns nothing. An
        empty list is a visible, reportable bug; returning every district's
        incidents to an officer who should see one is a silent breach, and this is
        the exact mistake the class exists to prevent.
        """

        if self.level is ScopeLevel.GLOBAL:
            return sa.true()
        if self.level is ScopeLevel.STATE:
            if state_column is not None and self.state_id is not None:
                return state_column == self.state_id
            return sa.false()
        if self.level is ScopeLevel.DISTRICT:
            if district_column is not None and self.district_id is not None:
                return district_column == self.district_id
            return sa.false()
        if owner_column is not None:
            return owner_column == self.user_id
        return sa.false()

    # ------------------------------------------------------------- guard helpers

    def require_district(
        self,
        district_id: Optional[uuid.UUID],
        state_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Raise 403 unless a district-level object is in scope."""

        if not self.allows_district(district_id, state_id):
            raise _forbidden("this record is outside your assigned jurisdiction")

    def require_owner(self, owner_id: Optional[uuid.UUID]) -> None:
        """Raise 403 unless a self-scoped caller owns the row."""

        if not self.allows_owner(owner_id):
            raise _forbidden("this record belongs to another user")


async def enforce_scope(user: User = Depends(get_current_user)) -> AccessScope:
    """Dependency yielding the caller's jurisdiction.

    Declared alongside ``require_permission`` on every listing endpoint. The pair
    is the whole authorisation story: the permission decides *whether*, the scope
    decides *which rows*.
    """

    return AccessScope.for_user(user)
