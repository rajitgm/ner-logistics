"""Role-based access control matrix.

Dependency-free by design: the matrix and its query helpers are plain Python so
they can be unit-tested without a database, a web framework or a running server.
The FastAPI wiring that consumes this module lives in ``app.api.deps``.

Two orthogonal concepts are enforced together:

``Permission``  *what* an action is (create a shipment, verify an incident)
``ScopeLevel``  *where* it may be applied (nationally, one state, one district,
                or only the caller's own records)

A request must satisfy both. Holding ``SHIPMENT_UPDATE`` does not let a district
officer edit another district's consignment; the scope check in
``app.api.deps.enforce_scope`` narrows every query by the caller's assignment.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable, Mapping

from app.core.enums import Permission as P, UserRole as R

__all__ = [
    "MUTATING_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "ROLE_SCOPE",
    "ScopeLevel",
    "describe_role",
    "has_permission",
    "permissions_for",
    "scope_for",
    "widest_scope",
]


class ScopeLevel(int, Enum):
    """Data visibility breadth, ordered from narrowest to widest."""

    SELF = 0
    DISTRICT = 1
    STATE = 2
    GLOBAL = 3


#: Permissions that change operational state. Used by tests to assert that
#: read-only roles cannot mutate anything, and by the audit layer to decide when
#: a before/after snapshot must be recorded.
MUTATING_PERMISSIONS: FrozenSet[P] = frozenset(
    {
        P.ROAD_STATUS_OVERRIDE,
        P.ROUTE_PLAN,
        P.ROUTE_OVERRIDE,
        P.INCIDENT_CREATE,
        P.INCIDENT_VERIFY,
        P.INCIDENT_DELETE,
        P.FIELD_REPORT_CREATE,
        P.VEHICLE_MANAGE,
        P.VEHICLE_POSITION_WRITE,
        P.SHIPMENT_CREATE,
        P.SHIPMENT_UPDATE,
        P.SHIPMENT_CANCEL,
        P.ALERT_CREATE,
        P.ALERT_ACKNOWLEDGE,
        P.SIMULATION_RUN,
        P.USER_MANAGE,
        P.ROLE_MANAGE,
        P.CONFIG_MANAGE,
        P.MODEL_MANAGE,
        P.SYNC_WRITE,
    }
)

#: Every read-only capability, i.e. the VIEWER baseline.
_READ_ONLY: FrozenSet[P] = frozenset(
    {
        P.ROAD_READ,
        P.RISK_READ,
        P.WEATHER_READ,
        P.ROUTE_READ,
        P.INCIDENT_READ,
        P.FIELD_REPORT_READ,
        P.VEHICLE_READ,
        P.SHIPMENT_READ,
        P.ALERT_READ,
        P.ANALYTICS_READ,
    }
)

#: A driver sees the corridor they are on and reports what they encounter, but
#: cannot verify their own reports or alter shipment records.
_DRIVER: FrozenSet[P] = frozenset(
    {
        P.ROAD_READ,
        P.RISK_READ,
        P.WEATHER_READ,
        P.ROUTE_READ,
        P.ALERT_READ,
        P.ALERT_ACKNOWLEDGE,
        P.SHIPMENT_READ,
        P.VEHICLE_READ,
        P.VEHICLE_POSITION_WRITE,
        P.INCIDENT_READ,
        P.INCIDENT_CREATE,
        P.FIELD_REPORT_CREATE,
        P.FIELD_REPORT_READ,
        P.SYNC_WRITE,
        P.AI_QUERY,
    }
)

#: Field officers collect ground truth. Deliberately *not* granted
#: INCIDENT_VERIFY: the reporter and the verifier must be different people, so a
#: single compromised field account cannot fabricate an authoritative closure.
_FIELD_OFFICER: FrozenSet[P] = frozenset(
    {
        P.ROAD_READ,
        P.RISK_READ,
        P.WEATHER_READ,
        P.ROUTE_READ,
        P.INCIDENT_READ,
        P.INCIDENT_CREATE,
        P.FIELD_REPORT_READ,
        P.FIELD_REPORT_CREATE,
        P.VEHICLE_READ,
        P.SHIPMENT_READ,
        P.ALERT_READ,
        P.ALERT_ACKNOWLEDGE,
        P.SYNC_WRITE,
        P.AI_QUERY,
    }
)

#: District officers run day-to-day operations inside one district: they verify
#: field intelligence, set road status and plan shipments, but cannot override a
#: committed route or cancel a consignment (reserved for state level and above).
_DISTRICT_OFFICER: FrozenSet[P] = _READ_ONLY | frozenset(
    {
        P.ROAD_STATUS_OVERRIDE,
        P.INCIDENT_CREATE,
        P.INCIDENT_VERIFY,
        P.FIELD_REPORT_CREATE,
        P.ROUTE_PLAN,
        P.SHIPMENT_CREATE,
        P.SHIPMENT_UPDATE,
        P.ALERT_CREATE,
        P.ALERT_ACKNOWLEDGE,
        P.SIMULATION_RUN,
        P.AI_QUERY,
        P.SYNC_WRITE,
    }
)

#: State officers add cross-district authority: route overrides, shipment
#: cancellation, fleet management and read access to the audit trail. They still
#: cannot manage users, roles, configuration or model promotion.
_STATE_OFFICER: FrozenSet[P] = _DISTRICT_OFFICER | frozenset(
    {
        P.ROUTE_OVERRIDE,
        P.SHIPMENT_CANCEL,
        P.VEHICLE_MANAGE,
        P.USER_READ,
        P.CONFIG_READ,
        P.AUDIT_READ,
        P.MODEL_READ,
    }
)

#: Admins hold every capability. Built from the enum itself so a newly added
#: permission is never silently unassigned.
_ADMIN: FrozenSet[P] = frozenset(P)

#: The authoritative role -> permission matrix.
ROLE_PERMISSIONS: Mapping[R, FrozenSet[P]] = {
    R.ADMIN: _ADMIN,
    R.STATE_OFFICER: _STATE_OFFICER,
    R.DISTRICT_OFFICER: _DISTRICT_OFFICER,
    R.FIELD_OFFICER: _FIELD_OFFICER,
    R.DRIVER: _DRIVER,
    R.VIEWER: _READ_ONLY | frozenset({P.AI_QUERY}),
}

#: How far each role can see. Officers are additionally pinned to a concrete
#: state/district on their user record; ``ScopeLevel`` only says which column the
#: query filter is built from.
ROLE_SCOPE: Mapping[R, ScopeLevel] = {
    R.ADMIN: ScopeLevel.GLOBAL,
    R.STATE_OFFICER: ScopeLevel.STATE,
    R.DISTRICT_OFFICER: ScopeLevel.DISTRICT,
    R.FIELD_OFFICER: ScopeLevel.DISTRICT,
    R.DRIVER: ScopeLevel.SELF,
    R.VIEWER: ScopeLevel.GLOBAL,
}


def permissions_for(roles: Iterable[R]) -> FrozenSet[P]:
    """Union of the permissions granted by ``roles``.

    Users may hold several roles; capabilities are additive while scope is taken
    as the widest of the roles held.
    """

    granted: set = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def has_permission(roles: Iterable[R], permission: P) -> bool:
    """Return whether any of ``roles`` grants ``permission``."""

    return permission in permissions_for(roles)


def scope_for(role: R) -> ScopeLevel:
    """Scope breadth of a single role."""

    return ROLE_SCOPE.get(role, ScopeLevel.SELF)


def widest_scope(roles: Iterable[R]) -> ScopeLevel:
    """Widest scope across ``roles``, defaulting to ``SELF`` when empty.

    Defaulting to the narrowest possible scope means a user with an unrecognised
    or missing role sees only their own records rather than everything.
    """

    levels = [scope_for(role) for role in roles]
    return max(levels) if levels else ScopeLevel.SELF


def describe_role(role: R) -> Dict[str, object]:
    """Serialisable summary of a role, used by ``GET /api/v1/auth/roles``.

    Exposing the matrix over the API lets the frontend hide controls the user
    cannot use, while the server remains the only thing that actually enforces
    access.
    """

    granted = sorted(str(p) for p in ROLE_PERMISSIONS.get(role, frozenset()))
    return {
        "role": str(role),
        "scope": scope_for(role).name,
        "permission_count": len(granted),
        "permissions": granted,
    }

