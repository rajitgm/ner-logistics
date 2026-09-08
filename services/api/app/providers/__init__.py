"""External data adapters: one interface per kind of thing we do not own.

Every source of information that comes from outside this application enters
through this package — weather, the road network, hazard extents, incident feeds,
routing engines, the language model, and outbound message transports. Call sites
depend on the interfaces here and resolve concrete implementations through
:func:`~app.providers.registry.get_routing_provider` and its siblings, which is
what makes replacing generated data with an authorised government source a
configuration change rather than a redesign.

Three properties hold across the layer, and they are structural rather than
conventional:

*Every record is labelled.* :class:`~app.providers.models.ProviderRecord` requires
``provenance``, ``source`` and ``observed_at`` with no defaults, so there is no
code path that emits an unlabelled figure. A number on screen can always be
traced to whether it was observed, generated, simulated, predicted or derived.

*"Not configured" is not "broken".* An integration that exists as an interface and
nothing more raises :class:`~app.providers.base.ProviderNotConfigured` naming what
is missing, and :func:`~app.providers.registry.unconnected_integrations` lists
them all. Nothing substitutes synthetic data for a source that was asked for and
is absent.

*Imports stay cheap.* Adapters import only the standard library and
``app.core.enums`` at module scope; ``httpx`` is imported inside the methods that
use it. So the interfaces and the synthetic implementations can be exercised with
no database, no network and no web framework running — which is how the synthetic
rainfall field came to be calibrated against published normals instead of assumed
correct.

Concrete implementations live in their own modules (``app.providers.weather`` and
so on) and are imported directly by tests and by the registry; what this module
re-exports is the stable surface: the interfaces, the errors, the record types and
the resolution functions.
"""

from __future__ import annotations

from app.providers.base import (
    Provider,
    ProviderError,
    ProviderNotConfigured,
    ProviderResponseError,
    ProviderStatus,
    ProviderUnavailable,
    UnconfiguredProvider,
)
from app.providers.hazard import HazardProvider
from app.providers.incident import IncidentProvider
from app.providers.llm import LLMProvider
from app.providers.models import (
    HazardZoneRecord,
    IncidentRecord,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    NotificationRequest,
    NotificationResult,
    ProviderRecord,
    RoadFeatureRecord,
    RouteLeg,
    RoutingResult,
    Waypoint,
    WeatherAlertRecord,
    WeatherForecastRecord,
    WeatherObservationRecord,
)
from app.providers.notification import NotificationProvider, default_channels
from app.providers.osm import OSMProvider
from app.providers.registry import (
    KNOWN_IMPLEMENTATIONS,
    PROVIDER_KINDS,
    all_providers,
    close_providers,
    get_hazard_provider,
    get_incident_provider,
    get_llm_provider,
    get_notification_provider,
    get_osm_provider,
    get_routing_provider,
    get_weather_provider,
    provider_health,
    provider_report,
    reset_providers,
    unconnected_integrations,
)
from app.providers.routing import RoutingProvider, haversine_km
from app.providers.weather import WeatherProvider

__all__ = [  # noqa: RUF022 - grouped by provider lifecycle for discoverability
    # base
    "Provider",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderResponseError",
    "ProviderStatus",
    "ProviderUnavailable",
    "UnconfiguredProvider",
    # interfaces
    "WeatherProvider",
    "OSMProvider",
    "HazardProvider",
    "IncidentProvider",
    "RoutingProvider",
    "LLMProvider",
    "NotificationProvider",
    # records
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
    # resolution
    "PROVIDER_KINDS",
    "KNOWN_IMPLEMENTATIONS",
    "all_providers",
    "get_weather_provider",
    "get_osm_provider",
    "get_hazard_provider",
    "get_incident_provider",
    "get_routing_provider",
    "get_llm_provider",
    "get_notification_provider",
    "provider_report",
    "provider_health",
    "unconnected_integrations",
    "reset_providers",
    "close_providers",
    # helpers
    "default_channels",
    "haversine_km",
]
