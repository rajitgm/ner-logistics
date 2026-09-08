"""Platform infrastructure tables: audit trail, offline sync, model registry, and
the configuration that holds routing and risk policy.

The last part is the important one. The spec's weight numbers — reliability 40%,
ETA 25%, risk 15%, distance 10%, cost 10% for a normal shipment, and a different
split for an emergency — are policy decisions belonging to the authority running
the platform, not facts about the world. They live in
``routing_weight_profiles`` and ``risk_factor_weights`` so an administrator can
retune them, with the change audit-logged, and no engineer has to ship a release
to change how the state prioritises its supply chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AuditAction,
    DataProvenance,
    ModelStage,
    RiskFactor,
    ShipmentPriorityProfile,
    SyncStatus,
)
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User

class AuditLog(UUIDPrimaryKeyMixin, Base):
    """An immutable record of a consequential action.

    Append-only by design: no ``updated_at``, no application code path that
    updates or deletes a row. The spec requires an entry for road status changes,
    incident verification, route overrides, shipment modification, alert creation
    and permission changes; authentication outcomes and denied access attempts are
    logged too, since those are what a reviewer looks for first.

    ``old_value`` and ``new_value`` hold only the fields that changed, never a
    whole record, and never a password hash or token.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_logs_actor_time", "actor_id", "occurred_at"),
        sa.Index("ix_audit_logs_action_time", "action", "occurred_at"),
        sa.Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, index=True
    )
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )
    #: Nullable so a failed login for an unknown username is still recorded.
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Login identifier as supplied, kept for failures where no user resolved.
    actor_label: Mapped[Optional[str]] = mapped_column(sa.String(160), nullable=True)
    #: Roles held at the time of the action; roles change, history should not.
    actor_roles: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)

    #: Table or domain name, e.g. ``road_segments``.
    entity_type: Mapped[Optional[str]] = mapped_column(sa.String(80), nullable=True)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: Human-readable identity of the subject, so a log line stays meaningful
    #: after the row it referred to has been deleted.
    entity_label: Mapped[Optional[str]] = mapped_column(sa.String(255), nullable=True)

    #: Changed fields only, before and after.
    old_value: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: Justification the actor gave, where the API demands one — an override
    #: reason, a rejection reason.
    reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    # ------------------------------------------------------------- request context
    #: False for denied or failed attempts, which are logged as attentively as
    #: successful ones.
    succeeded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    ip_address: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(sa.String(400), nullable=True)
    http_method: Mapped[Optional[str]] = mapped_column(sa.String(10), nullable=True)
    http_path: Mapped[Optional[str]] = mapped_column(sa.String(400), nullable=True)
    #: Correlates every log line produced while handling one request.
    request_id: Mapped[Optional[str]] = mapped_column(
        sa.String(64), nullable=True, index=True
    )
    #: Set when the action originated from an assistant tool call, so AI-initiated
    #: changes are distinguishable from ones a person made directly.
    ai_tool_name: Mapped[Optional[str]] = mapped_column(sa.String(80), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    actor: Mapped[Optional["User"]] = relationship()


class SyncQueueEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One mutation a mobile client made while offline, awaiting server apply.

    The device generates ``client_mutation_id`` before going offline, and the
    unique constraint on it makes replay harmless: a client that loses its
    connection mid-upload can send the whole queue again without creating
    duplicate incidents. This is the mechanism behind the offline claim, and it is
    only honest to claim offline support because the queue is real and the
    idempotency key is enforced by the database rather than by hope.
    """

    __tablename__ = "sync_queue"
    __table_args__ = (
        sa.UniqueConstraint(
            "device_id", "client_mutation_id", name="uq_sync_queue_device_mutation"
        ),
        sa.Index("ix_sync_queue_status_created", "status", "client_created_at"),
        sa.Index("ix_sync_queue_user_status", "user_id", "status"),
    )

    #: Who queued it. Kept nullable so an unauthenticated replay is recorded and
    #: rejected rather than silently dropped.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    device_id: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    client_mutation_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: Target of the mutation, e.g. ``field_reports``.
    entity_type: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    #: ``CREATE``, ``UPDATE`` or ``DELETE``.
    operation: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    #: UUID minted on the device, which is why the schema uses UUID keys
    #: throughout: an offline client can name a row before the server sees it.
    client_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    #: The mutation body exactly as the device sent it. Re-validated server-side
    #: on apply; an offline client is never trusted to have validated anything.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[SyncStatus] = mapped_column(
        pg_enum(SyncStatus, "sync_status"), nullable=False, default=SyncStatus.PENDING
    )
    #: When the user performed the action on the device — the operationally
    #: meaningful time, which may be hours before the upload.
    client_created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    received_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    #: Server-side row the mutation produced, so the device can reconcile.
    server_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: Set when the server rejected the client's version of reality — the row was
    #: changed by someone else in the meantime. Surfaced to the user rather than
    #: resolved silently, because a field observation is not safe to overwrite.
    conflict_detected: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    conflict_details: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )


class ModelVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered, reproducible ML artifact.

    Every prediction the platform shows points back to a row here, and every row
    carries ``training_data_provenance`` and ``evaluated_on``. That pairing is
    deliberate: a metric computed on synthetic data is recorded as such, so the UI
    and any report can say "0.87 AUC on synthetic validation data" instead of
    implying field-measured accuracy. No metric may be presented without the
    provenance of the data it was measured on.
    """

    __tablename__ = "model_versions"
    __table_args__ = (
        sa.UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        sa.Index("ix_model_versions_task_stage", "task", "stage"),
    )

    #: Registry name, e.g. ``segment_disruption``.
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    #: Monotonic label, e.g. ``v3`` or a semantic version.
    version: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    #: What it predicts: ``DISRUPTION``, ``ETA``, ``RELIABILITY``.
    task: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    #: ``LOGISTIC_REGRESSION``, ``RANDOM_FOREST``, ``XGBOOST`` — the three the
    #: spec requires be trained and compared.
    algorithm: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    stage: Mapped[ModelStage] = mapped_column(
        pg_enum(ModelStage, "model_stage"),
        nullable=False,
        default=ModelStage.TRAINING,
        index=True,
    )

    # ------------------------------------------------------------ reproducibility
    #: Ordered feature names exactly as the estimator expects them. Inference
    #: builds its vector from this list, so a reordered training script cannot
    #: silently mismatch a deployed model.
    feature_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    hyperparameters: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    #: Identifier of the exact dataset snapshot used, e.g. ``synthetic-2026.09-a``.
    dataset_version: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    training_rows: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    validation_rows: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    random_seed: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    trained_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    training_duration_seconds: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    #: Path under the models directory; never an absolute host path.
    artifact_path: Mapped[Optional[str]] = mapped_column(sa.String(500), nullable=True)
    artifact_sha256: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    #: Library versions the artifact was pickled with, so a load failure is
    #: diagnosable rather than mysterious.
    runtime_versions: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )

    # ---------------------------------------------------- metrics, honestly labelled
    #: Validation metrics keyed by name: accuracy, precision, recall, f1, roc_auc,
    #: plus MAE for regression tasks.
    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: Provenance of the data the model was TRAINED on. ``SYNTHETIC`` for every
    #: model in the prototype.
    training_data_provenance: Mapped[DataProvenance] = mapped_column(
        pg_enum(DataProvenance, "data_provenance"),
        nullable=False,
        default=DataProvenance.SYNTHETIC,
    )
    #: Provenance of the data the metrics were MEASURED on. Any accuracy figure
    #: shown to a user must be presented together with this label.
    metrics_data_provenance: Mapped[DataProvenance] = mapped_column(
        pg_enum(DataProvenance, "data_provenance"),
        nullable=False,
        default=DataProvenance.SYNTHETIC,
    )
    #: One line naming the held-out split, e.g. "temporal holdout, last 20% of the
    #: synthetic monsoon season".
    evaluation_protocol: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    #: Feature importances or coefficients, for the model card.
    feature_importances: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    # ------------------------------------------------------------------- promotion
    promoted_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    promoted_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    @property
    def influences_users(self) -> bool:
        """Whether this version may shape user-facing output.

        Only ``ACTIVE`` does. A ``SHADOW`` model is scored and logged alongside
        the active one for comparison but never drives a recommendation.
        """

        return self.stage == ModelStage.ACTIVE

    def metric_statement(self, metric: str) -> Optional[str]:
        """A metric rendered with the provenance of the data behind it.

        Returns, for example, ``"roc_auc 0.87 (measured on SYNTHETIC data)"``.
        Callers should prefer this over reading ``metrics`` directly, so a figure
        cannot reach a slide or an API response stripped of its caveat.
        """

        if not self.metrics or metric not in self.metrics:
            return None
        return (
            f"{metric} {self.metrics[metric]} "
            f"(measured on {self.metrics_data_provenance.value} data)"
        )


class RoutingWeightProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Configurable objective weights for route scoring.

    The prototype seeds the two profiles named in the spec — a normal shipment
    weighting reliability 0.40, ETA 0.25, risk 0.15, distance 0.10 and cost 0.10;
    an emergency weighting reliability 0.50, risk 0.30, ETA 0.15 and cost 0.05 —
    as *rows*, not constants. Superseding a profile creates a new version rather
    than editing the old one, so a past recommendation can still be explained
    under the policy that was actually in force when it was made.
    """

    __tablename__ = "routing_weight_profiles"
    __table_args__ = (
        sa.UniqueConstraint("profile", "version", name="uq_weight_profiles_version"),
        sa.CheckConstraint(
            "weight_reliability >= 0 AND weight_eta >= 0 AND weight_risk >= 0 "
            "AND weight_distance >= 0 AND weight_cost >= 0",
            name="weights_non_negative",
        ),
        # Weights are shares of one objective, so they must form a distribution.
        # Enforced in the database because a profile that does not sum to 1 makes
        # every comparison between candidates meaningless.
        sa.CheckConstraint(
            "abs((weight_reliability + weight_eta + weight_risk + weight_distance "
            "+ weight_cost) - 1.0) <= 0.0001",
            name="weights_sum_to_one",
        ),
    )

    profile: Mapped[ShipmentPriorityProfile] = mapped_column(
        pg_enum(ShipmentPriorityProfile, "shipment_priority_profile"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    label: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    #: Expected probability the corridor stays usable for the whole journey. The
    #: dominant term for a normal shipment, and more so for an emergency: a route
    #: that is fast when it works and impassable when it does not is a bad route
    #: for oxygen cylinders.
    weight_reliability: Mapped[float] = mapped_column(sa.Float, nullable=False)
    weight_eta: Mapped[float] = mapped_column(sa.Float, nullable=False)
    weight_risk: Mapped[float] = mapped_column(sa.Float, nullable=False)
    weight_distance: Mapped[float] = mapped_column(sa.Float, nullable=False)
    weight_cost: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: Candidates scoring above this risk are excluded outright unless the vehicle
    #: is an emergency class. Also policy, also configurable.
    max_acceptable_risk_score: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    #: Whether this profile may use CAUTION or HIGH_RISK corridors at all.
    allows_high_risk_segments: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True
    )
    effective_from: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Who last changed policy. The change itself is in ``audit_logs`` as a
    #: ``CONFIG_CHANGE``.
    updated_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def as_weights(self) -> dict[str, float]:
        """Weights in the shape the routing engine's scoring function expects."""

        return {
            "reliability": self.weight_reliability,
            "eta": self.weight_eta,
            "risk": self.weight_risk,
            "distance": self.weight_distance,
            "cost": self.weight_cost,
        }


class RiskFactorWeight(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Maximum contribution each factor may add to a 0-100 risk score.

    Rows, not constants, for the same reason as the routing weights: how much a
    landslide-susceptible slope should matter relative to yesterday's rainfall is
    a judgement the state's engineers are better placed to make than we are.

    The ceilings deliberately sum above 100 — the spec's own worked example
    reaches 91 from seven simultaneous factors — and the engine clamps the total.
    A factor set is versioned as a whole, and every score records which version
    produced it.
    """

    __tablename__ = "risk_factor_weights"
    __table_args__ = (
        sa.UniqueConstraint(
            "factor", "weight_set_version", name="uq_risk_factor_weights_version"
        ),
        sa.CheckConstraint(
            "max_contribution >= 0 AND max_contribution <= 100",
            name="max_contribution_range",
        ),
    )

    factor: Mapped[RiskFactor] = mapped_column(
        pg_enum(RiskFactor, "risk_factor"), nullable=False, index=True
    )
    #: Names the whole coherent set, e.g. ``baseline-v1``.
    weight_set_version: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    max_contribution: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: Value of the underlying measurement at which the factor reaches its
    #: ceiling — 150 mm of rain in 24 h, say. Below it the contribution scales.
    saturation_value: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: How it scales up to saturation: ``LINEAR``, ``SQRT`` or ``STEP``. Named
    #: rather than coded so a tuning change is a configuration edit.
    response_curve: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="LINEAR"
    )
    #: Below this measurement the factor contributes nothing, which keeps light
    #: drizzle from nudging a highway toward CAUTION.
    threshold_value: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: Multiplier applied when the factor's input is stale or missing, so absent
    #: data lowers confidence instead of silently reading as zero risk.
    missing_data_confidence_penalty: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    updated_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

