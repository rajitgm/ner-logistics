"""Request and response models for authentication and identity.

Three things this module is careful about.

**Password bounds are enforced here, not only in the hasher.** bcrypt ignores
anything past 72 bytes, and ``security.hash_password`` refuses a longer password
by raising. Repeating the bound in the schema turns that into a 422 with a
readable message instead of a 500 from an unhandled ``PasswordPolicyError``.

**The login schema does not advertise the password policy.** ``min_length`` on the
login field is 1, not 12: telling an attacker that passwords are at least twelve
characters shortens their search, and rejecting a short password at login with a
policy message also confirms it was the *wrong* password rather than a
non-existent account. The policy is enforced where passwords are *set*.

**Permissions in ``UserOut`` come from the code matrix.** They are computed by
``app.core.rbac`` at serialisation time, not read from ``roles.permissions_snapshot``.
The snapshot is a display mirror that the seeder refreshes; if the two ever
disagree, the code matrix is right and the response should say what the API will
actually allow. The frontend uses this list to hide controls, which is cosmetic —
every endpoint re-checks server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core import rbac
from app.core.enums import UserRole
from app.core.security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH

__all__ = [
    "LoginRequest",
    "TokenPair",
    "RefreshRequest",
    "PasswordChangeRequest",
    "RoleOut",
    "UserOut",
    "MessageOut",
]


def _reject_long_password(value: str) -> str:
    """Mirror the 72-byte bcrypt ceiling at the edge of the API.

    Measured in UTF-8 bytes, not characters, because that is what bcrypt counts:
    an Assamese or Bengali passphrase reaches the limit in roughly a third as many
    characters as an ASCII one, and silently keeping only the first 72 bytes would
    make two different passphrases the same secret.
    """

    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 "
            "encoded (non-Latin scripts use several bytes per character)"
        )
    return value


class LoginRequest(BaseModel):
    """Credentials presented to ``POST /auth/login``.

    ``identifier`` accepts either the email address or the username. Field
    officers are issued short usernames because typing an email on a phone in the
    rain is its own denial of service; the command centre uses email. Accepting
    both costs one ``OR`` in the lookup.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "identifier": "admin@ner-logistics.local",
                "password": "the-password-printed-by-the-seeder",
            }
        }
    )

    identifier: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Email address or username.",
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Plaintext password. Never logged, never stored.",
    )


class TokenPair(BaseModel):
    """What a successful login or refresh returns.

    ``expires_in`` is seconds until the *access* token expires, which is what a
    client needs to schedule a refresh. The refresh token's own lifetime is a
    server policy the client should not depend on.
    """

    access_token: str
    refresh_token: str
    token_type: str = Field(default="bearer", description="Always ``bearer``.")
    expires_in: int = Field(..., description="Access-token lifetime in seconds.")
    issued_at: datetime


class RefreshRequest(BaseModel):
    """Body of ``POST /auth/refresh``.

    The refresh token travels in the body rather than the ``Authorization``
    header so that a proxy or access log configured to capture bearer tokens does
    not end up holding the long-lived credential as well as the short-lived one.
    """

    refresh_token: str = Field(..., min_length=16, max_length=4096)


class PasswordChangeRequest(BaseModel):
    """Body of ``POST /auth/change-password``.

    The current password is required even though the caller is already
    authenticated: an access token left open on an unattended terminal should not
    be enough to take an account over permanently.
    """

    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(
        ...,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=256,
        description=(
            f"At least {MIN_PASSWORD_LENGTH} characters and at most "
            f"{MAX_PASSWORD_BYTES} UTF-8 bytes. Length is the only rule."
        ),
    )

    @field_validator("new_password")
    @classmethod
    def _check_new_password_bytes(cls, value: str) -> str:
        return _reject_long_password(value)

    @model_validator(mode="after")
    def _must_actually_change(self) -> "PasswordChangeRequest":
        if self.current_password == self.new_password:
            raise ValueError("new password must differ from the current one")
        return self


class RoleOut(BaseModel):
    """A role as an administrator sees it, with its capabilities spelled out."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: UserRole
    display_name: str
    description: Optional[str] = None
    scope: str = Field(..., description="SELF, DISTRICT, STATE or GLOBAL.")
    permissions: List[str] = Field(
        default_factory=list,
        description="Effective permissions from the server-side matrix.",
    )

    @classmethod
    def from_role(cls, role: Any) -> "RoleOut":
        described = rbac.describe_role(role.name)
        return cls(
            id=role.id,
            name=role.name,
            display_name=role.display_name,
            description=role.description,
            scope=str(described["scope"]),
            permissions=list(described["permissions"]),
        )


class UserOut(BaseModel):
    """The authenticated caller's own identity and effective access.

    ``state_name`` and ``district_name`` are included so the UI can show "Kamrup
    Metropolitan (Assam)" in the header without a second round trip — the header
    is how an officer notices they are looking at the wrong jurisdiction.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    username: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    designation: Optional[str] = None
    preferred_language: str = "en"
    is_active: bool = True
    is_superuser: bool = False

    roles: List[UserRole] = Field(default_factory=list)
    scope: str = Field(default="SELF", description="Widest scope across roles.")
    permissions: List[str] = Field(default_factory=list)

    state_id: Optional[uuid.UUID] = None
    state_name: Optional[str] = None
    district_id: Optional[uuid.UUID] = None
    district_name: Optional[str] = None

    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_user(cls, user: Any) -> "UserOut":
        held = list(user.role_names)
        return cls(
            id=user.id,
            email=user.email,
            username=user.username,
            full_name=user.full_name,
            phone=user.phone,
            designation=user.designation,
            preferred_language=user.preferred_language,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            roles=held,
            scope=rbac.widest_scope(held).name,
            permissions=sorted(str(p) for p in rbac.permissions_for(held)),
            state_id=user.state_id,
            state_name=user.state.name if user.state is not None else None,
            district_id=user.district_id,
            district_name=user.district.name if user.district is not None else None,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
        )


class MessageOut(BaseModel):
    """A bare acknowledgement, used where there is nothing to return."""

    detail: str
