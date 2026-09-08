"""Which adapter each kind of external data resolves to.

Every consumer asks this module for a provider instead of constructing one, and
that indirection is the point of the whole layer: moving from generated weather
to a live source, or from the straight-line stand-in to a compiled OSRM graph,
is an ``.env`` change and a restart with no call site touched. It also puts the
question "what is this deployment actually connected to?" in one place.
:func:`provider_report` answers it without making a single network call, which is
what makes it safe on ``/health/ready`` and behind the dashboard's provenance
panel.

Two rules hold here.

*Settings tokens and ``implementation`` names are the same strings.*
``ROUTING_PROVIDER=stub`` selects the class whose ``implementation`` is
``"stub"``, so an ``.env`` file, a log line and a health payload all say the same
word. :data:`KNOWN_IMPLEMENTATIONS` mirrors the ``Literal`` unions in
``app.core.config`` so a test can fail when the two drift apart.

*Selecting an integration is not the same as implementing one.* Setting
``WEATHER_PROVIDER=imd`` returns the IMD adapter, and that adapter raises
:class:`~app.providers.base.ProviderNotConfigured`: supplying a base URL does not
write the client. Nothing here quietly substitutes synthetic data to keep an
endpoint returning 200 — a deployment that asks for a source it does not have
gets an error naming what is missing, which is the only behaviour compatible with
labelling data by provenance at all.

Imports stay cheap for the same reason as in the adapters: ``app.core.config`` is
imported inside the functions, so this module can be imported and the synthetic
providers exercised with no pydantic-settings, no database and no ``.env``.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, Dict, List, Tuple, Type, TypeVar

from app.providers.base import Provider, ProviderStatus, UnconfiguredProvider
from app.providers.hazard import (
    BhuvanHazardProvider,
    HazardProvider,
    SyntheticHazardProvider,
)
from app.providers.incident import (
    ExternalFeedIncidentProvider,
    IncidentProvider,
    InternalIncidentProvider,
)
from app.providers.llm import (
    DisabledLLMProvider,
    LLMProvider,
    OllamaLLMProvider,
    OpenAICompatibleLLMProvider,
    StubLLMProvider,
)
from app.providers.notification import (
    CompositeNotificationProvider,
    EmailNotificationProvider,
    LoggingNotificationProvider,
    NotificationProvider,
    PushNotificationProvider,
    SmsNotificationProvider,
)
from app.providers.osm import LocalExtractOSMProvider, OSMProvider, OverpassOSMProvider
from app.providers.routing import (
    GraphHopperRoutingProvider,
    OsrmRoutingProvider,
    RoutingProvider,
    StraightLineRoutingProvider,
    ValhallaRoutingProvider,
)
from app.providers.weather import (
    ImdWeatherProvider,
    OpenMeteoWeatherProvider,
    SyntheticWeatherProvider,
    WeatherProvider,
)

__all__ = [
    "KNOWN_IMPLEMENTATIONS",
    "PROVIDER_KINDS",
    "all_providers",
    "close_providers",
    "get_hazard_provider",
    "get_incident_provider",
    "get_llm_provider",
    "get_notification_provider",
    "get_osm_provider",
    "get_routing_provider",
    "get_weather_provider",
    "provider_health",
    "provider_report",
    "reset_providers",
    "unconnected_integrations",
]

#: Report order. Data sources first, then the engines built on them, then the
#: two optional extras — which is roughly the order an operator debugs in.
PROVIDER_KINDS: Tuple[str, ...] = (
    "weather",
    "osm",
    "hazard",
    "incident",
    "routing",
    "llm",
    "notification",
)

#: Accepted setting tokens per kind. Must match the ``Literal`` unions in
#: ``app.core.config`` exactly; a test asserts it, because a token accepted by
#: settings but unknown here would mean a valid ``.env`` crashes on first use.
#: ``notification`` has no setting — there is nothing to choose between until a
#: real transport exists — so it lists only what the factory builds.
KNOWN_IMPLEMENTATIONS: Dict[str, Tuple[str, ...]] = {
    "weather": ("synthetic", "imd", "openmeteo"),
    "osm": ("local_extract", "overpass"),
    "hazard": ("synthetic", "bhuvan"),
    "incident": ("internal", "external_feed"),
    "routing": ("osrm", "graphhopper", "valhalla", "stub"),
    "llm": ("ollama", "openai_compatible", "stub"),
    "notification": ("composite",),
}

#: Instances handed out so far, so :func:`close_providers` can release the HTTP
#: clients that some of them hold without instantiating the ones nobody used.
_LIVE: List[Provider] = []

_P = TypeVar("_P", bound=Provider)


def _track(provider: _P) -> _P:
    _LIVE.append(provider)
    return provider


def _unknown(kind: str, token: str) -> ValueError:
    accepted = ", ".join(KNOWN_IMPLEMENTATIONS[kind])
    return ValueError(f"unknown {kind} provider {token!r}; accepted: {accepted}")


# --------------------------------------------------------------------------- getters
# One cached instance per kind per process. Caching matters for the adapters that
# own an httpx client — a new client per request would leak connections — and is
# harmless for the generators. Tests that change settings must call
# :func:`reset_providers`.


@lru_cache(maxsize=1)
def get_weather_provider() -> WeatherProvider:
    """Resolve ``WEATHER_PROVIDER``.

    ``synthetic`` is the default and the only one that works with no network. The
    seed is not configurable: reproducibility is the property that makes generated
    weather defensible in a demo, and an operator-tunable seed invites tuning the
    numbers until a scenario looks convincing.
    """

    from app.core.config import get_settings

    settings = get_settings()
    token = settings.WEATHER_PROVIDER
    if token == "synthetic":
        return _track(SyntheticWeatherProvider())
    if token == "openmeteo":
        return _track(OpenMeteoWeatherProvider(base_url=settings.OPENMETEO_BASE_URL))
    if token == "imd":
        return _track(ImdWeatherProvider())
    raise _unknown("weather", token)


@lru_cache(maxsize=1)
def get_osm_provider() -> OSMProvider:
    """Resolve ``OSM_PROVIDER``.

    ``local_extract`` is the default because the road network is loaded once by
    ``scripts/prepare_osm.sh`` and then queried from PostGIS; Overpass exists for
    filling a gap interactively and refuses region-sized bounding boxes.
    """

    from app.core.config import get_settings

    settings = get_settings()
    token = settings.OSM_PROVIDER
    if token == "local_extract":
        return _track(LocalExtractOSMProvider(settings.OSM_EXTRACT_PATH))
    if token == "overpass":
        return _track(OverpassOSMProvider(settings.OVERPASS_BASE_URL))
    raise _unknown("osm", token)


@lru_cache(maxsize=1)
def get_hazard_provider() -> HazardProvider:
    """Resolve ``HAZARD_PROVIDER``. Default synthetic; Bhuvan is declared only."""

    from app.core.config import get_settings

    token = get_settings().HAZARD_PROVIDER
    if token == "synthetic":
        return _track(SyntheticHazardProvider())
    if token == "bhuvan":
        return _track(BhuvanHazardProvider())
    raise _unknown("hazard", token)


@lru_cache(maxsize=1)
def get_incident_provider() -> IncidentProvider:
    """Resolve ``INCIDENT_PROVIDER``.

    ``internal`` is not a stand-in for anything: field reports and operator
    entries are this platform's own records, read straight from the database. The
    adapter exists so that adding an agency feed later is a setting, and so the
    provenance panel can state plainly that no external incident source is
    connected.
    """

    from app.core.config import get_settings

    token = get_settings().INCIDENT_PROVIDER
    if token == "internal":
        return _track(InternalIncidentProvider())
    if token == "external_feed":
        return _track(ExternalFeedIncidentProvider())
    raise _unknown("incident", token)


@lru_cache(maxsize=1)
def get_routing_provider() -> RoutingProvider:
    """Resolve ``ROUTING_PROVIDER``.

    The default is ``osrm`` rather than the stub on purpose: a deployment with no
    graph should fail its readiness probe and say so, not silently serve
    straight-line distances that look like ETAs. Choosing ``stub`` is a deliberate
    act, and everything it returns is flagged ``degraded``.
    """

    from app.core.config import get_settings

    settings = get_settings()
    token = settings.ROUTING_PROVIDER
    if token == "osrm":
        return _track(
            OsrmRoutingProvider(
                settings.OSRM_BASE_URL,
                timeout_seconds=settings.ROUTING_TIMEOUT_SECONDS,
            )
        )
    if token == "stub":
        return _track(StraightLineRoutingProvider())
    if token == "graphhopper":
        return _track(GraphHopperRoutingProvider())
    if token == "valhalla":
        return _track(ValhallaRoutingProvider())
    raise _unknown("routing", token)


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Resolve ``LLM_PROVIDER``, or the off switch.

    ``LLM_ENABLED`` is checked first and wins: with the copilot disabled — the
    default — this returns :class:`~app.providers.llm.DisabledLLMProvider`
    whatever ``LLM_PROVIDER`` says, so the assistant endpoints answer 503. The
    core product does not call anything here, which is the requirement the flag
    exists to keep honest.
    """

    from app.core.config import get_settings

    settings = get_settings()
    if not settings.LLM_ENABLED:
        return _track(DisabledLLMProvider())
    token = settings.LLM_PROVIDER
    if token == "ollama":
        return _track(
            OllamaLLMProvider(
                settings.OLLAMA_BASE_URL,
                settings.LLM_MODEL,
                timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
                temperature=settings.LLM_TEMPERATURE,
                max_tokens=settings.LLM_MAX_TOKENS,
            )
        )
    if token == "stub":
        return _track(StubLLMProvider())
    if token == "openai_compatible":
        return _track(OpenAICompatibleLLMProvider())
    raise _unknown("llm", token)


@lru_cache(maxsize=1)
def get_notification_provider() -> NotificationProvider:
    """The composite transport: three declared channels and a log sink.

    There is no setting to resolve here, because there is nothing yet to choose
    between. SMS, email and push are constructed unconfigured — the composite
    filters them out — so every message lands in the application log with a note
    saying which channel was requested and that nothing left the system. When a
    gateway is procured, this factory gains a branch and no caller changes.

    In-app and websocket notifications are not transports at all: they are a row
    in ``notifications`` and a frame on an open socket, both served by this
    application. Those two are what
    :func:`~app.providers.notification.default_channels` reports as actually
    working.
    """

    return _track(
        CompositeNotificationProvider(
            (
                SmsNotificationProvider(),
                EmailNotificationProvider(),
                PushNotificationProvider(),
            ),
            fallback=LoggingNotificationProvider(),
        )
    )


# ------------------------------------------------------------------- report & lifecycle

_GETTERS: Tuple[Any, ...] = (
    get_weather_provider,
    get_osm_provider,
    get_hazard_provider,
    get_incident_provider,
    get_routing_provider,
    get_llm_provider,
    get_notification_provider,
)


def all_providers() -> Tuple[Provider, ...]:
    """Every configured provider, one per kind, in :data:`PROVIDER_KINDS` order.

    Constructing them is cheap and offline: the HTTP-backed adapters build their
    client lazily on first use, so nothing here opens a socket.
    """

    return tuple(getter() for getter in _GETTERS)


def provider_report() -> List[Dict[str, Any]]:
    """What this deployment is wired to, without touching the network.

    This is the payload behind ``/health/ready`` and the dashboard's provenance
    panel, and it is deliberately synchronous and probe-free: it must be safe to
    call on every readiness check and it must answer even when every external
    source is down. ``healthy`` is therefore absent here — it is
    :func:`provider_health` that asks.

    For an unconnected integration the ``reason`` travels as ``detail``, so the
    panel can say *why* a source is unavailable rather than only that it is.
    """

    out: List[Dict[str, Any]] = []
    for provider in all_providers():
        detail = getattr(provider, "reason", None) if not provider.configured else None
        out.append(provider.describe(detail=detail).as_dict())
    return out


async def provider_health() -> List[Dict[str, Any]]:
    """Ask every provider how it is. Concurrent, and never raises.

    Some of these do cross a network — OSRM and Ollama are probed, the public
    weather and Overpass endpoints deliberately are not — so this belongs on an
    operator-triggered diagnostics endpoint rather than on a liveness probe that
    a load balancer hits every few seconds.

    An adapter's :meth:`healthcheck` is documented as never raising. If one does
    anyway, the exception is reported as an unhealthy status rather than taken as
    an outage of the diagnostics endpoint itself.
    """

    providers = all_providers()
    results = await asyncio.gather(
        *(provider.healthcheck() for provider in providers), return_exceptions=True
    )
    out: List[Dict[str, Any]] = []
    for provider, result in zip(providers, results, strict=False):
        if isinstance(result, ProviderStatus):
            out.append(result.as_dict())
        else:
            out.append(
                provider.describe(
                    healthy=False, detail=f"healthcheck raised: {result!r}"
                ).as_dict()
            )
    return out


def _unconnected_classes() -> List[Type[UnconfiguredProvider]]:
    """Every declared-but-not-connected adapter, found by walking the subclasses.

    Discovered rather than listed so the enumeration cannot fall behind the code:
    adding an integration point makes it appear here without touching this
    function. Two exclusions, both narrow. Private bases (``_UnconfiguredTransport``)
    carry no ``implementation`` of their own. And ``DisabledLLMProvider`` is not a
    future integration — it is the copilot's off switch, and listing it under
    "sources awaiting authorisation" would misdescribe a deliberate default.
    """

    found: List[Type[UnconfiguredProvider]] = []
    stack: List[Type[UnconfiguredProvider]] = list(UnconfiguredProvider.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__name__.startswith("_") or cls is DisabledLLMProvider:
            continue
        if cls not in found:
            found.append(cls)
    return sorted(found, key=lambda c: (c.kind, c.implementation))


def unconnected_integrations() -> List[Dict[str, Any]]:
    """The integration points that exist as interfaces and nothing more.

    IMD, Bhuvan, an agency incident feed, GraphHopper, Valhalla, a hosted
    inference endpoint, SMS, email and push. Each entry names the kind, the
    setting token that would select it, the provenance its records would carry if
    connected, and the specific thing that is missing — a licence, a credential, a
    running service, a procurement step.

    Publishing this list is the honest form of the claim this platform makes about
    government data: the interfaces for authorised integrations are real and
    testable, the connections are not present, and the distinction is visible in
    the product rather than buried in a README.
    """

    return [
        {
            "kind": cls.kind,
            "implementation": cls.implementation,
            "provenance_if_connected": str(cls.provenance),
            "connected": False,
            "reason": cls.reason,
        }
        for cls in _unconnected_classes()
    ]


def reset_providers() -> None:
    """Drop every cached instance. For tests that change settings.

    Synchronous, so it cannot await ``aclose``. A test that exercised an
    HTTP-backed adapter should ``await close_providers()`` instead; this one is for
    the common case of re-resolving after patching an environment variable.
    """

    for getter in _GETTERS:
        getter.cache_clear()
    _LIVE.clear()


async def close_providers() -> None:
    """Release held resources, then clear the caches. Call from app shutdown.

    Walks only what was actually handed out, so shutting down never constructs a
    provider that the process never used.
    """

    for provider in tuple(_LIVE):
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - shutdown must not fail on a dead connection
            pass
    reset_providers()
