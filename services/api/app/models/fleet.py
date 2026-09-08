"""Fleet: vehicles and their position history.

``vehicles`` carries the physical envelope that makes a route legal or illegal —
gross weight, height, width — because in the North East a bridge weight limit or
a low tunnel is a hard constraint, not a preference. A candidate route containing
one incompatible segment is rejected outright rather than scored badly.

``vehicle_positions`` is an append-only time series. Every row records where it
came from: ``SIMULATED`` for the Phase 9 GPS simulator, ``REAL`` once actual
telemetry is wired in. The dashboard shows that label, so a demo is never
mistaken for live tracking.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import VehicleOperationalStatus, VehicleType
from app.db.base import Base, ProvenanceMixin, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.shipment import Shipment

class Vehicle(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A transport asset with a physical envelope and an operational state."""

    __tablename__ = "vehicles"
    __table_args__ = (
        sa.CheckConstraint(
            "capacity_tonnes IS NULL OR capacity_tonnes > 0", name="capacity_positive"
        ),
        sa.Index("ix_vehicles_type_status", "vehicle_type", "operational_status"),
    )

    registration_number: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, unique=True, index=True
    )
    vehicle_type: Mapped[VehicleType] = mapped_column(
        pg_enum(VehicleType, "vehicle_type"), nullable=False, index=True
    )
    make_model: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    operator_name: Mapped[Optional[str]] = mapped_column(sa.String(160), nullable=True)

    # -------------------------------------------------------- physical envelope
    #: Payload the vehicle may legally carry.
    capacity_tonnes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Laden weight, compared against segment ``max_weight_tonnes``.
    gross_weight_tonnes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    height_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    width_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    length_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    fuel_type: Mapped[Optional[str]] = mapped_column(sa.String(30), nullable=True)
    #: Litres per 100 km, the basis of the cost term in route scoring.
    fuel_consumption_l_per_100km: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    #: Realistic sustained speed for this class on NER terrain, used before any
    #: segment-specific speed is known.
    avg_speed_kmph: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=35.0
    )
    #: Emergency classes may use corridors that are CAUTION or HIGH_RISK when a
    #: normal shipment would be rerouted.
    is_emergency_class: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    # ------------------------------------------------------------- assignment
    operational_status: Mapped[VehicleOperationalStatus] = mapped_column(
        pg_enum(VehicleOperationalStatus, "vehicle_operational_status"),
        nullable=False,
        default=VehicleOperationalStatus.AVAILABLE,
    )
    driver_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    home_district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Telemetry device identifier, null while the vehicle is simulator-driven.
    gps_device_id: Mapped[Optional[str]] = mapped_column(sa.String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    # ------------------------------------------- denormalised latest position
    #: Cached so the fleet layer renders without scanning the position history.
    #: Authoritative history remains in ``vehicle_positions``.
    last_position: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    last_position_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    last_speed_kmph: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    positions: Mapped[List["VehiclePosition"]] = relationship(
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="VehiclePosition.recorded_at.desc()",
    )
    shipments: Mapped[List["Shipment"]] = relationship(back_populates="vehicle")

    def envelope(self) -> dict[str, Optional[float]]:
        """Physical dimensions in the shape the segment check expects."""

        return {
            "weight_tonnes": self.gross_weight_tonnes,
            "height_m": self.height_m,
            "width_m": self.width_m,
        }


class VehiclePosition(UUIDPrimaryKeyMixin, ProvenanceMixin, Base):
    """One GPS fix.

    Append-only and high volume, so it deliberately omits ``TimestampMixin``:
    ``recorded_at`` is the only time that matters and a second index would cost
    write throughput for nothing.
    """

    __tablename__ = "vehicle_positions"
    __table_args__ = (
        sa.Index("ix_vehicle_positions_vehicle_time", "vehicle_id", "recorded_at"),
        sa.CheckConstraint(
            "speed_kmph IS NULL OR speed_kmph >= 0", name="speed_non_negative"
        ),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Set while the vehicle is executing a consignment, so a track can be
    #: replayed per shipment.
    shipment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("shipments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Nearest segment, resolved on ingest so corridor occupancy is a cheap query.
    road_segment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("road_segments.id", ondelete="SET NULL"),
        nullable=True,
    )

    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    #: Server receive time; the gap from ``recorded_at`` reveals buffered uploads
    #: from a vehicle that was out of coverage.
    ingested_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    speed_kmph: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    heading_deg: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    altitude_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    accuracy_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    odometer_km: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    vehicle: Mapped["Vehicle"] = relationship(back_populates="positions")

