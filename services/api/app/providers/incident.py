"""Incident feeds — disruptions reported by something other than our own users.

The primary incident source in this platform is not an adapter at all: it is our
own field officers, whose reports land in the ``incidents`` table through the API
and are read straight from the database by the incident service. This module
exists for the other case — an external agency feed we might one day be
authorised to consume.

The reason it exists now, unused, is the field the risk engine turns on.
``IncidentRecord.is_authoritative`` decides whether an incident may override an
ML prediction and force a segment to ``BLOCKED``. Which sources are allowed to
set it has to be a property of the adapter, decided once and visible, not a flag
that some ingestion script sets because the data looked official.

:class:`InternalIncidentProvider` is therefore an honest no-op, and
:class:`ExternalFeedIncidentProvider` raises. Neither invents an incident: a
fabricated road closure is the single most damaging piece of false data this
system could hold, because the whole point of the platform is to act on it.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import ClassVar, List, Optional, Tuple

from app.core.enums import DataProvenance
from app.providers.base import Provider, ProviderStatus, UnconfiguredProvider
from app.providers.models import IncidentRecord

__all__ = ["IncidentProvider", "InternalIncidentProvider", "ExternalFeedIncidentProvider"]


class IncidentProvider(Provider):
    """Incidents an external source is reporting."""

    kind: ClassVar[str] = "incident"

    @abc.abstractmethod
    async def recent(
        self,
        *,
        since: Optional[datetime] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
    ) -> List[IncidentRecord]:
        """Incidents reported since ``since``, optionally within ``bbox``.

        ``since`` is the ingestion cursor: adapters return what changed after it
        so a poll loop does not re-import the same closure every minute.
        Returning an empty list means "this source is reporting nothing new",
        which is not the same as "the roads are clear" — no caller may read it
        that way.
        """


class InternalIncidentProvider(IncidentProvider):
    """The default: our own reports, which do not arrive through a provider.

    Field reports and operator-entered incidents are written to the database by
    the API and read from it by ``app.services.incidents``. There is no external
    system to poll, so this adapter fetches nothing and says so.

    It is not dead code. It is what makes ``INCIDENT_PROVIDER=external_feed`` a
    configuration change rather than a code change: the ingestion loop calls
    :meth:`recent` on whatever is configured, and in the default deployment that
    call correctly does nothing.
    """

    implementation: ClassVar[str] = "internal"
    #: Our own field reports are observations of the real world, made by people.
    #: The label describes the incident data, not this adapter's (empty) output.
    provenance: ClassVar[DataProvenance] = DataProvenance.REAL
    detail: ClassVar[str] = (
        "internal reports are read directly from the database; this adapter "
        "polls no external system"
    )

    async def recent(
        self,
        *,
        since: Optional[datetime] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
    ) -> List[IncidentRecord]:
        return []

    async def healthcheck(self) -> ProviderStatus:
        return self.describe(healthy=True, detail=self.detail)


class ExternalFeedIncidentProvider(UnconfiguredProvider, IncidentProvider):
    """The integration point for an agency incident feed. Not connected.

    NHAI, NDMA and the state disaster management authorities publish road closure
    and disaster information, but not as machine-readable endpoints this project
    holds access to — what is public is bulletins and press notes, which are not
    a feed. Scraping them into an ``is_authoritative=True`` record would be
    inventing a government dataset and attaching official weight to our guess at
    what a PDF meant.

    Connecting a real feed means supplying a base URL and credentials, mapping its
    incident vocabulary onto :class:`~app.core.enums.IncidentType`, and deciding
    explicitly whether that source is authoritative. Until then this raises.
    """

    implementation: ClassVar[str] = "external_feed"
    provenance: ClassVar[DataProvenance] = DataProvenance.REAL
    reason: ClassVar[str] = (
        "no authorised agency incident feed is configured; public bulletins are "
        "not a machine-readable feed and are not scraped"
    )

    async def recent(
        self,
        *,
        since: Optional[datetime] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
    ) -> List[IncidentRecord]:
        raise self._unavailable()
