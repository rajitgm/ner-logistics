"""The road network: named roads and their routable segments.

``road_segments`` is the busiest table in the platform and the one the map reads.
It carries three distinct kinds of column, deliberately kept separate:

* **Static physical attributes** (surface, width, max weight, bridge flags) —
  change only when the network is re-imported from OSM.
* **Derived exposure** (weather / flood / landslide exposure) — recomputed when
  hazard or forecast data changes, not per request.
* **A cached current risk verdict** (score, level, confidence, timestamp) —
  denormalised so rendering thousands of segments does not require a join. The
  authoritative time series lives in ``risk_scores``; this is the latest value
  only, and ``risk_updated_at`` is what the UI shows as data freshness.

``official_closure`` is the deterministic override that outranks any model
output: when it is true the status is BLOCKED regardless of what the ML
prediction says.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    RiskLevel,
    RoadStatus,
    RoadType,
    SurfaceType,
    TerrainClass,
)
from app.db.base import Base, ProvenanceMixin, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.incident import Incident

class Road(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A named corridor, e.g. NH-6 Shillong-Silchar.

    Roads exist so the dashboard can talk about corridors the way officials do
    ("NH-6 is at risk") while routing operates on the finer segment graph.
    """

    __tablename__ = "roads"

    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    #: Official designation such as ``NH-6``, ``NH-27``, ``SH-1``.
    ref: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True, index=True)
    road_type: Mapped[RoadType] = mapped_column(
        pg_enum(RoadType, "road_type"), nullable=False, default=RoadType.OTHER
    )
    #: NHAI, PWD, BRO, etc. Determines who is notified about a closure.
    managing_authority: Mapped[Optional[str]] = mapped_column(
        sa.String(120), nullable=True
    )
    state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("states.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    total_length_km: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: OSM relation id when the corridor was imported from OpenStreetMap.
    osm_relation_id: Mapped[Optional[int]] = mapped_column(sa.BigInteger, nullable=True)

    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True),
        nullable=True,
    )

    segments: Mapped[List["RoadSegment"]] = relationship(
        back_populates="road",
        cascade="all, delete-orphan",
        order_by="RoadSegment.sequence",
    )

class RoadSegment(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A routable stretch of road — the edge of the routing graph."""

    __tablename__ = "road_segments"
    __table_args__ = (
        sa.CheckConstraint(
            "risk_score >= 0 AND risk_score <= 100", name="risk_score_range"
        ),
        sa.CheckConstraint(
            "risk_confidence >= 0 AND risk_confidence <= 1",
            name="risk_confidence_range",
        ),
        sa.CheckConstraint("length_m > 0", name="length_positive"),
        sa.Index("ix_road_segments_status_risk", "status", "risk_score"),
    )

    road_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("roads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Position along the parent road, for ordered traversal and human labels.
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    name: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    osm_way_id: Mapped[Optional[int]] = mapped_column(
        sa.BigInteger, nullable=True, index=True
    )

    state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("states.id", ondelete="SET NULL"), nullable=True
    )
    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ------------------------------------------------------------------- geometry
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=True),
        nullable=False,
    )
    start_point: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    end_point: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    #: Geodesic length in metres, computed once on import via
    #: ``ST_Length(geom::geography)``. Cached because it is read on every route
    #: scoring pass.
    length_m: Mapped[float] = mapped_column(sa.Float, nullable=False)

    # ------------------------------------------------- static physical attributes
    road_type: Mapped[RoadType] = mapped_column(
        pg_enum(RoadType, "road_type"), nullable=False, default=RoadType.OTHER
    )
    surface: Mapped[SurfaceType] = mapped_column(
        pg_enum(SurfaceType, "surface_type"), nullable=False, default=SurfaceType.UNKNOWN
    )
    lanes: Mapped[Optional[int]] = mapped_column(sa.SmallInteger, nullable=True)
    width_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    is_bridge: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_tunnel: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_oneway: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    #: Unrestricted design speed, before any risk or weather penalty.
    free_flow_speed_kmph: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=40.0
    )
    #: 0-100 pavement quality proxy; 100 is a well-maintained sealed surface.
    condition_index: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    # -------------------------------------------------- vehicle restriction limits
    #: Null means "no published limit", which the compatibility check treats as
    #: unrestricted. A vehicle exceeding any non-null limit makes the segment
    #: impassable for that vehicle and the route is rejected, not merely penalised.
    max_weight_tonnes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    max_height_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    max_width_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Free-form additional restrictions, e.g.
    #: ``{"no_night_travel": true, "convoy_only": true, "seasonal_closure":
    #: {"from": "06-15", "to": "09-30"}}``.
    vehicle_restrictions: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )

    # ------------------------------------------------------ terrain and hydrology
    elevation_min_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    elevation_max_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    elevation_mean_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    slope_mean_deg: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    slope_max_deg: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    terrain_class: Mapped[TerrainClass] = mapped_column(
        pg_enum(TerrainClass, "terrain_class"),
        nullable=False,
        default=TerrainClass.PLAIN,
    )
    #: Distance to the nearest mapped watercourse. A strong flood predictor and an
    #: input feature to the ML model.
    distance_to_water_m: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )

    # ------------------------------------------------------ accessibility verdict
    status: Mapped[RoadStatus] = mapped_column(
        pg_enum(RoadStatus, "road_status"),
        nullable=False,
        default=RoadStatus.UNKNOWN,
        index=True,
    )
    #: Deterministic override. Set only from a VERIFIED incident or an
    #: authenticated authority action, and it outranks every model prediction.
    official_closure: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    official_closure_reason: Mapped[Optional[str]] = mapped_column(
        sa.Text, nullable=True
    )
    official_closure_until: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Who or what last set ``status`` — e.g. ``risk-engine``, ``field-report``,
    #: ``officer:<uuid>``. Every change is also written to ``audit_logs``.
    status_source: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    status_updated_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    # ------------------------------------------------- cached risk (latest verdict)
    risk_score: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    risk_level: Mapped[RiskLevel] = mapped_column(
        pg_enum(RiskLevel, "risk_level"), nullable=False, default=RiskLevel.LOW
    )
    risk_confidence: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    risk_updated_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Per-factor breakdown of the cached score, so the map popup can explain the
    #: verdict without recomputing: ``{"landslide": 26, "flood": 12, ...}``.
    risk_factors: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # -------------------------------------------------------- hazard exposure 0-100
    #: Standing exposure of this segment to each hazard family, recomputed when
    #: hazard zones or forecasts change rather than per request.
    weather_exposure: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    flood_exposure: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    landslide_exposure: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )

    road: Mapped["Road"] = relationship(back_populates="segments")
    incidents: Mapped[List["Incident"]] = relationship(
        back_populates="road_segment",
        primaryjoin="RoadSegment.id == Incident.road_segment_id",
        viewonly=True,
    )

    @property
    def is_traversable(self) -> bool:
        """Whether any vehicle may currently use this segment.

        A BLOCKED or officially closed segment is removed from the routing graph
        entirely. HIGH_RISK is still traversable — it is penalised by the cost
        function instead, because refusing every risky road in the monsoon would
        leave much of the region unreachable.
        """

        return not self.official_closure and self.status != RoadStatus.BLOCKED

    def admits_vehicle(
        self,
        weight_tonnes: Optional[float] = None,
        height_m: Optional[float] = None,
        width_m: Optional[float] = None,
    ) -> bool:
        """Hard physical compatibility check against published limits."""

        limits = (
            (self.max_weight_tonnes, weight_tonnes),
            (self.max_height_m, height_m),
            (self.max_width_m, width_m),
        )
        return all(
            limit is None or value is None or value <= limit for limit, value in limits
        )

