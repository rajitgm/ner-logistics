"""Commodities and shipments.

Commodity priority is a database row, not a constant. The values in the spec
(emergency medicine 10, oxygen 10, water 10, food 8, agricultural produce 6,
construction material 5) are seeded as *prototype defaults* that an authorised
administrator can retune, because priority ordering is a policy decision that
should never require a code deploy.

A shipment records both its predicted and its actual ETA. Keeping the error
alongside them is what makes the analytics claim in Phase 14 measurable instead
of asserted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import RiskLevel, ShipmentPriorityProfile, ShipmentStatus
from app.db.base import Base, ProvenanceMixin, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.fleet import Vehicle
    from app.models.routing import Route

class Commodity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A class of goods with a configurable dispatch priority."""

    __tablename__ = "commodities"
    __table_args__ = (
        sa.CheckConstraint(
            "default_priority >= 1 AND default_priority <= 10",
            name="default_priority_range",
        ),
    )

    name: Mapped[str] = mapped_column(
        sa.String(160), nullable=False, unique=True, index=True
    )
    #: Grouping for analytics filters, e.g. ``MEDICAL``, ``FOOD``, ``FUEL``.
    category: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    #: 1 (lowest) to 10 (highest). Seeded from the spec's prototype defaults and
    #: editable by an administrator holding ``config:manage``.
    default_priority: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=5
    )
    unit: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="tonnes")

    is_perishable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_hazardous: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    requires_cold_chain: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    #: Marks life-critical cargo; these shipments default to the EMERGENCY weight
    #: profile, where reliability dominates ETA.
    is_life_critical: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    #: Maximum acceptable delay before the consignment loses its value.
    max_acceptable_delay_hours: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    shipments: Mapped[List["Shipment"]] = relationship(back_populates="commodity")

class Shipment(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A consignment moving from an origin to a destination."""

    __tablename__ = "shipments"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
        sa.CheckConstraint(
            "priority >= 1 AND priority <= 10", name="shipment_priority_range"
        ),
        sa.Index("ix_shipments_status_priority", "status", "priority"),
        sa.Index("ix_shipments_deadline", "deadline_at"),
    )

    #: Human-facing identifier such as ``NER-SHP-2026-000123``.
    reference_code: Mapped[str] = mapped_column(
        sa.String(40), nullable=False, unique=True, index=True
    )

    commodity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("commodities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="tonnes")
    #: Effective priority for this consignment. Defaults from the commodity but may
    #: be raised for a specific emergency; the change is audit-logged.
    priority: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=5)
    #: Which configurable weight profile the routing engine should optimise with.
    priority_profile: Mapped[ShipmentPriorityProfile] = mapped_column(
        pg_enum(ShipmentPriorityProfile, "shipment_priority_profile"),
        nullable=False,
        default=ShipmentPriorityProfile.NORMAL,
    )

    status: Mapped[ShipmentStatus] = mapped_column(
        pg_enum(ShipmentStatus, "shipment_status"),
        nullable=False,
        default=ShipmentStatus.PLANNED,
        index=True,
    )

    # ------------------------------------------------------- origin / destination
    origin_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    origin_geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    origin_district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    destination_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    destination_geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    destination_district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    vehicle_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("vehicles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Latest known position of the consignment, mirrored from the vehicle track.
    current_location: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    current_location_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    # --------------------------------------------------------------- timing / ETA
    planned_departure_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Latest useful arrival. Breaching it is what makes a shipment "at risk".
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Current best estimate. Revised whenever risk or an incident changes; the
    #: full revision history lives in ``eta_predictions``.
    predicted_eta: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    actual_eta: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Signed minutes: positive means later than predicted.
    eta_error_minutes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    # ------------------------------------------------------------- route and risk
    #: Currently committed route. ``use_alter`` because ``routes`` also references
    #: ``shipments``, so the constraint must be added after both tables exist.
    active_route_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("routes.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    #: Cached worst-case risk along the committed route, for list views.
    route_risk_score: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    route_risk_level: Mapped[Optional[RiskLevel]] = mapped_column(
        pg_enum(RiskLevel, "risk_level"), nullable=True
    )
    reroute_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    commodity: Mapped["Commodity"] = relationship(back_populates="shipments")
    vehicle: Mapped[Optional["Vehicle"]] = relationship(back_populates="shipments")
    active_route: Mapped[Optional["Route"]] = relationship(
        "Route",
        foreign_keys=[active_route_id],
        post_update=True,
    )
    routes: Mapped[List["Route"]] = relationship(
        "Route",
        back_populates="shipment",
        foreign_keys="Route.shipment_id",
        cascade="all, delete-orphan",
    )

    @property
    def is_overdue(self) -> bool:
        """Whether the current estimate already breaches the deadline.

        Answers "which shipments are in trouble right now" without recomputing a
        route, which is what the command centre needs on every refresh.
        """

        if self.deadline_at is None:
            return False
        reference = self.actual_eta or self.predicted_eta
        return reference is not None and reference > self.deadline_at

