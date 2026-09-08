"""Password hashing and JSON Web Token issuing/verification.

Deliberately narrow: this module turns a password into a hash, a claim set into a
signed token, and a token back into claims. It knows nothing about requests,
which is why the FastAPI dependencies in ``app.api.deps`` can be tested against
it without a running server, and why ``scripts/seed.py`` can reuse it.

Three decisions worth stating.

**bcrypt directly, not passlib.** passlib's bcrypt backend breaks against
bcrypt >= 4.1 — it reads a private ``__about__`` attribute that was removed — and
we need exactly two functions from it. One fewer dependency to pin, one fewer
compatibility trap.

**No silent truncation.** bcrypt hashes at most the first 72 bytes of a password
and ignores the rest, so a long passphrase and the same passphrase with a
different 80th character are the same secret. Rather than pre-hashing around it,
``hash_password`` refuses an over-long password; the request schema enforces the
same bound so the user sees a validation error instead of a 500.

**Tokens carry a version, not just a subject.** ``token_version`` is compared
against the user row on every request, so revoking access — a compromised
account, a role removed, a driver leaving — is one ``UPDATE`` rather than a
distributed blacklist. Roles are embedded for display and logging only; the
permission check always re-reads them from the database.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import bcrypt
import jwt

from app.core.config import get_settings

__all__ = [
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "PasswordPolicyError",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenClaims",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "generate_password",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
]

#: bcrypt ignores everything past this many bytes of the password.
MAX_PASSWORD_BYTES = 72
#: Long enough to be worth the bcrypt cost; short enough that field officers on a
#: phone keyboard will comply. Length is the only rule — no character-class
#: theatre, which pushes people toward "Password1!" and a sticky note.
MIN_PASSWORD_LENGTH = 12

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


class PasswordPolicyError(ValueError):
    """The supplied password cannot be hashed as given."""


class TokenError(Exception):
    """Base class for every token failure.

    Callers catch this and return 401. Subclasses exist so the *log* can say
    which failure it was without the *response* telling an attacker.
    """


class TokenExpiredError(TokenError):
    """Signature was valid but the token is past its expiry."""


class TokenInvalidError(TokenError):
    """Malformed, mis-signed, wrong issuer/audience, or the wrong token type."""


# ------------------------------------------------------------------- passwords


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of ``plain``.

    Cost comes from ``BCRYPT_ROUNDS`` so it can be raised as hardware improves
    without touching code. Raises ``PasswordPolicyError`` rather than truncating.
    """

    if not plain:
        raise PasswordPolicyError("password must not be empty")
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    encoded = plain.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 "
            "encoded; bcrypt silently ignores anything beyond that"
        )
    rounds = get_settings().BCRYPT_ROUNDS
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    """Constant-time check of ``plain`` against a stored hash.

    A missing or corrupt hash returns ``False`` instead of raising: a user row
    without a usable password (an invited account, a truncated import) must fail
    authentication, not crash the endpoint. ``bcrypt.checkpw`` is already
    constant-time with respect to the hash contents.
    """

    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def needs_rehash(hashed: Optional[str]) -> bool:
    """Whether ``hashed`` was produced with fewer rounds than we now require.

    Called after a successful login so the cost factor can be raised for the
    whole user base gradually, without a password reset campaign.
    """

    if not hashed:
        return True
    try:
        rounds = int(hashed.split("$")[2])
    except (IndexError, ValueError):
        return True
    return rounds < get_settings().BCRYPT_ROUNDS


def generate_password(length: int = 20) -> str:
    """A random password for the bootstrap administrator.

    ``token_urlsafe`` gives roughly 1.3 characters per byte, so ask for enough
    bytes and trim. Used only by the seeder, which prints the value once and
    never stores it in plaintext.
    """

    if length < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"generated password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    return secrets.token_urlsafe(length)[:length]


# ---------------------------------------------------------------------- tokens


@dataclass(frozen=True)
class TokenClaims:
    """Validated claims from an incoming token.

    Frozen because nothing downstream should be able to widen a caller's own
    identity by mutating what the token said.
    """

    subject: uuid.UUID
    token_type: str
    token_version: int
    roles: Tuple[str, ...]
    jti: str
    issued_at: datetime
    expires_at: datetime
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_access(self) -> bool:
        return self.token_type == TOKEN_TYPE_ACCESS

    @property
    def is_refresh(self) -> bool:
        return self.token_type == TOKEN_TYPE_REFRESH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(
    subject: uuid.UUID,
    token_type: str,
    lifetime: timedelta,
    token_version: int,
    roles: Iterable[str],
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Sign one token.

    ``iss`` and ``aud`` are set and verified on the way back in, so a token minted
    for a different service in the same cluster — or a stale token from a previous
    deployment with a different audience — is rejected rather than accepted by
    accident. ``jti`` is present so a single token can be denied by id later
    without inventing an identifier retroactively.
    """

    settings = get_settings()
    issued = _now()
    payload: Dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "ver": token_version,
        "roles": sorted(roles),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": issued,
        "nbf": issued,
        "exp": issued + lifetime,
        "jti": uuid.uuid4().hex,
    }
    if extra:
        # Reserved claims are ours; a caller cannot smuggle a different subject.
        payload.update({k: v for k, v in extra.items() if k not in payload})
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    subject: uuid.UUID,
    token_version: int = 0,
    roles: Iterable[str] = (),
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Short-lived token presented on every API call."""

    settings = get_settings()
    return _encode(
        subject,
        TOKEN_TYPE_ACCESS,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        token_version,
        roles,
        extra,
    )


def create_refresh_token(
    subject: uuid.UUID,
    token_version: int = 0,
    roles: Iterable[str] = (),
) -> str:
    """Longer-lived token whose only power is to mint access tokens.

    Roles are included for audit continuity, not for authorisation: the refresh
    endpoint re-reads the user's roles before issuing anything.
    """

    settings = get_settings()
    return _encode(
        subject,
        TOKEN_TYPE_REFRESH,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        token_version,
        roles,
    )


def decode_token(token: str, expected_type: Optional[str] = None) -> TokenClaims:
    """Verify ``token`` and return its claims.

    Every check PyJWT can do is switched on explicitly rather than left to
    defaults, because the defaults have changed between major versions and an
    unverified ``aud`` is the kind of regression that passes every test.

    ``expected_type`` closes the confusion attack where a refresh token — which
    lives for days — is presented as an access token to a data endpoint.
    """

    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={
                "require": ["exp", "iat", "nbf", "sub", "typ", "iss", "aud", "jti"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, wrong algorithm, wrong issuer/audience, missing
        # required claim and malformed input. Deliberately one message outward.
        raise TokenInvalidError("token is not valid") from exc

    token_type = payload.get("typ")
    if expected_type is not None and token_type != expected_type:
        raise TokenInvalidError(
            f"expected a {expected_type} token, got {token_type!r}"
        )

    try:
        subject = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenInvalidError("token subject is not a valid identifier") from exc

    roles = payload.get("roles") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise TokenInvalidError("token roles claim is malformed")

    version = payload.get("ver", 0)
    if not isinstance(version, int):
        raise TokenInvalidError("token version claim is malformed")

    return TokenClaims(
        subject=subject,
        token_type=str(token_type),
        token_version=version,
        roles=tuple(roles),
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        raw=payload,
    )
