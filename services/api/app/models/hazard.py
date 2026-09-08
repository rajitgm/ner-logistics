"""Standing hazard exposure zones.

Distinct from ``incidents``: a hazard zone says "this area is landslide-prone",
an incident says "a landslide happened here at 14:20". Zones change rarely and
set the baseline exposure of every segment that intersects them; incidents are
transient and drive the live risk factors.

Phase 1 defines the schema. Real zonation would come from authorised geospatial
sources (for example ISRO/Bhuvan landslide susceptibility or flood hazard atlas
layers) through :class:`app.providers.hazard.HazardProvider`; until such access
exists the seeded zones are generated and labelled ``SYNTHETIC``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import HazardType
from app.db.base import (
    Base,
    ConfidenceMixin,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class HazardZone(
    UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, ConfidenceMixin, Base
):
    """An area with elevated susceptibility to a named hazard."""

    __tablename__ = "hazard_zones"
    __table_args__ = (
        sa.CheckConstraint(
            "severity_index >= 0 AND severity_index <= 100",
            name="severity_index_range",
        ),
        sa.Index("ix_hazard_zones_geom_gist", "geom", postgresql_using="gist"),
        sa.Index("ix_hazard_zones_type_severity", "hazard_type", "severity_index"),
    )

    hazard_type: Mapped[HazardType] = mapped_column(
        pg_enum(HazardType, "hazard_type"), nullable=False, index=True
    )
    name: Mapped[Optional[str]] = mapped_column(sa.String(200), nullable=True)
    #: 0-100 susceptibility. Feeds the flood/landslide risk factors directly, so
    #: the scale is documented here rather than reinterpreted per consumer.
    severity_index: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    #: Statistical recurrence where the source dataset provides one, e.g. a
    #: 100-year flood extent.
    return_period_years: Mapped[Optional[int]] = mapped_column(
        sa.Integer, nullable=True
    )

    #: Seasonal zones (monsoon flood extents) are only active inside this window;
    #: null bounds mean permanently active.
    active_from: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    active_to: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    #: Name and version of the dataset the polygon came from, so a zone can be
    #: retired when the source is superseded.
    dataset_name: Mapped[Optional[str]] = mapped_column(sa.String(160), nullable=True)
    dataset_version: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=False,
    )
