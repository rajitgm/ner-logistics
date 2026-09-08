"""Weather observations, forecasts and official weather alerts.

Three tables rather than one because they answer different questions and carry
different provenance:

* ``weather_observations`` — what was measured. ``REAL`` when a live source is
  connected, ``SYNTHETIC`` otherwise.
* ``weather_forecasts`` — what is expected. Always ``PREDICTED``; every row keeps
  the issuing model and horizon so forecast skill can be scored later.
* ``weather_alerts`` — what an authority has warned about. These are inputs to
  the deterministic side of the risk engine, not to the ML side.

Nothing here claims a government feed. The default provider is the synthetic
generator; IMD and Open-Meteo adapters are selected by configuration, and the
``source`` column always records which one actually produced the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AlertSeverity
from app.db.base import (
    Base,
    ConfidenceMixin,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

class WeatherObservation(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A point-in-time weather measurement at a location."""

    __tablename__ = "weather_observations"
    __table_args__ = (
        sa.Index("ix_weather_obs_district_observed", "district_id", "observed_at"),
        sa.CheckConstraint("rainfall_mm >= 0", name="rainfall_non_negative"),
    )

    #: Upstream station identifier where one exists.
    station_id: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    station_name: Mapped[Optional[str]] = mapped_column(sa.String(160), nullable=True)
    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )

    temperature_c: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    humidity_pct: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Accumulation in the hour ending at ``observed_at``.
    rainfall_mm: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    #: Rolling 24-hour accumulation — the stronger flood and landslide predictor,
    #: since saturation matters more than instantaneous intensity.
    rainfall_24h_mm: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    wind_speed_kmph: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    wind_direction_deg: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    pressure_hpa: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    visibility_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Free-text/coded summary from the provider, e.g. ``heavy_rain``.
    condition_code: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

class WeatherForecast(
    UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, ConfidenceMixin, Base
):
    """An expected weather state over a future interval.

    ``issued_at`` plus ``horizon_hours`` are kept alongside the validity window so
    a stale forecast can be identified and so forecast error can be measured
    against ``weather_observations`` once both exist.
    """

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        sa.Index("ix_weather_fc_district_valid", "district_id", "valid_from"),
        sa.CheckConstraint("valid_to > valid_from", name="valid_window_ordered"),
        sa.CheckConstraint("horizon_hours >= 0", name="horizon_non_negative"),
    )

    district_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("districts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )

    issued_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    valid_to: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    horizon_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: Name of the numerical model or generator that produced the forecast.
    model_name: Mapped[Optional[str]] = mapped_column(sa.String(80), nullable=True)

    rainfall_mm: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    precipitation_probability: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    temperature_c: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    humidity_pct: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    wind_speed_kmph: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    condition_code: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)

class WeatherAlert(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, Base):
    """A warning issued by a meteorological authority over an area.

    Treated as authoritative input on the deterministic side of the risk engine:
    an active severe warning raises a segment's weather factor directly rather
    than waiting for the model to infer it. ``issuing_authority`` and
    ``external_id`` keep the row attributable, and ``provenance`` stays
    ``SYNTHETIC`` until a real feed is genuinely connected.
    """

    __tablename__ = "weather_alerts"
    __table_args__ = (
        sa.UniqueConstraint(
            "issuing_authority", "external_id", name="uq_weather_alerts_authority_ext"
        ),
        sa.Index("ix_weather_alerts_window", "effective_from", "effective_to"),
        sa.Index("ix_weather_alerts_geom_gist", "geom", postgresql_using="gist"),
    )

    issuing_authority: Mapped[Optional[str]] = mapped_column(
        sa.String(120), nullable=True
    )
    external_id: Mapped[Optional[str]] = mapped_column(sa.String(120), nullable=True)
    #: e.g. ``HEAVY_RAINFALL_WARNING``, ``FLOOD_WATCH``, ``THUNDERSTORM``.
    alert_type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(
        pg_enum(AlertSeverity, "alert_severity"),
        nullable=False,
        default=AlertSeverity.WARNING,
        index=True,
    )
    headline: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    effective_from: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    effective_to: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    geom: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

