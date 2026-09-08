"""Declarative base, naming conventions and shared column mixins.

Two decisions here matter for the life of the project:

1. **Explicit constraint naming.** Postgres auto-generates names for indexes and
   constraints; Alembic then cannot reliably drop or alter them across
   environments. The naming convention below makes every generated DDL name
   deterministic.

2. **Provenance as a column, not a comment.** Any table that stores an
   observation, prediction or simulated value mixes in
   :class:`ProvenanceMixin`, so the API can label data REAL / SYNTHETIC /
   SIMULATED / PREDICTED without guessing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.enums import DataProvenance

#: Deterministic names for every index and constraint Alembic emits.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    All timestamps are stored as ``TIMESTAMPTZ``. Mixing naive and aware
    datetimes is the most common source of wrong ETAs, so the codebase never
    produces a naive datetime.
    """

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Common declarative base for every model."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[type, Any]] = {
        dict[str, Any]: JSONB,
        datetime: sa.TIMESTAMP(timezone=True),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def pg_enum(python_enum: type, name: str) -> sa.Enum:
    """Build a native Postgres enum from a Python enum.

    ``values_callable`` makes Postgres store the enum *value* (e.g. ``road:read``)
    instead of the Python member name, which keeps the database readable and
    stable if a member is ever renamed in code.
    """

    return sa.Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
        create_constraint=False,
    )


class UUIDPrimaryKeyMixin:
    """Surrogate UUID primary key.

    UUIDs rather than serial integers because field devices create records
    offline and must be able to mint an id before they ever reach the server —
    which is what makes the offline sync queue in Phase 8 conflict-free.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Server-side created/updated timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=sa.text("now()"),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=sa.text("now()"),
        onupdate=utcnow,
    )


class ProvenanceMixin:
    """Honest labelling of where a value came from.

    ``provenance`` answers "is this real?", ``source`` names the concrete
    upstream ("open-meteo", "synthetic-generator-v1", "field-report"), and
    ``observed_at`` is when the fact was true in the world — as opposed to
    ``created_at``, which is when we happened to store it. Data freshness shown
    in the UI is computed from ``observed_at``.
    """

    provenance: Mapped[DataProvenance] = mapped_column(
        pg_enum(DataProvenance, "data_provenance"),
        nullable=False,
        default=DataProvenance.SYNTHETIC,
        index=True,
    )
    source: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    observed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True, index=True
    )


class ConfidenceMixin:
    """Confidence attached to any inferred or aggregated value.

    Stored 0-1. Every risk score, ETA and status derived from incomplete data
    must expose a confidence so the dashboard can distinguish "safe" from
    "we do not know".
    """

    confidence: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.5
    )

