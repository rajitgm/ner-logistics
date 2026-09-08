"""Alerts and their per-recipient delivery records.

An alert is the operational statement — this corridor is closing, this shipment
will miss its deadline — and carries a recommended action, because an alert that
tells an officer something is wrong without saying what to do about it wastes the
one thing they are short of during a monsoon: time.

A notification is one attempt to put one alert in front of one person over one
channel. Splitting them keeps the alert single-sourced while fan-out to several
officers over several transports is tracked independently, and makes the honest
statement possible: the in-app and WebSocket channels work, while SMS, e-mail and
push remain unconfigured transports whose rows will simply record that they were
never dispatched.
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
    AlertSeverity,
    AlertType,
    NotificationChannel,
    NotificationStatus,
)
from app.db.base import (
    Base,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User

class Alert(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """An operational warning with a location, a cause and a recommended action."""

    __tablename__ = "alerts"
    __table_args__ = (
        # A repeated condition must not produce a new alert every evaluation
        # cycle. The engine computes a stable key from type plus subject, so a
        # persisting closure updates one row instead of flooding the centre.
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
        sa.Index("ix_alerts_active_severity", "is_active", "severity"),
        sa.Index("ix_alerts_triggered_at", "triggered_at"),
    )

    alert_type: Mapped[AlertType] = mapped_column(
        pg_enum(AlertType, "alert_type"), nullable=False, index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        pg_enum(AlertSeverity, "alert_severity"),
        nullable=False,
        default=AlertSeverity.WARNING,
        index=True,
    )
    #: Short enough for a notification headline.
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: What the recipient should do. Required by the spec and by common sense.
    recommended_action: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: Stable identity of the underlying condition, e.g.
    #: ``ROAD_CLOSURE:<segment_id>``. Null for one-off manual alerts.
    dedupe_key: Mapped[Optional[str]] = mapped_column(sa.String(200), nullable=True)

    # -------------------------------------------------------------- where and what
    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    #: Place name for recipients reading text rather than a map.
    location_name: Mapped[Optional[str]] = mapped_column(sa.String(200), nullable=True)
    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Subjects. All optional and independently indexed, because an alert may be
    #: about a corridor, a consignment, a reported event, or several at once.
    road_segment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("road_segments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    shipment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("shipments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("routes.id", ondelete="SET NULL"), nullable=True
    )
    #: Structured detail for the UI: risk before and after, affected shipment
    #: references, the alternate route offered.
    context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # ------------------------------------------------------------------ lifecycle
    triggered_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    #: After this, the condition is assumed stale and the alert stops showing as
    #: current. Null means it stays until explicitly resolved.
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Bumped each time the same condition is re-detected, so the centre can see
    #: "closure still in force, 6th update" without six rows.
    occurrence_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    # -------------------------------------------------------------- human handling
    acknowledged_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    acknowledgement_note: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: Null when raised by an engine; set when a person raised it by hand.
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_simulated: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    simulation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    notifications: Mapped[List["Notification"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )

    @property
    def is_current(self) -> bool:
        """Whether this alert should still be presented as live.

        Active and unexpired. Acknowledgement deliberately does not clear it: an
        officer confirming they have seen a landslide does not reopen the road.
        """

        if not self.is_active:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.now(tz=self.expires_at.tzinfo)


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One delivery attempt of one alert to one recipient over one channel.

    ``status`` records what actually happened. A row for an unconfigured
    transport stays ``QUEUED`` or becomes ``FAILED`` with a reason rather than
    being reported as sent, so the dashboard cannot imply an SMS gateway that
    does not exist.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.UniqueConstraint(
            "alert_id", "user_id", "channel", name="uq_notifications_alert_user_channel"
        ),
        sa.Index("ix_notifications_user_status", "user_id", "status"),
        sa.Index("ix_notifications_status_queued", "status", "created_at"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        pg_enum(NotificationChannel, "notification_channel"),
        nullable=False,
        default=NotificationChannel.IN_APP,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.QUEUED,
    )

    #: Rendered in the recipient's preferred language at dispatch time, so the
    #: message they were actually shown is the message that is stored.
    language: Mapped[str] = mapped_column(sa.String(10), nullable=False, default="en")
    rendered_title: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    rendered_body: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Only set by a channel that can actually confirm receipt.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    #: Why delivery failed, including "channel not configured".
    failure_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: Transport identifier from the provider, when one is returned.
    external_reference: Mapped[Optional[str]] = mapped_column(
        sa.String(160), nullable=True
    )

    alert: Mapped["Alert"] = relationship(back_populates="notifications")
    user: Mapped["User"] = relationship()

