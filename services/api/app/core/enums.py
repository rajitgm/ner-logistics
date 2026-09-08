"""Canonical domain enumerations for the NER Logistics platform.

This module is intentionally dependency-free (standard library only) so that it
can be imported by the API, the engine packages, the ML pipeline and the test
suite without pulling in FastAPI, SQLAlchemy or numpy. Keeping it pure also
means the domain vocabulary stays testable in environments where third-party
packages are unavailable.

Rules encoded here:
  * Presentation concerns (colours, labels, translations) do NOT belong in this
    module. The backend emits semantic states; the frontend maps them to colour.
  * Every dataset surfaced to a user must carry a DataProvenance value so the UI
    can label REAL vs SYNTHETIC vs SIMULATED vs PREDICTED data honestly.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "UserRole",
    "Permission",
    "RoadStatus",
    "RiskLevel",
    "RiskFactor",
    "IncidentType",
    "IncidentSeverity",
    "VerificationStatus",
    "VehicleType",
    "VehicleOperationalStatus",
    "ShipmentStatus",
    "RouteStatus",
    "RouteEventType",
    "AlertType",
    "AlertSeverity",
    "NotificationChannel",
    "NotificationStatus",
    "SyncStatus",
    "DataProvenance",
    "ScenarioType",
    "RoadType",
    "SurfaceType",
    "TerrainClass",
    "HazardType",
    "ModelStage",
    "AuditAction",
    "ShipmentPriorityProfile",
]


class StrEnum(str, Enum):
    """String-valued enum that serialises to its value.

    Python 3.11 ships ``enum.StrEnum``; we define a compatible shim so the code
    runs on 3.10 (the version pinned in the API container) as well.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class UserRole(StrEnum):
    """Roles recognised by the platform.

    Roles are coarse-grained job functions; the fine-grained capability check is
    always done against :class:`Permission` via ``app.core.rbac``. Never gate an
    API purely on role name outside of the RBAC matrix.
    """

    ADMIN = "ADMIN"
    STATE_OFFICER = "STATE_OFFICER"
    DISTRICT_OFFICER = "DISTRICT_OFFICER"
    FIELD_OFFICER = "FIELD_OFFICER"
    DRIVER = "DRIVER"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    """Atomic capabilities enforced server-side.

    Naming convention is ``<resource>:<action>``. Read permissions are separated
    from mutating permissions so that a compromised viewer token can never change
    operational state.
    """

    # Road network and accessibility
    ROAD_READ = "road:read"
    ROAD_STATUS_OVERRIDE = "road:status_override"

    # Risk, weather, routing
    RISK_READ = "risk:read"
    WEATHER_READ = "weather:read"
    ROUTE_READ = "route:read"
    ROUTE_PLAN = "route:plan"
    ROUTE_OVERRIDE = "route:override"

    # Incidents and field intelligence
    INCIDENT_READ = "incident:read"
    INCIDENT_CREATE = "incident:create"
    INCIDENT_VERIFY = "incident:verify"
    INCIDENT_DELETE = "incident:delete"
    FIELD_REPORT_READ = "field_report:read"
    FIELD_REPORT_CREATE = "field_report:create"

    # Fleet
    VEHICLE_READ = "vehicle:read"
    VEHICLE_MANAGE = "vehicle:manage"
    VEHICLE_POSITION_WRITE = "vehicle:position_write"

    # Shipments
    SHIPMENT_READ = "shipment:read"
    SHIPMENT_CREATE = "shipment:create"
    SHIPMENT_UPDATE = "shipment:update"
    SHIPMENT_CANCEL = "shipment:cancel"

    # Alerts and notifications
    ALERT_READ = "alert:read"
    ALERT_CREATE = "alert:create"
    ALERT_ACKNOWLEDGE = "alert:acknowledge"

    # Analytics, simulation, assistant
    ANALYTICS_READ = "analytics:read"
    SIMULATION_RUN = "simulation:run"
    AI_QUERY = "ai:query"

    # Administration
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    ROLE_MANAGE = "role:manage"
    CONFIG_READ = "config:read"
    CONFIG_MANAGE = "config:manage"
    AUDIT_READ = "audit:read"
    MODEL_READ = "model:read"
    MODEL_MANAGE = "model:manage"
    SYNC_WRITE = "sync:write"


class RoadStatus(StrEnum):
    """Operational accessibility state of a road segment."""

    OPEN = "OPEN"
    CAUTION = "CAUTION"
    HIGH_RISK = "HIGH_RISK"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"

class RiskLevel(StrEnum):
    """Banded interpretation of a 0-100 risk score."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @classmethod
    def from_score(cls, score: float) -> "RiskLevel":
        """Map a normalised 0-100 risk score onto a band.

        Bands are deliberately defined in one place so the API, the risk engine
        and the ML evaluation code cannot drift apart. Scores are clamped rather
        than rejected because upstream engines may accumulate slightly above 100
        before normalisation.
        """

        if score < 0 or score > 100:
            score = min(100.0, max(0.0, float(score)))
        if score < 25:
            return cls.LOW
        if score < 50:
            return cls.MODERATE
        if score < 75:
            return cls.HIGH
        return cls.CRITICAL


class RiskFactor(StrEnum):
    """Canonical contributing factors behind a risk score.

    The risk engine must always return the per-factor breakdown alongside the
    total: a road is never described as risky without showing why. Per-factor
    maximum contributions are configuration (stored in the database), not
    constants, so policy can be tuned without a code change.
    """

    WEATHER = "weather"
    RAINFALL = "rainfall"
    FLOOD = "flood"
    LANDSLIDE = "landslide"
    TERRAIN = "terrain"
    ROAD_CONDITION = "road_condition"
    HISTORICAL_INCIDENT = "historical_incident"
    CURRENT_INCIDENT = "current_incident"
    TRAFFIC = "traffic"
    VEHICLE_COMPATIBILITY = "vehicle_compatibility"

class IncidentType(StrEnum):
    """Field-reportable and machine-detectable disruption categories."""

    LANDSLIDE = "LANDSLIDE"
    FLOOD = "FLOOD"
    ROAD_BLOCK = "ROAD_BLOCK"
    BRIDGE_DAMAGE = "BRIDGE_DAMAGE"
    ACCIDENT = "ACCIDENT"
    DEBRIS = "DEBRIS"
    HEAVY_RAIN = "HEAVY_RAIN"
    TRAFFIC = "TRAFFIC"
    ROAD_DAMAGE = "ROAD_DAMAGE"
    OTHER = "OTHER"


class IncidentSeverity(StrEnum):
    """Reported impact of an incident on trafficability."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    SEVERE = "SEVERE"


class VerificationStatus(StrEnum):
    """Moderation state of user-submitted intelligence.

    Only ``VERIFIED`` reports are allowed to act as deterministic overrides of an
    ML prediction (see the hybrid decision rules in docs/ARCHITECTURE.md).
    """

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class VehicleType(StrEnum):
    """Fleet classes with distinct physical and priority characteristics."""

    LIGHT_VEHICLE = "LIGHT_VEHICLE"
    TRUCK = "TRUCK"
    HEAVY_TRUCK = "HEAVY_TRUCK"
    AMBULANCE = "AMBULANCE"
    EMERGENCY_VEHICLE = "EMERGENCY_VEHICLE"


class VehicleOperationalStatus(StrEnum):
    """Availability of a vehicle for assignment."""

    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    EN_ROUTE = "EN_ROUTE"
    MAINTENANCE = "MAINTENANCE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"

class ShipmentStatus(StrEnum):
    """Lifecycle of a consignment."""

    PLANNED = "PLANNED"
    IN_TRANSIT = "IN_TRANSIT"
    DELAYED = "DELAYED"
    REROUTING = "REROUTING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class RouteStatus(StrEnum):
    """State of a route that has been committed to a shipment."""

    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class RouteEventType(StrEnum):
    """Auditable events on the timeline of a route."""

    CREATED = "CREATED"
    SELECTED = "SELECTED"
    RISK_INCREASED = "RISK_INCREASED"
    RISK_DECREASED = "RISK_DECREASED"
    SEGMENT_BLOCKED = "SEGMENT_BLOCKED"
    REROUTED = "REROUTED"
    ETA_REVISED = "ETA_REVISED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    COMPLETED = "COMPLETED"


class ShipmentPriorityProfile(StrEnum):
    """Named sets of routing objective weights.

    The numeric weights themselves live in the ``routing_weight_profiles``
    configuration rather than in code, so authorities can retune policy without a
    redeploy. These identifiers only name the profile.
    """

    NORMAL = "NORMAL"
    EMERGENCY = "EMERGENCY"
    BULK_ECONOMY = "BULK_ECONOMY"

class AlertType(StrEnum):
    """Reasons the platform raises an operational alert."""

    HIGH_RISK_ROAD = "HIGH_RISK_ROAD"
    ROAD_CLOSURE = "ROAD_CLOSURE"
    PREDICTED_DISRUPTION = "PREDICTED_DISRUPTION"
    SHIPMENT_DELAY = "SHIPMENT_DELAY"
    ROUTE_CHANGE = "ROUTE_CHANGE"
    WEATHER_WARNING = "WEATHER_WARNING"
    FIELD_INCIDENT = "FIELD_INCIDENT"
    EMERGENCY_REROUTE = "EMERGENCY_REROUTE"


class AlertSeverity(StrEnum):
    """Urgency of an alert, independent of any UI colour mapping."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


class NotificationChannel(StrEnum):
    """Delivery transports for an alert.

    ``SMS`` and ``PUSH`` require third-party credentials; the Phase 10 default
    implementation is an in-app/WebSocket channel plus a logging sink so no
    integration is claimed that is not actually configured.
    """

    IN_APP = "IN_APP"
    WEBSOCKET = "WEBSOCKET"
    EMAIL = "EMAIL"
    SMS = "SMS"
    PUSH = "PUSH"


class NotificationStatus(StrEnum):
    """Delivery state of an individual notification."""

    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"

class SyncStatus(StrEnum):
    """State of a queued offline mutation from the mobile client."""

    PENDING = "PENDING"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class DataProvenance(StrEnum):
    """Honest provenance label attached to every dataset and derived value.

    This is a first-class field, not documentation. Any value shown in the UI must
    be traceable to one of these labels:

    ``REAL``       observed from an actually connected external source
    ``SYNTHETIC``  generated by our own generator for development/demo
    ``SIMULATED``  produced by a what-if scenario or the GPS simulator
    ``PREDICTED``  output of a model, not an observation
    ``DERIVED``    deterministically computed from other labelled inputs
    """

    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"
    SIMULATED = "SIMULATED"
    PREDICTED = "PREDICTED"
    DERIVED = "DERIVED"


class ScenarioType(StrEnum):
    """What-if scenarios the simulator can inject."""

    HEAVY_RAINFALL = "HEAVY_RAINFALL"
    FLOOD = "FLOOD"
    LANDSLIDE = "LANDSLIDE"
    ROAD_CLOSURE = "ROAD_CLOSURE"
    BRIDGE_FAILURE = "BRIDGE_FAILURE"
    TRAFFIC_SPIKE = "TRAFFIC_SPIKE"


class HazardType(StrEnum):
    """Standing hazard exposure zones (as opposed to point-in-time incidents)."""

    FLOOD_PLAIN = "FLOOD_PLAIN"
    LANDSLIDE_SUSCEPTIBLE = "LANDSLIDE_SUSCEPTIBLE"
    SEISMIC = "SEISMIC"
    EROSION = "EROSION"
    HIGH_RAINFALL = "HIGH_RAINFALL"

class RoadType(StrEnum):
    """Road classification, bridging Indian administrative classes and OSM tags."""

    NATIONAL_HIGHWAY = "NATIONAL_HIGHWAY"
    STATE_HIGHWAY = "STATE_HIGHWAY"
    DISTRICT_ROAD = "DISTRICT_ROAD"
    RURAL_ROAD = "RURAL_ROAD"
    URBAN_ROAD = "URBAN_ROAD"
    TRACK = "TRACK"
    BRIDGE = "BRIDGE"
    TUNNEL = "TUNNEL"
    OTHER = "OTHER"


class SurfaceType(StrEnum):
    """Running surface, a direct input to the road-condition risk factor."""

    ASPHALT = "ASPHALT"
    CONCRETE = "CONCRETE"
    PAVED_OTHER = "PAVED_OTHER"
    GRAVEL = "GRAVEL"
    EARTH = "EARTH"
    UNPAVED_OTHER = "UNPAVED_OTHER"
    UNKNOWN = "UNKNOWN"


class TerrainClass(StrEnum):
    """Coarse terrain band derived from slope and elevation statistics."""

    PLAIN = "PLAIN"
    ROLLING = "ROLLING"
    HILLY = "HILLY"
    MOUNTAINOUS = "MOUNTAINOUS"
    STEEP = "STEEP"


class ModelStage(StrEnum):
    """Promotion stage of a registered ML model version.

    A model only influences user-facing output at ``ACTIVE``. ``SHADOW`` models are
    scored and logged for comparison but never drive a recommendation.
    """

    TRAINING = "TRAINING"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"

class AuditAction(StrEnum):
    """Actions written to the immutable audit trail.

    The spec requires an audit entry for road status changes, incident
    verification, route overrides, shipment modification, alert creation and user
    permission changes. Authentication outcomes and denied access attempts are
    included as well, since they are the events a reviewer looks for first.
    """

    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGOUT = "LOGOUT"
    TOKEN_REFRESH = "TOKEN_REFRESH"
    ACCESS_DENIED = "ACCESS_DENIED"

    ROAD_STATUS_CHANGE = "ROAD_STATUS_CHANGE"
    INCIDENT_VERIFICATION = "INCIDENT_VERIFICATION"
    ROUTE_OVERRIDE = "ROUTE_OVERRIDE"
    SHIPMENT_MODIFICATION = "SHIPMENT_MODIFICATION"
    ALERT_CREATION = "ALERT_CREATION"
    USER_PERMISSION_CHANGE = "USER_PERMISSION_CHANGE"

    CONFIG_CHANGE = "CONFIG_CHANGE"
    MODEL_PROMOTION = "MODEL_PROMOTION"
    SIMULATION_RUN = "SIMULATION_RUN"
    AI_TOOL_CALL = "AI_TOOL_CALL"

    ENTITY_CREATE = "ENTITY_CREATE"
    ENTITY_UPDATE = "ENTITY_UPDATE"
    ENTITY_DELETE = "ENTITY_DELETE"

