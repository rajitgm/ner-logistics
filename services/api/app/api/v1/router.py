"""Version 1 of the REST API.

One place where every router group is mounted, so the shape of the API is visible
without opening ``/docs``. The groups the platform needs, and the phase that adds
each one:

===================  =====  ==========================================
Prefix               Phase  Purpose
===================  =====  ==========================================
``/auth``            1      Login, refresh, identity, password change
``/roads``           2      Segments, accessibility status, search
``/weather``         3      Observations, forecasts, alerts
``/incidents``       3, 8   Reports, verification, photos
``/risk``            4      Scores with their contributing factors
``/routes``          6      Candidates, comparison, selection
``/vehicles``        7, 9   Fleet, restrictions, live positions
``/shipments``       7      Consignments and their ETAs
``/alerts``          10     Alert feed and acknowledgement
``/simulation``      11     What-if scenarios
``/ai``              12     Assistant over validated backend tools
``/analytics``       14     Aggregates and audit access
===================  =====  ==========================================

Only ``/auth`` exists today. The table is the plan, not a claim — an endpoint that
is not in ``api_router`` does not answer, and listing it here as "phase 6" is
exactly the point of writing it down.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routers import auth

api_router = APIRouter()
api_router.include_router(auth.router)

__all__ = ["api_router"]
