"""Reference and default data applied by ``scripts/seed.py``.

Separate from the seeder, and free of any database dependency, for two reasons: a
test can assert the defaults satisfy their own constraints — weights summing to
one, priorities inside 1-10 — without a Postgres instance, and a reviewer can read
what the platform starts life believing without reading DDL.

Provenance, stated honestly per group:

``NER_STATES`` and ``DISTRICTS``
    Real public administrative facts: state names, ISO 3166-2:IN subdivision
    codes, Census of India 2011 state codes, capitals, district names and
    headquarters. **No boundary geometry.** ``geom`` is left null and Phase 2
    loads polygons from a published administrative boundary dataset. The district
    list is a reference list compiled from public sources and is not asserted to
    be exhaustive or current — several NER states have created districts recently
    — so Phase 2 reconciles it against the boundary dataset rather than treating
    these rows as authoritative.

``COMMODITIES``, ``ROUTING_WEIGHT_PROFILES``, ``RISK_FACTOR_WEIGHTS``
    Configuration, not data. These are the prototype defaults the requirements
    call for, seeded as rows precisely so an authorised administrator can change
    policy without a deployment. Nothing here is a measurement, so nothing here
    carries a provenance label.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence, Tuple

from app.core.enums import RiskFactor, ShipmentPriorityProfile, UserRole

__all__ = [
    "ROLE_PROFILES",
    "COMMODITIES",
    "ROUTING_WEIGHT_PROFILES",
    "RISK_WEIGHT_SET_VERSION",
    "RISK_FACTOR_WEIGHTS",
    "NER_STATES",
    "DISTRICTS",
    "STATE_SOURCE",
    "DISTRICT_SOURCE",
]

#: Cited on every state/district row so the UI can show where a fact came from.
STATE_SOURCE = "Public administrative records (ISO 3166-2:IN, Census of India 2011)"
DISTRICT_SOURCE = "Public administrative records; reference list, boundaries pending"

# --------------------------------------------------------------------- roles

#: Human-readable descriptions for the six roles. The *permissions* are not here:
#: they come from ``app.core.rbac.ROLE_PERMISSIONS``, which stays the single
#: authoritative matrix. What lands in ``roles.permissions_snapshot`` is a copy
#: for display and audit, and the seeder refreshes it on every run.
ROLE_PROFILES: Tuple[Dict[str, Any], ...] = (
    {
        "role": UserRole.ADMIN,
        "display_name": "Administrator",
        "description": (
            "Full platform control, including user management, risk and routing "
            "policy, and model promotion. Every action is audited."
        ),
    },
    {
        "role": UserRole.STATE_OFFICER,
        "display_name": "State Officer",
        "description": (
            "Oversees corridors, shipments and alerts across one state. May "
            "verify incidents and override a recommended route, with a reason."
        ),
    },
    {
        "role": UserRole.DISTRICT_OFFICER,
        "display_name": "District Officer",
        "description": (
            "Operational authority for one district: verifies field reports, "
            "changes road status and dispatches shipments within the district."
        ),
    },
    {
        "role": UserRole.FIELD_OFFICER,
        "display_name": "Field Officer",
        "description": (
            "Submits geo-tagged field reports from the road, offline if needed. "
            "Deliberately cannot verify a report — including their own."
        ),
    },
    {
        "role": UserRole.DRIVER,
        "display_name": "Driver",
        "description": (
            "Sees only their own assigned shipment, its route and its alerts, "
            "and can report an incident encountered en route."
        ),
    },
    {
        "role": UserRole.VIEWER,
        "display_name": "Viewer",
        "description": (
            "Read-only situational awareness across the region, for coordination "
            "cells and observers who must not change operational state."
        ),
    },
)

# ---------------------------------------------------------------- commodities

#: The requirements' prototype priorities, 1 (lowest) to 10 (highest). Seeded as
#: rows because the document is explicit that they "must be configurable by
#: authorized administrators" — so the dispatch order of oxygen against
#: construction material is a decision the state can revisit, not a constant
#: compiled into the routing engine.
COMMODITIES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "Emergency medicine",
        "category": "MEDICAL",
        "default_priority": 10,
        "unit": "cartons",
        "is_perishable": True,
        "requires_cold_chain": True,
        "is_life_critical": True,
        "max_acceptable_delay_hours": 12.0,
        "description": (
            "Cold chain assumed by default because vaccines and insulin are the "
            "common case in the region; an administrator can clear the flag for "
            "consignments that do not need it."
        ),
    },
    {
        "name": "Oxygen",
        "category": "MEDICAL",
        "default_priority": 10,
        "unit": "cylinders",
        "is_hazardous": True,
        "is_life_critical": True,
        "max_acceptable_delay_hours": 6.0,
        "description": (
            "Hazardous cargo under pressure, so vehicle compatibility and "
            "corridor reliability matter more than arrival time."
        ),
    },
    {
        "name": "Water",
        "category": "WATER",
        "default_priority": 10,
        "unit": "kilolitres",
        "is_life_critical": True,
        "max_acceptable_delay_hours": 24.0,
        "description": "Potable water for a cut-off settlement.",
    },
    {
        "name": "Food",
        "category": "FOOD",
        "default_priority": 8,
        "unit": "tonnes",
        "is_perishable": True,
        "max_acceptable_delay_hours": 48.0,
        "description": "Dry rations and relief supplies.",
    },
    {
        "name": "Agricultural produce",
        "category": "AGRICULTURE",
        "default_priority": 6,
        "unit": "tonnes",
        "is_perishable": True,
        "max_acceptable_delay_hours": 72.0,
        "description": (
            "Outbound farm produce. Perishable, so a long detour can cost the "
            "consignment its value even when the corridor is safe."
        ),
    },
    {
        "name": "Construction material",
        "category": "CONSTRUCTION",
        "default_priority": 5,
        "unit": "tonnes",
        "description": (
            "Heavy, tolerant of delay, and usually the reason a HEAVY_TRUCK is "
            "the assigned vehicle — which narrows the admissible corridors."
        ),
    },
)

# ------------------------------------------------------------- routing weights

#: Objective weights per priority profile. Each set must sum to 1.0 — the database
#: enforces it with a CHECK constraint, because a profile that does not sum to one
#: makes every comparison between candidate routes meaningless.
#:
#: NORMAL and EMERGENCY are the two weightings the requirements specify. The
#: relative ordering is the point: for an emergency consignment, reliability and
#: risk together take 0.80 of the objective and distance takes none, which is what
#: makes the engine prefer a longer road that will still be open on arrival.
ROUTING_WEIGHT_PROFILES: Tuple[Dict[str, Any], ...] = (
    {
        "profile": ShipmentPriorityProfile.NORMAL,
        "version": 1,
        "label": "Normal shipment (default)",
        "weight_reliability": 0.40,
        "weight_eta": 0.25,
        "weight_risk": 0.15,
        "weight_distance": 0.10,
        "weight_cost": 0.10,
        "max_acceptable_risk_score": 75.0,
        "allows_high_risk_segments": False,
        "description": (
            "Reliability leads, then arrival time. Candidates scoring above 75 "
            "are excluded outright rather than merely penalised."
        ),
    },
    {
        "profile": ShipmentPriorityProfile.EMERGENCY,
        "version": 1,
        "label": "Emergency shipment",
        "weight_reliability": 0.50,
        "weight_eta": 0.15,
        "weight_risk": 0.30,
        "weight_distance": 0.00,
        "weight_cost": 0.05,
        "max_acceptable_risk_score": 90.0,
        "allows_high_risk_segments": True,
        "description": (
            "Distance carries no weight at all. A high-risk corridor is allowed "
            "because refusing to move oxygen is also a harm — but the higher "
            "ceiling is a deliberate, auditable policy choice, not a default."
        ),
    },
    {
        "profile": ShipmentPriorityProfile.BULK_ECONOMY,
        "version": 1,
        "label": "Bulk economy freight",
        "weight_reliability": 0.20,
        "weight_eta": 0.15,
        "weight_risk": 0.05,
        "weight_distance": 0.25,
        "weight_cost": 0.35,
        "max_acceptable_risk_score": 60.0,
        "allows_high_risk_segments": False,
        "description": (
            "Not specified by the requirements: a starting point for non-urgent "
            "freight where fuel cost dominates, expected to be tuned by the "
            "operator. Seeded so the third enum member is not dead weight."
        ),
    },
)

# --------------------------------------------------------------- risk weighting

#: Names the coherent set of ceilings. Every risk score records the version that
#: produced it, so a score from last month can still be explained under the
#: weighting that was in force when it was computed.
RISK_WEIGHT_SET_VERSION = "baseline-v1"

#: Maximum points each factor may contribute to a 0-100 risk score.
#:
#: The ceilings sum to 164, deliberately above 100, and the engine clamps the
#: total. The requirements' own worked example reaches 91 from seven simultaneous
#: factors (weather 18, flood 12, landslide 26, road condition 8, historical 7,
#: current incident 15, traffic 5), which is only reachable if each factor's
#: ceiling is set independently of the others.
#:
#: ``saturation_value`` is the measurement at which a factor reaches its ceiling
#: and ``threshold_value`` the measurement below which it contributes nothing —
#: light drizzle must not nudge a national highway toward CAUTION. Both are
#: prototype judgements, held as configuration precisely because the state's own
#: engineers are better placed to set them than we are.
RISK_FACTOR_WEIGHTS: Tuple[Dict[str, Any], ...] = (
    {
        "factor": RiskFactor.WEATHER,
        "max_contribution": 18.0,
        "saturation_value": 100.0,
        "threshold_value": 20.0,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.15,
        "description": "Composite severity of current and warned weather.",
    },
    {
        "factor": RiskFactor.RAINFALL,
        "max_contribution": 20.0,
        "saturation_value": 150.0,
        "threshold_value": 10.0,
        "response_curve": "SQRT",
        "missing_data_confidence_penalty": 0.20,
        "description": (
            "Rolling 24-hour accumulation in mm. SQRT because the first 40 mm "
            "changes a hill road's behaviour far more than the next 40 does."
        ),
    },
    {
        "factor": RiskFactor.FLOOD,
        "max_contribution": 20.0,
        "saturation_value": 1.0,
        "threshold_value": 0.15,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.10,
        "description": "Flood exposure 0-1 from hazard zone overlap and rainfall.",
    },
    {
        "factor": RiskFactor.LANDSLIDE,
        "max_contribution": 26.0,
        "saturation_value": 1.0,
        "threshold_value": 0.10,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.10,
        "description": (
            "Highest ceiling of any factor. In the NER a landslide does not slow "
            "a corridor, it removes it, and often for days."
        ),
    },
    {
        "factor": RiskFactor.TERRAIN,
        "max_contribution": 10.0,
        "saturation_value": 25.0,
        "threshold_value": 5.0,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.0,
        "description": (
            "Maximum slope along the segment, in per cent. No confidence penalty: "
            "terrain is static, so it is never stale."
        ),
    },
    {
        "factor": RiskFactor.ROAD_CONDITION,
        "max_contribution": 12.0,
        "saturation_value": 1.0,
        "threshold_value": 0.20,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.15,
        "description": (
            "Surface and structural condition 0-1, worst for an unpaved rural "
            "road with a weak bridge on it."
        ),
    },
    {
        "factor": RiskFactor.HISTORICAL_INCIDENT,
        "max_contribution": 10.0,
        "saturation_value": 12.0,
        "threshold_value": 1.0,
        "response_curve": "SQRT",
        "missing_data_confidence_penalty": 0.05,
        "description": (
            "Incidents recorded on this segment in comparable past seasons. A "
            "corridor that fails every monsoon is not a surprise."
        ),
    },
    {
        "factor": RiskFactor.CURRENT_INCIDENT,
        "max_contribution": 25.0,
        "saturation_value": 3.0,
        "threshold_value": 1.0,
        "response_curve": "STEP",
        "missing_data_confidence_penalty": 0.0,
        "description": (
            "Open, verified incidents on the segment now. STEP because one "
            "verified landslide is already decisive; a second adds little. Note "
            "this factor only scores an incident — an official verified closure "
            "is not a risk contribution at all, it forces BLOCKED outright."
        ),
    },
    {
        "factor": RiskFactor.TRAFFIC,
        "max_contribution": 8.0,
        "saturation_value": 1.0,
        "threshold_value": 0.30,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.25,
        "description": (
            "Congestion 0-1. Low ceiling and a high staleness penalty: traffic "
            "affects ETA far more than safety, and our traffic input is weak."
        ),
    },
    {
        "factor": RiskFactor.VEHICLE_COMPATIBILITY,
        "max_contribution": 15.0,
        "saturation_value": 1.0,
        "threshold_value": 0.0,
        "response_curve": "LINEAR",
        "missing_data_confidence_penalty": 0.0,
        "description": (
            "Margin between the vehicle's dimensions and the segment's limits. "
            "Scores the tight fit — a 16 t truck over an 18 t bridge. An outright "
            "breach is not scored here; the candidate is rejected as inadmissible."
        ),
    },
)

# ------------------------------------------------------------------- geography

#: The eight states of the North Eastern Region. ``census_code`` is the Census of
#: India 2011 state code, kept because most published NER datasets key on it and
#: joining on a name is how you lose Kamrup to a spelling variant.
NER_STATES: Tuple[Dict[str, Any], ...] = (
    {"name": "Arunachal Pradesh", "iso_code": "IN-AR", "census_code": "12",
     "capital": "Itanagar"},
    {"name": "Assam", "iso_code": "IN-AS", "census_code": "18",
     "capital": "Dispur"},
    {"name": "Manipur", "iso_code": "IN-MN", "census_code": "14",
     "capital": "Imphal"},
    {"name": "Meghalaya", "iso_code": "IN-ML", "census_code": "17",
     "capital": "Shillong"},
    {"name": "Mizoram", "iso_code": "IN-MZ", "census_code": "15",
     "capital": "Aizawl"},
    {"name": "Nagaland", "iso_code": "IN-NL", "census_code": "13",
     "capital": "Kohima"},
    {"name": "Sikkim", "iso_code": "IN-SK", "census_code": "11",
     "capital": "Gangtok"},
    {"name": "Tripura", "iso_code": "IN-TR", "census_code": "16",
     "capital": "Agartala"},
)

#: Districts keyed by state ISO code, as ``(name, headquarters)``.
#:
#: A reference list, not an authoritative one. Several NER states have created
#: districts in the last few years and the set changes by notification, so Phase 2
#: reconciles these rows against a published boundary dataset and corrects them
#: there. ``is_remote`` is left false for every district on purpose: which
#: districts depend on seasonally closing roads is an operational classification
#: for a state officer to make, not something a seed script should assert.
DISTRICTS: Dict[str, Sequence[Tuple[str, str]]] = {
    "IN-AR": (
        ("Tawang", "Tawang"),
        ("West Kameng", "Bomdila"),
        ("East Kameng", "Seppa"),
        ("Papum Pare", "Yupia"),
        ("Lower Subansiri", "Ziro"),
        ("Upper Subansiri", "Daporijo"),
        ("West Siang", "Aalo"),
        ("East Siang", "Pasighat"),
        ("Upper Siang", "Yingkiong"),
        ("Lower Dibang Valley", "Roing"),
        ("Dibang Valley", "Anini"),
        ("Lohit", "Tezu"),
        ("Anjaw", "Hawai"),
        ("Namsai", "Namsai"),
        ("Changlang", "Changlang"),
        ("Tirap", "Khonsa"),
        ("Longding", "Longding"),
    ),
    "IN-AS": (
        ("Kamrup Metropolitan", "Guwahati"),
        ("Kamrup", "Amingaon"),
        ("Barpeta", "Barpeta"),
        ("Bongaigaon", "Bongaigaon"),
        ("Cachar", "Silchar"),
        ("Darrang", "Mangaldoi"),
        ("Dhubri", "Dhubri"),
        ("Dibrugarh", "Dibrugarh"),
        ("Dima Hasao", "Haflong"),
        ("Goalpara", "Goalpara"),
        ("Golaghat", "Golaghat"),
        ("Hailakandi", "Hailakandi"),
        ("Jorhat", "Jorhat"),
        ("Karbi Anglong", "Diphu"),
        ("Karimganj", "Karimganj"),
        ("Lakhimpur", "North Lakhimpur"),
        ("Nagaon", "Nagaon"),
        ("Sivasagar", "Sivasagar"),
        ("Sonitpur", "Tezpur"),
        ("Tinsukia", "Tinsukia"),
    ),
    "IN-MN": (
        ("Imphal West", "Lamphelpat"),
        ("Imphal East", "Porompat"),
        ("Bishnupur", "Bishnupur"),
        ("Thoubal", "Thoubal"),
        ("Kakching", "Kakching"),
        ("Churachandpur", "Churachandpur"),
        ("Senapati", "Senapati"),
        ("Kangpokpi", "Kangpokpi"),
        ("Ukhrul", "Ukhrul"),
        ("Kamjong", "Kamjong"),
        ("Chandel", "Chandel"),
        ("Tengnoupal", "Tengnoupal"),
        ("Tamenglong", "Tamenglong"),
        ("Noney", "Noney"),
        ("Pherzawl", "Pherzawl"),
        ("Jiribam", "Jiribam"),
    ),
    "IN-ML": (
        ("East Khasi Hills", "Shillong"),
        ("West Khasi Hills", "Nongstoin"),
        ("South West Khasi Hills", "Mawkyrwat"),
        ("Ri Bhoi", "Nongpoh"),
        ("West Jaintia Hills", "Jowai"),
        ("East Jaintia Hills", "Khliehriat"),
        ("East Garo Hills", "Williamnagar"),
        ("West Garo Hills", "Tura"),
        ("North Garo Hills", "Resubelpara"),
        ("South Garo Hills", "Baghmara"),
        ("South West Garo Hills", "Ampati"),
    ),
    "IN-MZ": (
        ("Aizawl", "Aizawl"),
        ("Lunglei", "Lunglei"),
        ("Champhai", "Champhai"),
        ("Kolasib", "Kolasib"),
        ("Serchhip", "Serchhip"),
        ("Mamit", "Mamit"),
        ("Lawngtlai", "Lawngtlai"),
        ("Saiha", "Saiha"),
        ("Hnahthial", "Hnahthial"),
        ("Saitual", "Saitual"),
        ("Khawzawl", "Khawzawl"),
    ),
    "IN-NL": (
        ("Kohima", "Kohima"),
        ("Dimapur", "Dimapur"),
        ("Mokokchung", "Mokokchung"),
        ("Mon", "Mon"),
        ("Tuensang", "Tuensang"),
        ("Wokha", "Wokha"),
        ("Zunheboto", "Zunheboto"),
        ("Phek", "Phek"),
        ("Kiphire", "Kiphire"),
        ("Longleng", "Longleng"),
        ("Peren", "Peren"),
        ("Noklak", "Noklak"),
    ),
    "IN-SK": (
        ("Gangtok", "Gangtok"),
        ("Pakyong", "Pakyong"),
        ("Namchi", "Namchi"),
        ("Soreng", "Soreng"),
        ("Gyalshing", "Gyalshing"),
        ("Mangan", "Mangan"),
    ),
    "IN-TR": (
        ("West Tripura", "Agartala"),
        ("Sepahijala", "Bishramganj"),
        ("Khowai", "Khowai"),
        ("Gomati", "Udaipur"),
        ("South Tripura", "Belonia"),
        ("Dhalai", "Ambassa"),
        ("Unakoti", "Kailashahar"),
        ("North Tripura", "Dharmanagar"),
    ),
}
