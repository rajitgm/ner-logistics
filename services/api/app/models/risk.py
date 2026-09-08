"""Risk scores and ETA predictions — the two derived time series.

Both tables exist because the platform has to answer "why did you say that, and
were you right?" long after the fact. The denormalised copies on
``road_segments`` and ``shipments`` are caches for list views; these rows are the
record.

``risk_scores``      one appraisal of one segment at one moment, always stored
                     with its per-factor breakdown. A score without its factors
                     is not allowed to exist.
``eta_predictions``  one estimate for one shipment, with the delay attribution
                     that produced it and, later, the observed arrival. Keeping
                     predicted and actual side by side is what makes the accuracy
                     claim in Phase 14 measurable rather than asserted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import RiskLevel, VehicleType
from app.db.base import (
    Base,
    ConfidenceMixin,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

class RiskScore(UUIDPrimaryKeyMixin, ProvenanceMixin, ConfidenceMixin, Base):
    """A hybrid risk appraisal of one road segment at one point in time.

    Append-only, so no ``TimestampMixin``: ``computed_at`` is the only relevant
    time. The row records both halves of the hybrid decision — the probabilistic
    contribution from the model and whether a deterministic rule overrode it —
    because "an official verified closure outranks the prediction" has to be
    provable, not merely intended.
    """

    __tablename__ = "risk_scores"
    __table_args__ = (
        sa.Index("ix_risk_scores_segment_time", "road_segment_id", "computed_at"),
        sa.Index("ix_risk_scores_level_time", "risk_level", "computed_at"),
        sa.CheckConstraint(
            "risk_score >= 0 AND risk_score <= 100", name="risk_score_range"
        ),
    )

    road_segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("road_segments.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Null means the generic appraisal. A value means the score was computed for
    #: that class, because the vehicle-compatibility factor differs by envelope.
    vehicle_type: Mapped[Optional[VehicleType]] = mapped_column(
        pg_enum(VehicleType, "vehicle_type"), nullable=True
    )

    computed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, index=True
    )
    risk_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        pg_enum(RiskLevel, "risk_level"), nullable=False
    )

    # ------------------------------------------------------------- the breakdown
    #: Contribution of every :class:`~app.core.enums.RiskFactor` that applied,
    #: keyed by factor value, e.g. ``{"weather": 18.0, "flood": 12.0,
    #: "landslide": 26.0, "road_condition": 8.0, "historical_incident": 7.0,
    #: "current_incident": 15.0, "traffic": 5.0}`` summing to the total. Not
    #: nullable: the UI must always be able to explain the number.
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The raw inputs behind those contributions — rainfall in mm, slope in
    #: degrees, incident count — so a reviewer can recompute the score by hand.
    factor_inputs: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    #: Which configurable weight set was in force. Policy is data, not code.
    weight_set_version: Mapped[Optional[str]] = mapped_column(
        sa.String(40), nullable=True
    )

    # ------------------------------------------------------- hybrid attribution
    #: Model probability of disruption, 0-1, before banding. Null when no model
    #: was active and the score came entirely from rules.
    model_probability: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: True when a deterministic rule replaced the computed value — a verified
    #: closure, an official order. ``override_reason`` says which.
    rule_override_applied: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    override_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: What the score would have been without the override, kept so the
    #: model's behaviour can still be evaluated on overridden cases.
    pre_override_score: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )

    # -------------------------------------------------------------- validity window
    #: Scores are cheap to recompute but expensive to trust when stale. The API
    #: refuses to present a score past ``valid_until`` as current.
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Oldest observation that fed this score; surfaced as data freshness.
    oldest_input_observed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: True when produced inside a what-if run. Excluded from analytics and from
    #: ML training sets so a scenario can never contaminate measured accuracy.
    is_simulated: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    simulation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    @property
    def top_factors(self) -> list[tuple[str, float]]:
        """Contributing factors ordered by contribution, largest first.

        The explanation layer shows the leading three; ordering here keeps that
        choice out of the presentation code.
        """

        numeric = {
            key: float(value)
            for key, value in (self.factors or {}).items()
            if isinstance(value, (int, float))
        }
        return sorted(numeric.items(), key=lambda item: item[1], reverse=True)


class EtaPrediction(UUIDPrimaryKeyMixin, TimestampMixin, ProvenanceMixin, ConfidenceMixin, Base):
    """One arrival estimate for one shipment, with its delay attribution.

    The MVP fills this from the deterministic ETA engine (base travel time plus
    traffic, weather, incident and road-condition penalties). ``method`` and
    ``model_version_id`` exist so a learned estimator can take over later without
    a migration, and so the two can be compared on the same rows.
    """

    __tablename__ = "eta_predictions"
    __table_args__ = (
        sa.Index("ix_eta_predictions_shipment_time", "shipment_id", "predicted_at"),
        sa.CheckConstraint(
            "total_duration_minutes >= 0", name="eta_duration_non_negative"
        ),
    )

    shipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=False,
    )
    route_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    predicted_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    predicted_eta: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    #: Why this estimate was produced: ``INITIAL``, ``RISK_CHANGE``, ``REROUTE``,
    #: ``POSITION_UPDATE``. Explains a sequence of revisions in the timeline.
    trigger: Mapped[Optional[str]] = mapped_column(sa.String(40), nullable=True)

    # ---------------------------------------------------------- delay attribution
    #: Free-flow travel time for the route and vehicle class.
    base_duration_minutes: Mapped[float] = mapped_column(sa.Float, nullable=False)
    traffic_delay_minutes: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    weather_delay_minutes: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    incident_delay_minutes: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    road_condition_delay_minutes: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    #: Rest and handover time; separated so it is not mistaken for a disruption.
    rest_stop_minutes: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    #: Sum of the components above. Stored rather than computed so a historical
    #: row cannot silently change if the summation rule is revised.
    total_duration_minutes: Mapped[float] = mapped_column(sa.Float, nullable=False)
    #: Optional ± window in minutes, from the model or from segment variance.
    uncertainty_minutes: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )

    # -------------------------------------------------------------- outcome
    #: Backfilled on delivery. Null while the shipment is in flight.
    actual_arrival_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Signed: positive means the shipment arrived later than predicted.
    error_minutes: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    absolute_error_minutes: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )

    # ------------------------------------------------------------- provenance
    #: ``HEURISTIC`` for the deterministic engine, ``ML`` once a trained
    #: estimator is active, ``BLENDED`` if both contribute.
    method: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="HEURISTIC"
    )
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_simulated: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    simulation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    def delay_breakdown(self) -> dict[str, float]:
        """Delay components in the shape the ETA explanation expects.

        Returned even when every entry is zero, so the UI can state that nothing
        is currently slowing the shipment rather than showing an empty panel.
        """

        return {
            "base": self.base_duration_minutes,
            "traffic": self.traffic_delay_minutes,
            "weather": self.weather_delay_minutes,
            "incident": self.incident_delay_minutes,
            "road_condition": self.road_condition_delay_minutes,
            "rest_stops": self.rest_stop_minutes,
        }

