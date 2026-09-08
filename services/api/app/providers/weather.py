"""Weather: the input that drives most of the risk signal in this region.

Three adapters, and the difference between them is the whole point of this
package.

``SyntheticWeatherProvider`` computes plausible NER weather in-process. It is the
default because it makes the platform demonstrable with no network and no keys,
and every record it returns is labelled ``SYNTHETIC``. It is a coarse
hand-written climatology — monsoon seasonality, an orographic bias toward the
Meghalaya escarpment, a diurnal temperature curve — assembled from published
monthly normals. It is *not* a weather model and reproduces no agency's numbers.
What it does guarantee is determinism: the same coordinate and hour always yield
the same values, so a demo replays identically and a test can assert on them.

``OpenMeteoWeatherProvider`` calls a genuinely public, key-free API. Its records
are labelled ``REAL`` because they come from a connected external source, with
one caveat stated here rather than buried: Open-Meteo's current values are a
numerical-model best estimate, not a rain-gauge reading. Station-level ground
truth for this region means IMD.

``ImdWeatherProvider`` is that integration point, and it is not connected. It
raises rather than returning anything. IMD's data products require registration
and a licence; this platform makes no claim of access to them, and the class
exists so that being granted access is a matter of setting ``IMD_BASE_URL`` and
``IMD_API_KEY``.
"""

from __future__ import annotations

import abc
import hashlib
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from app.core.enums import AlertSeverity, DataProvenance
from app.providers.base import (
    Provider,
    ProviderResponseError,
    ProviderStatus,
    ProviderUnavailable,
    UnconfiguredProvider,
)
from app.providers.models import (
    WeatherAlertRecord,
    WeatherForecastRecord,
    WeatherObservationRecord,
)

__all__ = [
    "ImdWeatherProvider",
    "OpenMeteoWeatherProvider",
    "SyntheticWeatherProvider",
    "WeatherProvider",
]


class WeatherProvider(Provider):
    """Observations, forecasts and warnings for a coordinate.

    Point queries rather than bulk downloads: the consumer is "score this road
    segment now", and a segment is a short line whose midpoint is a fair sample.
    Bulk ingestion in Phase 3 loops over segment midpoints and caches by cell.
    """

    kind: ClassVar[str] = "weather"

    @abc.abstractmethod
    async def observation(
        self, latitude: float, longitude: float, *, at: Optional[datetime] = None
    ) -> WeatherObservationRecord:
        """Conditions at a point, defaulting to now."""

    @abc.abstractmethod
    async def forecast(
        self, latitude: float, longitude: float, *, hours: int = 24
    ) -> List[WeatherForecastRecord]:
        """Hourly buckets from now to ``hours`` ahead, oldest first."""

    async def alerts(
        self, *, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> List[WeatherAlertRecord]:
        """Official warnings covering a bounding box.

        Defaults to none rather than abstract: most sources do not publish
        machine-readable warnings, and an adapter should not be forced to invent
        an implementation to satisfy the interface. An empty list means "this
        source has nothing to say", never "there are no warnings in force".
        """

        return []


#: Rainfall as a fraction of the June-July peak, by calendar month. The NER
#: monsoon arrives earlier and lasts longer than most of India: pre-monsoon
#: thunderstorms from April, the south-west monsoon June-September, a wet
#: October in the south, and a genuinely dry December-February.
_MONTH_WETNESS: Dict[int, float] = {
    1: 0.05, 2: 0.07, 3: 0.16, 4: 0.38, 5: 0.62, 6: 1.00,
    7: 1.00, 8: 0.88, 9: 0.68, 10: 0.34, 11: 0.11, 12: 0.05,
}

#: Terrain highs, as (lat, lon, lat_spread, lon_spread, strength). Anisotropic on
#: purpose: the Meghalaya plateau and the eastern Himalayan front are both ridges
#: running roughly east-west, and a circular bump would put highland conditions in
#: the middle of the Brahmaputra valley. Used as a coarse *altitude* proxy for
#: temperature; rainfall uses the sharper table below.
#:
#: The hill ranges east of the Barak are listed separately rather than as one wide
#: bump, because one wide bump gets both ends wrong: it cooled the Barak valley
#: floor at Silchar, which is at 26 m, while leaving Kohima at 1440 m reading as
#: warm as the plains. The Arunachal front is centred north of the foothill towns
#: for the same reason — Pasighat sits at 155 m at the mouth of the Siang, not on
#: the ridge that shares its latitude.
_OROGRAPHIC_BUMPS: Tuple[Tuple[float, float, float, float, float], ...] = (
    (25.42, 91.60, 0.34, 0.80, 2.60),  # Meghalaya plateau (Shillong, Sohra)
    (28.75, 94.40, 0.42, 2.30, 2.10),  # eastern Himalayan front
    (27.15, 93.50, 0.35, 0.70, 0.60),  # first Arunachal ridge (Itanagar)
    (25.75, 94.30, 0.60, 0.55, 1.35),  # Naga hills (Kohima)
    (24.80, 93.90, 0.60, 0.55, 1.05),  # Manipur hills ringing the Imphal valley
    (23.45, 92.80, 0.90, 0.40, 1.30),  # Mizoram ridges (Aizawl)
)

#: July rainfall added over the plains baseline, in mm, as (lat, lon, lat_spread,
#: lon_spread, mm) bumps. The escarpment lobe is extremely tight in latitude
#: because the real gradient is: Sohra receives roughly six times what Shillong
#: does, 25 km away. A smooth field would paint the whole plateau — and then the
#: Brahmaputra valley — as the wettest place on earth, and every flood-risk
#: number downstream would inherit that.
#:
#: Anchors are eyeballed against published monthly normals for twelve reference
#: towns and land within roughly a quarter of them (see the calibration note in
#: :class:`SyntheticWeatherProvider`). That is a hand-drawn approximation of a
#: climatology, not a reproduction of one, which is why everything derived from
#: it is labelled ``SYNTHETIC``.
#:
#: A negative strength is a rain shadow, and there is a real one here: the Imphal
#: valley floor sits at 790 m ringed by hills that take the moisture out of the
#: monsoon before it arrives, so it receives roughly a third of what the hills
#: 40 km away do. Without that term the generator paints the valley as wet as its
#: ridges, and every flood-risk figure for the Imphal corridor inherits the error.
_RAINFALL_BUMPS: Tuple[Tuple[float, float, float, float, float], ...] = (
    (25.28, 91.72, 0.12, 0.55, 2620.0),
    (27.30, 93.60, 0.45, 0.70, 300.0),
    (28.10, 95.10, 0.40, 0.90, 620.0),
    (24.60, 92.90, 0.70, 0.60, 220.0),
    (23.85, 91.35, 0.45, 0.55, 100.0),
    (24.82, 93.94, 0.28, 0.30, -125.0),
)

#: July rainfall on the valley floor, in mm. Everything else is this plus a bump.
_PLAINS_JULY_MM = 300.0

_CONDITION_BANDS: Tuple[Tuple[float, str], ...] = (
    (0.1, "CLEAR"),
    (0.5, "CLOUDY"),
    (2.5, "LIGHT_RAIN"),
    (7.5, "MODERATE_RAIN"),
    (20.0, "HEAVY_RAIN"),
    (float("inf"), "VERY_HEAVY_RAIN"),
)


def _bump(
    latitude: float, longitude: float, bump: Tuple[float, float, float, float, float]
) -> float:
    """Value of one anisotropic Gaussian at a coordinate."""

    lat0, lon0, lat_spread, lon_spread, strength = bump
    d2 = ((latitude - lat0) ** 2) / (2 * lat_spread**2) + (
        (longitude - lon0) ** 2
    ) / (2 * lon_spread**2)
    return strength * math.exp(-d2)


def _orographic_factor(latitude: float, longitude: float) -> float:
    """Terrain-elevation proxy for a coordinate, 0.4 to 3.0.

    Used only to cool the temperature curve in the hills. It is not an elevation
    model; Phase 2 loads real elevation statistics per segment and those, not
    this, are what the risk engine's terrain factor consumes.
    """

    factor = 0.85 + sum(_bump(latitude, longitude, b) for b in _OROGRAPHIC_BUMPS)
    return min(3.0, max(0.4, factor))


def _july_normal_mm(latitude: float, longitude: float) -> float:
    """Approximate July rainfall total for a coordinate, in mm.

    Floored at 60 mm so a rain-shadow term can never drive the total to zero — no
    part of this region is dry in July, and a zero here would silently switch the
    monsoon off for a district.
    """

    total = _PLAINS_JULY_MM + sum(_bump(latitude, longitude, b) for b in _RAINFALL_BUMPS)
    return max(60.0, total)


def _stable_rng(
    seed: int, salt: str, latitude: float, longitude: float, when: datetime
) -> random.Random:
    """A generator keyed reproducibly on place and hour.

    ``hash()`` is not usable here: Python salts string hashing per process, so a
    demo would produce different weather after a restart and a test would pass
    once. Digesting the key with blake2b keeps it stable across processes,
    machines and Python versions.
    """

    key = f"{seed}|{salt}|{latitude:.2f}|{longitude:.2f}|{when:%Y-%m-%dT%H}"
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _condition_code(rainfall_mm: float, temperature_c: float, hour: int) -> str:
    """A small fixed vocabulary, shared with the frontend legend.

    Free text here would mean the map legend and the generator drift apart, and
    the risk engine would end up string-matching on prose.
    """

    if rainfall_mm < 0.1 and temperature_c < 15 and hour in (4, 5, 6, 7):
        return "FOG"
    for threshold, code in _CONDITION_BANDS:
        if rainfall_mm < threshold:
            return code
    return "VERY_HEAVY_RAIN"


class SyntheticWeatherProvider(WeatherProvider):
    """Deterministic, offline, and labelled as generated.

    The correlations are the reason this is not ``random.uniform``: rain pushes
    humidity up, temperature and visibility down, and pressure down, all in the
    same hour. Downstream, the flood and landslide factors read rainfall, so an
    incoherent generator would produce a platform that looks alive and behaves
    nonsensically — a wet hour with 30 °C and 40 % humidity and clear visibility.

    **What is calibrated and what is not.** Monthly rainfall totals were checked
    against published July normals for twelve reference towns from Sohra to
    Agartala. With the shipped default seed the generated totals land between
    0.91x and 1.27x of the published normal, eleven of the twelve within 20 %, and
    they reproduce two features that matter locally: the escarpment gradient
    (Sohra receives roughly seven times Shillong's July rainfall, 25 km away) and
    the Imphal valley rain shadow. Totals are stochastic, so a different seed
    shifts each town by some tens of millimetres; the band, not any single figure,
    is the claim. That much matters because rainfall is what the flood and
    landslide factors consume. Temperature, humidity, wind, pressure and
    visibility are plausible regional fields tuned by eye, not calibrated against
    station normals, and nothing in the risk engine reads them. Neither set is a
    forecast, and no claim of meteorological accuracy attaches to either.
    """

    implementation: ClassVar[str] = "synthetic"
    provenance: ClassVar[DataProvenance] = DataProvenance.SYNTHETIC
    source_name: ClassVar[str] = "synthetic-climatology-v1"

    def __init__(self, *, seed: int = 0) -> None:
        self._seed = seed

    def _sample(
        self, latitude: float, longitude: float, when: datetime, *, salt: str = "obs"
    ) -> Dict[str, Any]:
        """One coherent hour of weather at one place."""

        rng = _stable_rng(self._seed, salt, latitude, longitude, when)
        wet = _MONTH_WETNESS[when.month]
        oro = _orographic_factor(latitude, longitude)
        july_mm = _july_normal_mm(latitude, longitude)

        # Work backwards from a monthly total rather than forwards from an
        # arbitrary intensity: pick the fraction of hours that rain, then set the
        # mean intensity so hours x fraction x intensity lands on the normal.
        # Getting the total right is what makes the flood factor downstream
        # behave like the region rather than like a random-number generator.
        month_mm = july_mm * wet
        mean_mm_per_hour = month_mm / (30 * 24)
        wet_fraction = 0.12 + 0.38 * wet * min(1.0, july_mm / 2000.0)
        # Convective rainfall here peaks late afternoon and again before dawn.
        # Mean 1.0 over 24 h, so modulating the probability by it preserves the
        # monthly total while making the daily shape realistic.
        diurnal = 1.0 + 0.45 * math.cos((when.hour - 16) / 24 * 2 * math.pi)
        if wet > 0.0 and rng.random() < wet_fraction * diurnal:
            target_intensity = mean_mm_per_hour / wet_fraction
            rainfall = rng.gammavariate(1.6, target_intensity / 1.6)
        else:
            rainfall = 0.0
        rainfall = round(min(rainfall, 120.0), 2)

        # July warmest, January coolest; hills cooler than the valley floor. The
        # ``wet`` term is monsoon cloud cover: July afternoons here are cooler than
        # a clear-sky curve predicts even in hours with no rain falling, which is
        # why it is separate from the per-hour rain penalty below.
        seasonal_t = 25.0 + 6.5 * math.cos((when.month - 7) / 12 * 2 * math.pi)
        temperature = (
            seasonal_t
            - 0.35 * (latitude - 25.5)
            - 4.3 * max(0.0, oro - 0.85)
            - 1.5 * wet
            + 4.5 * math.sin((when.hour - 9) / 24 * 2 * math.pi)
            - (2.5 if rainfall > 1.0 else 0.0)
            + rng.uniform(-1.2, 1.2)
        )
        humidity = min(
            99.0,
            max(35.0, 58 + 30 * wet + (9 if rainfall > 0.5 else 0) + rng.uniform(-5, 5)),
        )
        wind = max(1.0, rng.gammavariate(2.2, 3.0) + (4.0 if rainfall > 8 else 0.0))
        pressure = 1010 - 7 * wet - min(6.0, rainfall * 0.3) + rng.uniform(-2, 2)
        visibility = 10000.0 if rainfall < 0.1 else max(300.0, 9000.0 - rainfall * 420)
        code = _condition_code(rainfall, temperature, when.hour)
        if code == "FOG":
            visibility = min(visibility, rng.uniform(400, 1800))
        return {
            "rainfall_mm": rainfall,
            "temperature_c": round(temperature, 1),
            "humidity_pct": round(humidity, 1),
            "wind_speed_kmph": round(wind, 1),
            "wind_direction_deg": round(rng.uniform(0, 360), 1),
            "pressure_hpa": round(pressure, 1),
            "visibility_m": round(visibility, 0),
            "condition_code": code,
            "wet_hour_chance": round(min(1.0, wet_fraction * diurnal), 3),
            "july_normal_mm": round(july_mm, 1),
        }

    async def observation(
        self, latitude: float, longitude: float, *, at: Optional[datetime] = None
    ) -> WeatherObservationRecord:
        when = (at or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
        s = self._sample(latitude, longitude, when)
        return WeatherObservationRecord(
            provenance=self.provenance,
            source=self.source_name,
            observed_at=when,
            latitude=latitude,
            longitude=longitude,
            station_name=f"synthetic-{latitude:.2f}-{longitude:.2f}",
            temperature_c=s["temperature_c"],
            humidity_pct=s["humidity_pct"],
            rainfall_mm=s["rainfall_mm"],
            wind_speed_kmph=s["wind_speed_kmph"],
            wind_direction_deg=s["wind_direction_deg"],
            pressure_hpa=s["pressure_hpa"],
            visibility_m=s["visibility_m"],
            condition_code=s["condition_code"],
            raw_payload={"generator": self.source_name, "seed": self._seed},
        )

    async def forecast(
        self, latitude: float, longitude: float, *, hours: int = 24
    ) -> List[WeatherForecastRecord]:
        """Hourly buckets that deliberately disagree with the observations.

        The forecast is generated with a different salt and then perturbed by an
        error that grows with lead time. That is not sloppiness — if the
        synthetic forecast reproduced the synthetic observation exactly, Phase 5
        would measure a perfect model and we would be reporting an accuracy that
        is an artefact of the generator.
        """

        issued = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        out: List[WeatherForecastRecord] = []
        for h in range(1, max(1, hours) + 1):
            valid_from = issued + timedelta(hours=h)
            s = self._sample(latitude, longitude, valid_from, salt="forecast")
            rng = _stable_rng(self._seed, "fcst-error", latitude, longitude, valid_from)
            spread = 0.15 + 0.025 * h
            rainfall = max(0.0, s["rainfall_mm"] * (1.0 + rng.uniform(-spread, spread)))
            out.append(
                WeatherForecastRecord(
                    provenance=DataProvenance.PREDICTED,
                    source=self.source_name,
                    observed_at=valid_from,
                    latitude=latitude,
                    longitude=longitude,
                    issued_at=issued,
                    valid_from=valid_from,
                    valid_to=valid_from + timedelta(hours=1),
                    horizon_hours=h,
                    model_name=self.source_name,
                    rainfall_mm=round(rainfall, 2),
                    precipitation_probability=round(
                        min(1.0, s["wet_hour_chance"] * (1.6 if rainfall > 0.1 else 0.6)), 3
                    ),
                    temperature_c=s["temperature_c"],
                    humidity_pct=s["humidity_pct"],
                    wind_speed_kmph=s["wind_speed_kmph"],
                    condition_code=s["condition_code"],
                )
            )
        return out

    async def alerts(
        self, *, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> List[WeatherAlertRecord]:
        """A warning derived from our own forecast, with no issuing authority.

        ``issuing_authority`` is left ``None`` deliberately and permanently. The
        hybrid decision rules only let an alert from a named authority override a
        model prediction, so a generated warning can raise a risk score and light
        up the map but can never close a road. A synthetic alert that claimed to
        be from IMD would be a fabricated government dataset.
        """

        if bbox is None:
            return []
        min_lon, min_lat, max_lon, max_lat = bbox
        lat = (min_lat + max_lat) / 2
        lon = (min_lon + max_lon) / 2
        buckets = await self.forecast(lat, lon, hours=24)
        total = sum(b.rainfall_mm for b in buckets)
        if total < 60.0:
            return []
        severity = (
            AlertSeverity.EMERGENCY if total >= 200
            else AlertSeverity.CRITICAL if total >= 120
            else AlertSeverity.WARNING
        )
        issued = buckets[0].issued_at
        return [
            WeatherAlertRecord(
                provenance=DataProvenance.PREDICTED,
                source=self.source_name,
                observed_at=issued,
                alert_type="HEAVY_RAINFALL",
                severity=severity,
                headline=f"{total:.0f} mm forecast in 24 h over this area (generated)",
                issuing_authority=None,
                external_id=None,
                description=(
                    "Derived from the synthetic climatology, not from any "
                    "meteorological agency. Advisory only; cannot close a road."
                ),
                effective_from=issued,
                effective_to=issued + timedelta(hours=24),
                geometry={
                    "type": "Polygon",
                    "coordinates": [[
                        [min_lon, min_lat], [max_lon, min_lat],
                        [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
                    ]],
                },
                raw_payload={"forecast_total_mm": round(total, 1)},
            )
        ]


_OPENMETEO_HOURLY = (
    "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability,"
    "wind_speed_10m,wind_direction_10m,surface_pressure,visibility,weather_code"
)
_OPENMETEO_CURRENT = (
    "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
    "wind_direction_10m,surface_pressure,weather_code"
)


class OpenMeteoWeatherProvider(WeatherProvider):
    """A real, connected, key-free public source.

    Chosen for the prototype because it needs no registration, which means the
    "is this actually connected?" question has a demonstrable answer. Records are
    labelled ``REAL`` for observations and ``PREDICTED`` for forecasts.

    Honest limitation, repeated from the module docstring because it matters at
    the point of use: Open-Meteo's current values are a numerical-model best
    estimate for the coordinate, not a station reading. For a district officer
    deciding whether a road is under water, model rainfall is a useful signal and
    not the same thing as a gauge.
    """

    implementation: ClassVar[str] = "openmeteo"
    provenance: ClassVar[DataProvenance] = DataProvenance.REAL
    source_name: ClassVar[str] = "open-meteo"

    def __init__(self, *, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client: Optional[Any] = None

    def _http(self) -> Any:
        """Lazily build the shared client.

        Imported here rather than at module scope so that this module — and the
        interface every engine codes against — stays importable in a test
        environment with no HTTP client installed.
        """

        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        try:
            response = await self._http().get(f"{self._base_url}/forecast", params=params)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"{self.name}: timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderResponseError(
                f"{self.name}: HTTP {response.status_code} {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError(f"{self.name}: response was not JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderResponseError(f"{self.name}: expected a JSON object")
        return payload

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        """Open-Meteo returns naive local-to-``timezone`` strings; we ask for UTC."""

        if not isinstance(value, str):
            raise ProviderResponseError("open-meteo: missing timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ProviderResponseError(f"open-meteo: bad timestamp {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def observation(
        self, latitude: float, longitude: float, *, at: Optional[datetime] = None
    ) -> WeatherObservationRecord:
        """Current conditions.

        ``at`` is accepted for interface compatibility and ignored: retrieving a
        past hour needs the archive API, which is a different endpoint with a
        different lag. Silently returning *now* for a requested historical hour
        would be worse than saying so here — Phase 3 backfill uses the archive
        adapter, not this one.
        """

        payload = await self._get(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": _OPENMETEO_CURRENT,
                "timezone": "UTC",
            }
        )
        current = payload.get("current")
        if not isinstance(current, dict):
            raise ProviderResponseError(f"{self.name}: no 'current' block in response")
        return WeatherObservationRecord(
            provenance=self.provenance,
            source=self.source_name,
            observed_at=self._parse_time(current.get("time")),
            latitude=latitude,
            longitude=longitude,
            station_id=None,
            station_name=None,
            temperature_c=current.get("temperature_2m"),
            humidity_pct=current.get("relative_humidity_2m"),
            rainfall_mm=float(current.get("precipitation") or 0.0),
            wind_speed_kmph=current.get("wind_speed_10m"),
            wind_direction_deg=current.get("wind_direction_10m"),
            pressure_hpa=current.get("surface_pressure"),
            visibility_m=None,
            condition_code=(
                None if current.get("weather_code") is None
                else f"WMO_{int(current['weather_code'])}"
            ),
            raw_payload={"current": current, "units": payload.get("current_units")},
        )

    async def forecast(
        self, latitude: float, longitude: float, *, hours: int = 24
    ) -> List[WeatherForecastRecord]:
        wanted = max(1, min(hours, 384))
        payload = await self._get(
            {
                "latitude": latitude,
                "longitude": longitude,
                "hourly": _OPENMETEO_HOURLY,
                "forecast_hours": wanted,
                "timezone": "UTC",
            }
        )
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            raise ProviderResponseError(f"{self.name}: no 'hourly.time' array in response")
        times: List[Any] = hourly["time"]
        issued = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        def col(key: str, index: int) -> Any:
            series = hourly.get(key)
            if isinstance(series, list) and index < len(series):
                return series[index]
            return None

        out: List[WeatherForecastRecord] = []
        for i, raw_time in enumerate(times[:wanted]):
            valid_from = self._parse_time(raw_time)
            probability = col("precipitation_probability", i)
            code = col("weather_code", i)
            out.append(
                WeatherForecastRecord(
                    provenance=DataProvenance.PREDICTED,
                    source=self.source_name,
                    observed_at=valid_from,
                    latitude=latitude,
                    longitude=longitude,
                    issued_at=issued,
                    valid_from=valid_from,
                    valid_to=valid_from + timedelta(hours=1),
                    horizon_hours=max(0, round((valid_from - issued).total_seconds() / 3600)),
                    model_name="open-meteo:best_match",
                    rainfall_mm=float(col("precipitation", i) or 0.0),
                    precipitation_probability=(
                        None if probability is None else float(probability) / 100.0
                    ),
                    temperature_c=col("temperature_2m", i),
                    humidity_pct=col("relative_humidity_2m", i),
                    wind_speed_kmph=col("wind_speed_10m", i),
                    condition_code=None if code is None else f"WMO_{int(code)}",
                )
            )
        return out

    async def healthcheck(self) -> ProviderStatus:
        """Reports configuration, and does not call the API.

        Readiness is polled every few seconds by an orchestrator. Turning that
        into traffic against a free public endpoint is how a project gets its own
        IP rate-limited during a demo. Whether the source actually answers is
        established by the first real request, which reports
        :class:`ProviderUnavailable` honestly.
        """

        return self.describe(healthy=None, detail=f"configured for {self._base_url}; not probed")


class ImdWeatherProvider(UnconfiguredProvider, WeatherProvider):
    """India Meteorological Department — the integration point, not a connection.

    IMD is the authoritative source for this region: station observations,
    district rainfall bulletins, and the colour-coded warnings that a state
    disaster-management authority actually acts on. Access requires registration
    and agreement to their terms, which this project does not hold.

    So this class raises. It does not fall back to the synthetic generator, and
    it does not fall back to Open-Meteo, because both would put non-IMD numbers
    behind an IMD label. Supplying ``IMD_BASE_URL`` and ``IMD_API_KEY`` after
    authorisation is granted turns this into a working adapter; nothing else in
    the platform changes, which is the entire reason the interface exists.
    """

    implementation: ClassVar[str] = "imd"
    provenance: ClassVar[DataProvenance] = DataProvenance.REAL
    reason: ClassVar[str] = (
        "IMD data products require registration and a licence; no credentials are "
        "configured and no access is claimed"
    )

    async def observation(
        self, latitude: float, longitude: float, *, at: Optional[datetime] = None
    ) -> WeatherObservationRecord:
        raise self._unavailable()

    async def forecast(
        self, latitude: float, longitude: float, *, hours: int = 24
    ) -> List[WeatherForecastRecord]:
        raise self._unavailable()

    async def alerts(
        self, *, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> List[WeatherAlertRecord]:
        raise self._unavailable()
