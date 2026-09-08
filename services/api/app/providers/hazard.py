"""Standing hazard exposure zones.

The distinction this module exists to preserve: a hazard zone says "this area
floods", an incident says "this area is flooded". The first is a baseline that
changes across years; the second is a fact about today. Conflating them is how a
road that flooded once ends up permanently red on the map, and how a genuinely
dangerous corridor stops standing out.

Authoritative hazard mapping for this region is published by government bodies —
NRSC/Bhuvan flood inundation layers, GSI landslide susceptibility, the IS 1893
seismic zoning. Those are the layers this platform should eventually consume, and
:class:`BhuvanHazardProvider` is where they attach. It is not connected, and it
does not invent a substitute.

What ships instead is :class:`SyntheticHazardProvider`: a dozen hand-drawn boxes
over the places that are, as a matter of public record, exposed — the Brahmaputra
and Barak flood plains, the hill belts that lose roads to slips every monsoon.
Every zone it returns says ``SYNTHETIC`` and names its dataset as hand-drawn, so
no screen can present it as a published map.
"""

from __future__ import annotations

import abc
from datetime import datetime, timezone
from typing import ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.enums import DataProvenance, HazardType
from app.providers.base import Provider, UnconfiguredProvider
from app.providers.models import HazardZoneRecord

__all__ = ["BhuvanHazardProvider", "HazardProvider", "SyntheticHazardProvider"]

#: Hand-drawn zones: (hazard type, name, min_lat, min_lon, max_lat, max_lon,
#: severity 0-1, return period years or None). Boxes rather than real polygons
#: because a box is obviously a sketch — nobody will mistake one for NRSC output,
#: which is the intent.
_ZONES: Tuple[Tuple[HazardType, str, float, float, float, float, float, Optional[int]], ...] = (
    # Brahmaputra valley, west to east, as four reaches.
    (HazardType.FLOOD_PLAIN, "Brahmaputra flood plain — Dhubri to Goalpara", 25.85, 89.85, 26.35, 90.85, 0.88, 5),
    (HazardType.FLOOD_PLAIN, "Brahmaputra flood plain — Guwahati to Tezpur", 26.05, 90.85, 26.85, 92.90, 0.80, 5),
    (HazardType.FLOOD_PLAIN, "Brahmaputra flood plain — Majuli to Dibrugarh", 26.60, 93.60, 27.60, 95.10, 0.90, 3),
    (HazardType.FLOOD_PLAIN, "Barak valley flood plain — Silchar reach", 24.55, 92.40, 25.05, 93.10, 0.75, 5),
    (HazardType.FLOOD_PLAIN, "Imphal valley waterlogging", 24.55, 93.75, 25.05, 94.10, 0.55, 10),
    # Hill belts that shed slopes onto roads every monsoon.
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Meghalaya escarpment — Shillong to Sohra", 25.15, 91.40, 25.65, 92.10, 0.90, None),
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Arunachal foothills — Itanagar belt", 26.95, 92.90, 27.60, 94.30, 0.85, None),
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Arunachal eastern front — Pasighat to Anini", 27.80, 94.80, 28.85, 95.90, 0.88, None),
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Naga hills — Kohima to Mokokchung", 25.55, 93.75, 26.45, 94.65, 0.80, None),
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Mizoram ridges", 22.90, 92.35, 24.35, 93.20, 0.82, None),
    (HazardType.LANDSLIDE_SUSCEPTIBLE, "Manipur hill ranges — Tamenglong to Ukhrul", 24.35, 93.10, 25.45, 94.60, 0.78, None),
    (HazardType.EROSION, "Brahmaputra bank erosion — Majuli", 26.80, 93.80, 27.05, 94.35, 0.85, None),
    (HazardType.HIGH_RAINFALL, "Sohra-Mawsynram very high rainfall belt", 25.15, 91.45, 25.45, 91.95, 0.95, None),
    # Seismic: the classification is public record, the geometry here is not.
    (HazardType.SEISMIC, "North Eastern Region — IS 1893 Zone V (approximate extent)", 22.00, 89.70, 29.50, 97.40, 0.90, None),
)


class HazardProvider(Provider):
    """Exposure zones intersecting an area of interest."""

    kind: ClassVar[str] = "hazard"

    @abc.abstractmethod
    async def zones(
        self,
        *,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        hazard_types: Optional[Iterable[HazardType]] = None,
    ) -> List[HazardZoneRecord]:
        """Zones overlapping ``bbox`` (min_lon, min_lat, max_lon, max_lat).

        ``None`` means everything the source has. Filtering by type is offered
        because the risk engine asks for one factor at a time and a hazard source
        may hold layers we do not use.
        """


class SyntheticHazardProvider(HazardProvider):
    """Hand-drawn boxes over places that are, on the public record, exposed.

    The seismic zone deserves its own note. That the whole North Eastern Region
    falls in the highest Indian seismic zone is published fact; the rectangle
    here is a sketch of its extent, not the published boundary. So the record
    carries ``SYNTHETIC`` provenance with a dataset name that says hand-drawn,
    and the note repeats it. Labelling the classification ``REAL`` because the
    underlying fact is real would be exactly the confusion this project refuses
    to introduce.
    """

    implementation: ClassVar[str] = "synthetic"
    provenance: ClassVar[DataProvenance] = DataProvenance.SYNTHETIC
    source_name: ClassVar[str] = "synthetic-hazard-v1"
    dataset_version: ClassVar[str] = "hand-drawn-2026-09"

    @staticmethod
    def _overlaps(
        zone: Tuple[float, float, float, float], bbox: Tuple[float, float, float, float]
    ) -> bool:
        """Rectangle intersection, in lon/lat order for the bbox argument.

        Deliberately not Shapely: this is four comparisons, and pulling GEOS into
        the provider layer to do them would make the interfaces untestable
        without a compiled geometry library.
        """

        min_lat, min_lon, max_lat, max_lon = zone
        b_min_lon, b_min_lat, b_max_lon, b_max_lat = bbox
        return not (
            max_lon < b_min_lon
            or min_lon > b_max_lon
            or max_lat < b_min_lat
            or min_lat > b_max_lat
        )

    async def zones(
        self,
        *,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        hazard_types: Optional[Iterable[HazardType]] = None,
    ) -> List[HazardZoneRecord]:
        wanted: Optional[Sequence[HazardType]] = None
        if hazard_types is not None:
            wanted = tuple(hazard_types)
        observed_at = datetime.now(timezone.utc)
        out: List[HazardZoneRecord] = []
        for hazard_type, name, min_lat, min_lon, max_lat, max_lon, severity, period in _ZONES:
            if wanted is not None and hazard_type not in wanted:
                continue
            if bbox is not None and not self._overlaps(
                (min_lat, min_lon, max_lat, max_lon), bbox
            ):
                continue
            attributes: Dict[str, object] = {
                "sketch": True,
                "bbox": [min_lon, min_lat, max_lon, max_lat],
            }
            out.append(
                HazardZoneRecord(
                    provenance=self.provenance,
                    source=self.source_name,
                    observed_at=observed_at,
                    hazard_type=hazard_type,
                    severity_index=severity,
                    geometry={
                        "type": "Polygon",
                        "coordinates": [[
                            [min_lon, min_lat], [max_lon, min_lat],
                            [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
                        ]],
                    },
                    name=name,
                    return_period_years=period,
                    dataset_name=self.source_name,
                    dataset_version=self.dataset_version,
                    notes=(
                        "Hand-drawn approximate extent for development and "
                        "demonstration. Not derived from any published hazard map."
                    ),
                    attributes=attributes,
                )
            )
        return out


class BhuvanHazardProvider(UnconfiguredProvider, HazardProvider):
    """NRSC Bhuvan — the integration point for authoritative hazard layers.

    Bhuvan publishes flood inundation and landslide susceptibility products for
    this region. Programmatic access requires registration and acceptance of
    NRSC's terms, which this project does not hold. The class raises rather than
    falling back, because a fallback here would put hand-drawn boxes behind a
    government label.
    """

    implementation: ClassVar[str] = "bhuvan"
    provenance: ClassVar[DataProvenance] = DataProvenance.REAL
    reason: ClassVar[str] = (
        "NRSC Bhuvan services require registration and licence acceptance; no "
        "credentials are configured and no access is claimed"
    )

    async def zones(
        self,
        *,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        hazard_types: Optional[Iterable[HazardType]] = None,
    ) -> List[HazardZoneRecord]:
        raise self._unavailable()
