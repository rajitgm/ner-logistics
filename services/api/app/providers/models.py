"""Transport objects returned by provider adapters.

Plain frozen dataclasses, not Pydantic models, for two reasons. They are the
boundary between "outside" and "inside", and that boundary should not need a
web framework to exist — these types are importable and testable with nothing
installed. And they are not request bodies: nothing here is ever parsed from an
untrusted HTTP payload, so the validation machinery would earn nothing.

Field names deliberately match the corresponding SQLAlchemy columns in
``app.models``, so ingestion in later phases is an assignment rather than a
translation layer that can drift. Two things are *not* on these records:

* ``district_id`` / ``state_id`` — a provider knows a coordinate, not our
  administrative tree. Those are resolved by a spatial join at ingestion.
* database ids — a provider has no opinion about our primary keys. External
  identifiers travel as ``external_id`` / ``osm_way_id``, which is what
  de-duplication on re-ingest actually keys on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.enums import (
    AlertSeverity,
    DataProvenance,
    HazardType,
    IncidentSeverity,
    IncidentType,
    RoadType,
    SurfaceType,
)

__all__ = [
    "ProviderRecord",
    "WeatherObservationRecord",
    "WeatherForecastRecord",
    "WeatherAlertRecord",
    "HazardZoneRecord",
    "RoadFeatureRecord",
    "IncidentRecord",
    "Waypoint",
    "RouteLeg",
    "RoutingResult",
    "LLMMessage",
    "LLMToolCall",
    "LLMResponse",
    "NotificationRequest",
    "NotificationResult",
]

#: A GeoJSON geometry as a plain mapping. Providers hand back GeoJSON rather
#: than Shapely or WKB because it is what both HTTP sources and the frontend
#: speak, and because converting once at ingestion is cheaper than making every
#: adapter depend on GEOS.
GeoJSON = Dict[str, Any]


@dataclass(frozen=True)
class ProviderRecord:
    """What every record must say about itself before anything else.

    These three fields have no defaults on purpose. A provider cannot construct
    a record without stating whether the values are observed, generated,
    simulated, predicted or derived; where they came from; and when they were
    true in the world. ``observed_at`` is not ``created_at`` — data freshness in
    the UI is computed from the former, so a nine-hour-old forecast reads as
    nine hours old even if we stored it a second ago.
    """

    provenance: DataProvenance
    source: str
    observed_at: datetime


@dataclass(frozen=True)
class WeatherObservationRecord(ProviderRecord):
    """A point-in-time surface observation.

    ``rainfall_mm`` is the accumulation over the reporting interval, not an
    instantaneous rate, because that is what both IMD bulletins and Open-Meteo's
    hourly series report and what the flood factor in the risk engine consumes.
    """

    latitude: float
    longitude: float
    station_id: Optional[str] = None
    station_name: Optional[str] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    rainfall_mm: float = 0.0
    wind_speed_kmph: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    pressure_hpa: Optional[float] = None
    visibility_m: Optional[float] = None
    condition_code: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class WeatherForecastRecord(ProviderRecord):
    """One forecast bucket for one location.

    A forecast is ``PREDICTED`` provenance even when it comes from a real
    meteorological agency: it is a model output, not an observation, and the
    dashboard must be able to say so. ``issued_at`` and ``horizon_hours``
    together are what makes two forecasts for the same hour comparable.
    """

    latitude: float
    longitude: float
    issued_at: datetime
    valid_from: datetime
    valid_to: datetime
    horizon_hours: int
    model_name: Optional[str] = None
    rainfall_mm: float = 0.0
    precipitation_probability: Optional[float] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_speed_kmph: Optional[float] = None
    condition_code: Optional[str] = None


@dataclass(frozen=True)
class WeatherAlertRecord(ProviderRecord):
    """An official warning, or a generated stand-in for one.

    ``issuing_authority`` and ``external_id`` are what let a real IMD warning be
    told apart from a synthetic one in the same table — and what makes the
    deterministic override rule auditable: only an alert with a named authority
    is allowed to force a segment's status in the hybrid decision logic.
    """

    alert_type: str
    severity: AlertSeverity
    headline: str
    effective_from: datetime
    issuing_authority: Optional[str] = None
    external_id: Optional[str] = None
    description: Optional[str] = None
    effective_to: Optional[datetime] = None
    geometry: Optional[GeoJSON] = None
    raw_payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class HazardZoneRecord(ProviderRecord):
    """A standing exposure zone — a flood plain, a landslide-prone slope.

    Distinct from an incident: a hazard zone says "this area floods", an incident
    says "this area is flooded". The risk engine uses the first as a baseline
    multiplier and the second as a present-tense fact, and conflating them is
    how a road that has flooded once ends up permanently red.

    ``severity_index`` is 0-1 so a zone from a five-class published map and one
    from our generator can be compared. ``dataset_name`` and ``dataset_version``
    are mandatory reading for anyone citing this layer: they are how a number on
    screen is traced back to a specific published raster or a generator run.
    """

    hazard_type: HazardType
    severity_index: float
    geometry: GeoJSON
    name: Optional[str] = None
    return_period_years: Optional[int] = None
    active_from: Optional[datetime] = None
    active_to: Optional[datetime] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    notes: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class RoadFeatureRecord(ProviderRecord):
    """One road linework feature, before it is split into routable segments.

    Mirrors ``RoadSegment``'s physical columns. Everything derived — length,
    slope, terrain class, distance to water — is computed by the Phase 2 GIS
    pipeline in PostGIS, not asserted by the provider, because the provider has
    no elevation model. ``tags`` keeps the original OSM key/values so a later
    rule ("``maxweight`` was actually in kg here") can be applied without
    re-downloading the extract.
    """

    osm_way_id: Optional[int]
    geometry: GeoJSON
    name: Optional[str] = None
    ref: Optional[str] = None
    road_type: RoadType = RoadType.OTHER
    surface: SurfaceType = SurfaceType.UNKNOWN
    lanes: Optional[int] = None
    width_m: Optional[float] = None
    is_bridge: bool = False
    is_tunnel: bool = False
    is_oneway: bool = False
    max_weight_tonnes: Optional[float] = None
    max_height_m: Optional[float] = None
    max_width_m: Optional[float] = None
    free_flow_speed_kmph: Optional[float] = None
    tags: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class IncidentRecord(ProviderRecord):
    """A disruption reported by something other than our own field officers.

    ``is_authoritative`` is the field the hybrid decision rules turn on: an
    incident from a named agency feed may override an ML prediction, an incident
    scraped from an unverified source may not. An adapter that cannot establish
    the difference must leave it ``False``.
    """

    incident_type: IncidentType
    severity: IncidentSeverity
    latitude: float
    longitude: float
    started_at: datetime
    external_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    causes_closure: bool = False
    is_authoritative: bool = False
    estimated_clearance_at: Optional[datetime] = None
    attributes: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Waypoint:
    """A coordinate handed to a routing engine.

    Longitude first is a trap in this domain — GeoJSON, OSRM and PostGIS all use
    ``(lon, lat)`` while humans and every UI form use ``(lat, lon)``. The field
    names are explicit and :meth:`as_lon_lat` is the only place the order flips,
    so the mistake can be made in exactly one function.
    """

    latitude: float
    longitude: float
    name: Optional[str] = None

    def as_lon_lat(self) -> Tuple[float, float]:
        return (self.longitude, self.latitude)


@dataclass(frozen=True)
class RouteLeg:
    """One waypoint-to-waypoint portion of a routing answer."""

    distance_km: float
    duration_minutes: float
    #: Road-network identifiers the engine traversed, when it reports them.
    #: OSRM does with the right profile; the straight-line fallback does not.
    osm_way_ids: Sequence[int] = ()


@dataclass(frozen=True)
class RoutingResult(ProviderRecord):
    """A geometric path with its unweighted cost.

    This is deliberately *not* a recommendation. A routing provider answers
    "how do you physically get from A to B and how long is it" — free-flow
    distance and duration over the network. Risk, reliability, vehicle
    compatibility and commodity priority are applied by the Phase 6 scoring
    layer on top of this, which is why the platform can score candidates the
    same way whether they came from OSRM, GraphHopper or the fallback.

    ``degraded`` is the honest flag: when it is true the geometry is not a real
    road path (see the straight-line provider) and no ETA built on it should be
    presented without saying so.
    """

    distance_km: float
    duration_minutes: float
    geometry: GeoJSON
    legs: List[RouteLeg] = field(default_factory=list)
    degraded: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class LLMMessage:
    """One turn of a conversation sent to a language model."""

    role: str
    content: str


@dataclass(frozen=True)
class LLMToolCall:
    """A tool the model is asking us to run — a request, never an action.

    The assistant is a natural-language interface over validated backend tools,
    not an executor. This object crosses back into Phase 12's dispatcher, which
    checks the name against an allowlist, validates ``arguments`` against a
    schema, and applies the caller's own permissions before anything runs. A
    model asking for a tool it may not use is an ordinary, expected event.
    """

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """What a model returned.

    ``text`` may be empty when the model only asked for tools. ``model`` and
    ``usage`` are recorded so an answer shown to an operator can be attributed
    to a specific local model at a specific size — "the AI said so" is not
    something this platform will ever print.
    """

    text: str
    model: str
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class NotificationRequest:
    """An outbound message, already rendered.

    Channel selection and recipient resolution happen in the Phase 10 alert
    engine; a notification provider only delivers. ``dedupe_key`` lets a
    transport drop repeats of the same alert to the same person — a landslide
    that keeps re-scoring should not page a district officer eleven times.
    """

    channel: str
    recipient: str
    body: str
    subject: Optional[str] = None
    severity: Optional[AlertSeverity] = None
    dedupe_key: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class NotificationResult:
    """Outcome of one delivery attempt.

    ``delivered`` means the transport accepted it, which for SMS means a gateway
    queued it — not that a human read it. The distinction is kept because the
    notifications table stores SENT and DELIVERED as different states.
    """

    channel: str
    delivered: bool
    provider: str
    detail: Optional[str] = None
    external_id: Optional[str] = None
