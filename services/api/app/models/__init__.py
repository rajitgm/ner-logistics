"""Model package: imports every mapped class so the metadata is complete.

Alembic autogenerate only sees tables that have been imported, and SQLAlchemy
cannot resolve a string-annotated relationship to a class that was never loaded.
Both problems have the same fix, and it is this module: importing
``app.models`` registers all 30 tables on ``Base.metadata`` in one step.

Ordering follows dependency direction — geography, then identity, then the road
network, then the data that describes it, then the operational objects that move
along it — so reading top to bottom is a reasonable tour of the schema.
"""

from __future__ import annotations

from app.db.base import (
    Base,
    ConfidenceMixin,
    ProvenanceMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.models.alerting import Alert, Notification
from app.models.fleet import Vehicle, VehiclePosition
from app.models.geography import District, State
from app.models.hazard import HazardZone
from app.models.incident import FieldReport, Incident, IncidentPhoto
from app.models.risk import EtaPrediction, RiskScore
from app.models.road import Road, RoadSegment
from app.models.routing import Route, RouteCandidate, RouteEvent
from app.models.shipment import Commodity, Shipment
from app.models.system import (
    AuditLog,
    ModelVersion,
    RiskFactorWeight,
    RoutingWeightProfile,
    SyncQueueEntry,
)
from app.models.user import Role, User, user_roles
from app.models.weather import WeatherAlert, WeatherForecast, WeatherObservation

__all__ = [  # noqa: RUF022 - grouped by domain for the public model API
    # base and mixins
    "Base",
    "UUIDPrimaryKeyMixin",
    "TimestampMixin",
    "ProvenanceMixin",
    "ConfidenceMixin",
    # geography and identity
    "State",
    "District",
    "Role",
    "User",
    "user_roles",
    # road network
    "Road",
    "RoadSegment",
    # environmental inputs
    "WeatherObservation",
    "WeatherForecast",
    "WeatherAlert",
    "HazardZone",
    # field intelligence
    "Incident",
    "IncidentPhoto",
    "FieldReport",
    # fleet and cargo
    "Vehicle",
    "VehiclePosition",
    "Commodity",
    "Shipment",
    # routing and derived predictions
    "RouteCandidate",
    "Route",
    "RouteEvent",
    "RiskScore",
    "EtaPrediction",
    # notification
    "Alert",
    "Notification",
    # platform
    "AuditLog",
    "SyncQueueEntry",
    "ModelVersion",
    "RoutingWeightProfile",
    "RiskFactorWeight",
]

#: The 27 tables the specification names, in the order it lists them, plus the
#: three the design adds. Kept as data so a test can assert coverage against the
#: live metadata instead of a reviewer counting by hand.
SPEC_TABLES: tuple[str, ...] = (
    "users",
    "roles",
    "roads",
    "road_segments",
    "districts",
    "states",
    "weather_observations",
    "weather_forecasts",
    "weather_alerts",
    "hazard_zones",
    "incidents",
    "incident_photos",
    "field_reports",
    "vehicles",
    "vehicle_positions",
    "shipments",
    "commodities",
    "routes",
    "route_candidates",
    "route_events",
    "risk_scores",
    "eta_predictions",
    "alerts",
    "notifications",
    "audit_logs",
    "sync_queue",
    "model_versions",
)

#: Tables beyond the specified set, each with the requirement that forced it.
ADDITIONAL_TABLES: dict[str, str] = {
    "user_roles": "many-to-many association; a district officer may also drive",
    "routing_weight_profiles": "'store configurable weights', not hardcoded policy",
    "risk_factor_weights": "per-factor ceilings must be tunable without a deploy",
}

