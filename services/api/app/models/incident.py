"""Incidents, their photographic evidence, and raw field reports.

The two-table split is deliberate:

``field_reports``  the raw submission from a person in the field, possibly created
                  offline hours earlier. Immutable evidence of what someone said.
``incidents``      the canonical operational event that the risk engine consumes.
                  Created or confirmed from one or more reports.

Keeping them separate is what lets an unverified report be visible to a district
officer without it silently altering the risk of a national highway. Only an
incident whose ``verification_status`` is VERIFIED may act as a deterministic
override.
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
    IncidentSeverity,
    IncidentType,
    SyncStatus,
    VerificationStatus,
)
from app.db.base import (
    Base,
    ConfidenceMixin,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.road import RoadSegment

class Incident(
    UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, ConfidenceMixin, Base
):
    """A disruption event affecting the network."""

    __tablename__ = "incidents"
    __table_args__ = (
        sa.Index("ix_incidents_active_type", "is_active", "incident_type"),
        sa.Index("ix_incidents_segment_active", "road_segment_id", "is_active"),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= started_at",
            name="resolution_after_start",
        ),
    )

    incident_type: Mapped[IncidentType] = mapped_column(
        pg_enum(IncidentType, "incident_type"), nullable=False, index=True
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        pg_enum(IncidentSeverity, "incident_severity"),
        nullable=False,
        default=IncidentSeverity.MODERATE,
    )
    title: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    #: Optional extent for area-wide events such as inundation, so the simulator
    #: and alert fan-out can find every segment touched, not just the nearest one.
    affected_area: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    road_segment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("road_segments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # --------------------------------------------------------------- verification
    verification_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.PENDING,
        index=True,
    )
    reported_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    # ------------------------------------------------------------------- lifecycle
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    estimated_clearance_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True
    )

    # ---------------------------------------------------------------------- impact
    #: When true *and* verified, the linked segment is forced to BLOCKED. This is
    #: the single deterministic switch that outranks the ML prediction.
    causes_closure: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    lanes_blocked: Mapped[Optional[int]] = mapped_column(sa.SmallInteger, nullable=True)
    #: Additional delay attributed to this incident, used by the ETA engine when
    #: the segment remains passable.
    delay_minutes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Speed multiplier applied while active, 0-1. 0.4 means traffic crawls at 40%.
    speed_factor: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    #: Set when the incident was injected by the what-if simulator. Simulated
    #: incidents are excluded from analytics and from ML training sets.
    is_simulated: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    #: Groups every object created by one simulator run so the scenario can be
    #: rolled back cleanly.
    simulation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    road_segment: Mapped[Optional["RoadSegment"]] = relationship(
        back_populates="incidents",
        primaryjoin="RoadSegment.id == Incident.road_segment_id",
    )
    photos: Mapped[List["IncidentPhoto"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    field_reports: Mapped[List["FieldReport"]] = relationship(
        back_populates="incident"
    )

    @property
    def is_authoritative(self) -> bool:
        """Whether this incident may deterministically override a prediction.

        Requires an active, verified, non-simulated record. Anything else informs
        the probabilistic side of the risk engine only.
        """

        return (
            self.is_active
            and not self.is_simulated
            and self.verification_status == VerificationStatus.VERIFIED
        )

class FieldReport(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A geo-tagged submission from a field officer or driver.

    Reports are the offline-first entry point. The mobile client mints
    ``client_report_id`` locally, which makes the upload idempotent: replaying a
    queued report after a flaky connection cannot create duplicates.
    """

    __tablename__ = "field_reports"
    __table_args__ = (
        sa.UniqueConstraint(
            "reporter_id", "client_report_id", name="uq_field_reports_reporter_client"
        ),
        sa.Index("ix_field_reports_status_captured", "sync_status", "captured_at"),
    )

    #: UUID generated on the device before submission.
    client_report_id: Mapped[Optional[str]] = mapped_column(
        sa.String(64), nullable=True
    )
    reporter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    incident_type: Mapped[IncidentType] = mapped_column(
        pg_enum(IncidentType, "incident_type"), nullable=False
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        pg_enum(IncidentSeverity, "incident_severity"),
        nullable=False,
        default=IncidentSeverity.MODERATE,
    )
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    #: GPS horizontal accuracy in metres. A report with poor accuracy is still
    #: accepted but is snapped to a segment with lower confidence.
    gps_accuracy_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: When the observation was made on the device — not when it reached us.
    captured_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, index=True
    )
    #: When the server received it. The gap is the offline latency.
    received_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    verification_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.PENDING,
        index=True,
    )
    sync_status: Mapped[SyncStatus] = mapped_column(
        pg_enum(SyncStatus, "sync_status"), nullable=False, default=SyncStatus.SYNCED
    )
    #: Whether the report was composed while the device had no connectivity.
    created_offline: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    device_info: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: Language the reporter wrote in, so free text can be translated for others.
    language: Mapped[str] = mapped_column(sa.String(10), nullable=False, default="en")

    incident: Mapped[Optional["Incident"]] = relationship(
        back_populates="field_reports"
    )
    photos: Mapped[List["IncidentPhoto"]] = relationship(
        back_populates="field_report", cascade="all, delete-orphan"
    )

class IncidentPhoto(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """Photographic evidence attached to an incident or field report.

    The classification columns are intentionally nullable and unpopulated in the
    MVP. No vision model is configured, so nothing here claims automatic
    detection of blockage, flooding or bridge damage; a human sets the incident
    type. The columns exist so a vision adapter can fill them later without a
    migration, and ``classified_by_model`` records which model did it.
    """

    __tablename__ = "incident_photos"
    __table_args__ = (
        sa.CheckConstraint(
            "incident_id IS NOT NULL OR field_report_id IS NOT NULL",
            name="photo_has_parent",
        ),
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    field_report_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("field_reports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Path relative to ``UPLOAD_DIR``; never a client-supplied absolute path.
    storage_path: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    thumbnail_path: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    #: Validated server-side by sniffing content, not by trusting the extension.
    mime_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    #: SHA-256 of the bytes; used to deduplicate re-uploads from a retrying device.
    checksum_sha256: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)

    captured_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Location the photo was taken, which may differ from the report location.
    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    exif: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    #: Reserved for a future vision adapter. Null in the MVP.
    auto_classification: Mapped[Optional[str]] = mapped_column(
        sa.String(60), nullable=True
    )
    auto_classification_confidence: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    classified_by_model: Mapped[Optional[str]] = mapped_column(
        sa.String(120), nullable=True
    )

    incident: Mapped[Optional["Incident"]] = relationship(back_populates="photos")
    field_report: Mapped[Optional["FieldReport"]] = relationship(
        back_populates="photos"
    )

