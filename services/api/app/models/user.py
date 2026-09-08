"""Users, roles and the user-role association.

The authoritative permission matrix lives in :mod:`app.core.rbac` as code, not in
the database. The ``roles`` table exists so roles can be described, listed and
assigned relationally, and it carries a materialised snapshot of the granted
permissions purely so an administrator can *see* the matrix in the UI. The
server always checks against the code matrix — a stale or edited snapshot row
cannot escalate anyone's access.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import UserRole
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.geography import District, State

#: Association table. Users may hold more than one role; capabilities are the
#: union and scope is the widest of them (see ``rbac.widest_scope``).
user_roles = sa.Table(
    "user_roles",
    Base.metadata,
    sa.Column(
        "user_id",
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "role_id",
        UUID(as_uuid=True),
        sa.ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "assigned_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")
    ),
    sa.Column(
        "assigned_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    ),
)


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named job function that bundles permissions."""

    __tablename__ = "roles"

    name: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False, unique=True
    )
    display_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: Read-only mirror of ``rbac.ROLE_PERMISSIONS`` for display. Refreshed by the
    #: seeder; never consulted when authorising a request.
    permissions_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    scope: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="SELF")

    users: Mapped[List["User"]] = relationship(
        secondary=user_roles,
        back_populates="roles",
        primaryjoin="Role.id == user_roles.c.role_id",
        secondaryjoin="User.id == user_roles.c.user_id",
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated principal.

    Officers are pinned to a state and/or district; that assignment, combined
    with the role's scope level, is what narrows every list query. A driver is
    instead linked to a vehicle and sees only their own consignments.
    """

    __tablename__ = "users"
    __table_args__ = (
        sa.CheckConstraint(
            "failed_login_count >= 0", name="failed_login_count_non_negative"
        ),
    )

    email: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, unique=True, index=True
    )
    username: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, index=True
    )

    #: bcrypt hash. The plaintext password never leaves the request handler and
    #: is never logged.
    hashed_password: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(sa.String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(sa.String(20), nullable=True)
    designation: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    #: BCP-47 tag, e.g. ``en``, ``hi``, ``as``, ``bn``, ``lus``, ``mni``, ``kha``.
    #: Drives alert translation for this user.
    preferred_language: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, default="en"
    )

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True
    )
    #: Break-glass flag for the bootstrap administrator only.
    is_superuser: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("states.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Reset on success; when it crosses ``LOGIN_MAX_FAILURES`` the account is
    #: locked until ``locked_until``.
    failed_login_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    #: Invalidates previously issued refresh tokens when bumped. Incremented on
    #: password change and on explicit "sign out everywhere".
    token_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    roles: Mapped[List["Role"]] = relationship(
        secondary=user_roles,
        back_populates="users",
        lazy="selectin",
        primaryjoin="User.id == user_roles.c.user_id",
        secondaryjoin="Role.id == user_roles.c.role_id",
    )
    state: Mapped[Optional["State"]] = relationship(
        "State", lazy="joined", viewonly=True
    )
    district: Mapped[Optional["District"]] = relationship(
        "District", lazy="joined", viewonly=True
    )

    @property
    def role_names(self) -> List[UserRole]:
        """Roles held, as enum members — the input to every RBAC check."""

        return [role.name for role in self.roles]

    @property
    def is_locked(self) -> bool:
        """Whether the account is currently within a lockout window."""

        from app.db.base import utcnow

        return self.locked_until is not None and self.locked_until > utcnow()

