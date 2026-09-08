"""Routing engines.

A routing provider answers one narrow question: how do you physically get from A
to B over the road network, and how far and how long is it at free flow. It does
not decide which route to take. Risk, reliability, vehicle compatibility and
commodity priority are applied on top by the Phase 6 scorer, which is what lets
the platform compare candidates identically whether they came from OSRM, from
GraphHopper, or from the straight-line fallback.

That split is also why vehicle profiles are thin here. OSRM is compiled with one
profile and knows nothing about our weight and height restrictions; asking it to
enforce them would scatter policy across an engine we do not control. Instead the
engine proposes geometry, and our own segment attributes reject a route a lorry
cannot use — the rule the specification states plainly: a route must be rejected
if a vehicle is incompatible with a road.

:class:`StraightLineRoutingProvider` exists so the platform is demonstrable
without a compiled OSRM graph, and every result it returns is flagged
``degraded`` with ``SIMULATED`` provenance. It is a stand-in for a road network,
not a routing engine, and nothing built on it may be presented as a real ETA.
"""

from __future__ import annotations

import abc
import math
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any, ClassVar, Dict, List, Sequence

from app.core.enums import DataProvenance
from app.providers.base import (
    Provider,
    ProviderResponseError,
    ProviderStatus,
    ProviderUnavailable,
    UnconfiguredProvider,
)
from app.providers.models import RouteLeg, RoutingResult, Waypoint

__all__ = [
    "GraphHopperRoutingProvider",
    "OsrmRoutingProvider",
    "RoutingProvider",
    "StraightLineRoutingProvider",
    "ValhallaRoutingProvider",
    "haversine_km",
]

_EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: Waypoint, b: Waypoint) -> float:
    """Great-circle distance in kilometres.

    Good to about 0.5 % at these latitudes, which is far below the error of the
    detour assumption it feeds, so a geodesic library would be false precision.
    """

    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


class RoutingProvider(Provider):
    """Geometric paths between waypoints, with unweighted cost."""

    kind: ClassVar[str] = "routing"

    @abc.abstractmethod
    async def routes(
        self,
        waypoints: Sequence[Waypoint],
        *,
        profile: str = "driving",
        alternatives: int = 0,
    ) -> List[RoutingResult]:
        """One or more distinct paths through ``waypoints``, best first.

        ``alternatives`` is a request, not a guarantee: on a network with one
        viable corridor — common in this region, which is the whole problem the
        platform addresses — an engine will return a single route no matter what
        is asked. Callers must handle getting fewer than they wanted rather than
        padding the list, because a fabricated second option in a route
        comparison table is a recommendation made out of nothing.
        """

    async def route(
        self, waypoints: Sequence[Waypoint], *, profile: str = "driving"
    ) -> RoutingResult:
        """The single best path. Convenience over :meth:`routes`."""

        results = await self.routes(waypoints, profile=profile, alternatives=0)
        if not results:
            raise ProviderResponseError(f"{self.name}: no route returned")
        return results[0]

    @staticmethod
    def _validate(waypoints: Sequence[Waypoint]) -> Sequence[Waypoint]:
        if len(waypoints) < 2:
            raise ValueError("routing needs at least an origin and a destination")
        for point in waypoints:
            if not (-90.0 <= point.latitude <= 90.0 and -180.0 <= point.longitude <= 180.0):
                raise ValueError(f"waypoint out of range: {point}")
        return waypoints


class OsrmRoutingProvider(RoutingProvider):
    """OSRM over HTTP. The intended engine for this deployment.

    Provenance is ``DERIVED``, not ``REAL``: the road network underneath is real
    OSM data, but a path and a duration are computed products. The distinction
    matters on screen — an operator should see that an ETA was calculated, while
    the rainfall beside it was observed.

    OSRM does not report OSM way ids for a route (its ``annotations=nodes`` gives
    node ids), so ``RouteLeg.osm_way_ids`` stays empty here. Phase 6 matches the
    returned geometry against our ``road_segments`` in PostGIS instead, which it
    would have to do anyway to attach risk scores to a path.
    """

    implementation: ClassVar[str] = "osrm"
    provenance: ClassVar[DataProvenance] = DataProvenance.DERIVED
    source_name: ClassVar[str] = "osrm"

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client = self._http()
        try:
            response = await client.get(f"{self._base_url}{path}", params=params)
        except Exception as exc:  # httpx is imported lazily; catch by behaviour
            raise ProviderUnavailable(f"{self.name}: {exc}") from exc
        if response.status_code >= 500:
            raise ProviderUnavailable(f"{self.name}: HTTP {response.status_code} from OSRM")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError(f"{self.name}: non-JSON response") from exc
        code = payload.get("code")
        if code != "Ok":
            # NoRoute and NoSegment are answers, not failures: the graph simply
            # has no path, or the coordinate is off-network. Both are routing
            # facts the caller must see rather than a transport error.
            raise ProviderResponseError(
                f"{self.name}: OSRM returned {code}: {payload.get('message', 'no detail')}"
            )
        return payload

    async def routes(
        self,
        waypoints: Sequence[Waypoint],
        *,
        profile: str = "driving",
        alternatives: int = 0,
    ) -> List[RoutingResult]:
        self._validate(waypoints)
        coordinates = ";".join(
            f"{point.longitude:.6f},{point.latitude:.6f}" for point in waypoints
        )
        params: Dict[str, Any] = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
        }
        if alternatives > 0:
            params["alternatives"] = alternatives
        payload = await self._get(f"/route/v1/{profile}/{coordinates}", params)

        observed_at = datetime.now(timezone.utc)
        out: List[RoutingResult] = []
        for route in payload.get("routes", []):
            legs = [
                RouteLeg(
                    distance_km=round(float(leg.get("distance", 0.0)) / 1000.0, 3),
                    duration_minutes=round(float(leg.get("duration", 0.0)) / 60.0, 2),
                )
                for leg in route.get("legs", [])
                if isinstance(leg, dict)
            ]
            geometry = route.get("geometry")
            if not isinstance(geometry, dict):
                continue
            out.append(
                RoutingResult(
                    provenance=self.provenance,
                    source=self.source_name,
                    observed_at=observed_at,
                    distance_km=round(float(route.get("distance", 0.0)) / 1000.0, 3),
                    duration_minutes=round(float(route.get("duration", 0.0)) / 60.0, 2),
                    geometry=geometry,
                    legs=legs,
                    degraded=False,
                    notes=None,
                )
            )
        if not out:
            raise ProviderResponseError(f"{self.name}: OSRM returned no usable route")
        return out

    async def healthcheck(self) -> ProviderStatus:
        """Probes the engine with a two-point query inside the region.

        Unlike the public weather and Overpass endpoints, OSRM here is our own
        container, so probing it costs nothing anyone else pays for — and it is
        worth probing, because "OSRM is up but no graph is loaded" is a state that
        otherwise only surfaces when the first user asks for a route.
        """

        try:
            await self._get(
                "/route/v1/driving/91.740,26.140;91.800,26.180", {"overview": "false"}
            )
        except ProviderResponseError as exc:
            # Reachable, but it could not route: usually the wrong extract.
            return self.describe(healthy=False, detail=str(exc))
        except ProviderUnavailable as exc:
            return self.describe(healthy=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - a health probe must never raise
            return self.describe(healthy=False, detail=f"unexpected error: {exc}")
        return self.describe(healthy=True, detail=f"routed a test pair via {self._base_url}")


class StraightLineRoutingProvider(RoutingProvider):
    """No road network: straight lines with a detour allowance. Always degraded.

    Configured as ``ROUTING_PROVIDER=stub``. It exists for one reason — building
    an OSRM graph needs a multi-gigabyte extract and a preprocessing step, and the
    rest of the platform should be runnable and testable before that lands. Every
    result says ``degraded=True``, carries ``SIMULATED`` provenance and a note
    explaining itself, so the UI can refuse to present it as an ETA.

    Both numbers are assumptions, stated rather than hidden. Road distance in
    mountainous terrain typically runs 30-50 % longer than the straight line;
    ``detour_factor`` takes the middle of that. ``average_speed_kmph`` of 32 is a
    plausible hill-road average, not a measurement. Neither is tuned to make
    anything look good, and both are constructor arguments so a test can pin them.
    """

    implementation: ClassVar[str] = "stub"
    provenance: ClassVar[DataProvenance] = DataProvenance.SIMULATED
    source_name: ClassVar[str] = "straight-line-fallback"

    def __init__(self, *, detour_factor: float = 1.4, average_speed_kmph: float = 32.0) -> None:
        if detour_factor < 1.0:
            raise ValueError("detour_factor below 1.0 would be shorter than a straight line")
        if average_speed_kmph <= 0:
            raise ValueError("average_speed_kmph must be positive")
        self.detour_factor = detour_factor
        self.average_speed_kmph = average_speed_kmph

    async def routes(
        self,
        waypoints: Sequence[Waypoint],
        *,
        profile: str = "driving",
        alternatives: int = 0,
    ) -> List[RoutingResult]:
        points = self._validate(waypoints)
        legs: List[RouteLeg] = []
        total_km = 0.0
        for start, end in pairwise(points):
            leg_km = haversine_km(start, end) * self.detour_factor
            total_km += leg_km
            legs.append(
                RouteLeg(
                    distance_km=round(leg_km, 3),
                    duration_minutes=round(leg_km / self.average_speed_kmph * 60.0, 2),
                )
            )
        # A single result, never alternatives. Two different straight lines
        # between the same two points would be the same line; offering a second
        # "option" would be inventing a choice that does not exist.
        return [
            RoutingResult(
                provenance=self.provenance,
                source=self.source_name,
                observed_at=datetime.now(timezone.utc),
                distance_km=round(total_km, 3),
                duration_minutes=round(total_km / self.average_speed_kmph * 60.0, 2),
                geometry={
                    "type": "LineString",
                    "coordinates": [list(p.as_lon_lat()) for p in points],
                },
                legs=legs,
                degraded=True,
                notes=(
                    "Straight-line estimate: no road network was consulted. "
                    f"Distance is the great-circle path x{self.detour_factor} and "
                    f"duration assumes {self.average_speed_kmph:.0f} km/h throughout. "
                    "Not a routable path and not a usable ETA."
                ),
            )
        ]

    async def healthcheck(self) -> ProviderStatus:
        return self.describe(
            healthy=True,
            detail="straight-line fallback; all results are flagged degraded",
        )


class GraphHopperRoutingProvider(UnconfiguredProvider, RoutingProvider):
    """GraphHopper — declared so the engine can be swapped, not connected.

    Worth keeping visible because it answers a real gap: GraphHopper supports
    per-vehicle weight, height and width constraints natively, which OSRM does
    not, and this region's low bridges and narrow hill roads make that valuable.
    Adopting it means running the service and mapping its response shape; the
    interface above is already engine-neutral, so no calling code changes.
    """

    implementation: ClassVar[str] = "graphhopper"
    provenance: ClassVar[DataProvenance] = DataProvenance.DERIVED
    reason: ClassVar[str] = "no GraphHopper endpoint is configured"

    async def routes(
        self,
        waypoints: Sequence[Waypoint],
        *,
        profile: str = "driving",
        alternatives: int = 0,
    ) -> List[RoutingResult]:
        raise self._unavailable()


class ValhallaRoutingProvider(UnconfiguredProvider, RoutingProvider):
    """Valhalla — declared so the engine can be swapped, not connected.

    Valhalla's dynamic per-request costing is the closest fit to what this
    platform does with edge weights, so it is the most likely eventual engine.
    Not connected: no endpoint, and no tile set built for this region.
    """

    implementation: ClassVar[str] = "valhalla"
    provenance: ClassVar[DataProvenance] = DataProvenance.DERIVED
    reason: ClassVar[str] = "no Valhalla endpoint is configured"

    async def routes(
        self,
        waypoints: Sequence[Waypoint],
        *,
        profile: str = "driving",
        alternatives: int = 0,
    ) -> List[RoutingResult]:
        raise self._unavailable()
