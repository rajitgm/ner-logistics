"""Administrative geography: states and districts of the North Eastern Region.

Boundaries are the spatial frame for every scoped query in the platform: a
district officer's visibility, hazard-zone intersection, per-district analytics
and alert fan-out all resolve through these two tables.

Boundary geometry itself is loaded in Phase 2 from published administrative
boundary datasets; Phase 1 seeds the eight NER states and their districts as
attribute rows with ``geom`` left null, and the seeder labels them accordingly
rather than inventing polygons.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, ProvenanceMixin, TimestampMixin, UUIDPrimaryKeyMixin


class State(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A state or union territory."""

    __tablename__ = "states"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False, unique=True)
    #: ISO 3166-2:IN subdivision code, e.g. ``IN-AS`` for Assam.
    iso_code: Mapped[str] = mapped_column(sa.String(10), nullable=False, unique=True)
    #: Census of India state code, useful for joining published datasets.
    census_code: Mapped[Optional[str]] = mapped_column(sa.String(10), nullable=True)
    capital: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    #: Whether the state is part of the North Eastern Region.
    is_ner: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )

    districts: Mapped[List["District"]] = relationship(
        back_populates="state", cascade="all, delete-orphan"
    )

class District(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A district within a state.

    The district is the unit of operational accountability in the demo: incidents
    are verified by district officers, and alert routing uses
    ``ST_Intersects(district.geom, incident.geom)`` once boundaries are loaded.
    """

    __tablename__ = "districts"
    __table_args__ = (
        sa.UniqueConstraint("state_id", "name", name="uq_districts_state_id_name"),
        sa.Index("ix_districts_geom_gist", "geom", postgresql_using="gist"),
    )

    state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("states.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    census_code: Mapped[Optional[str]] = mapped_column(sa.String(10), nullable=True)
    headquarters: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    population: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    #: Set when a district is served only by roads that close seasonally, which
    #: raises the baseline reliability penalty applied by the routing engine.
    is_remote: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    #: Representative point, used to place map labels and as a routing fallback
    #: when a shipment names a district rather than an exact address.
    centroid: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=True,
    )

    state: Mapped["State"] = relationship(back_populates="districts")

