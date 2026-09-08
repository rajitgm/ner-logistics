"""The contract every external data source is reached through.

Nothing in this platform talks to a weather API, a road extract, a routing
engine or a language model directly. Each of those sits behind an interface
defined here, for one reason that matters more than tidiness: the data we are
allowed to use today is not the data this platform should eventually use.
Rainfall comes from a public forecast API now and from IMD later; hazard zones
come from our own generator now and from Bhuvan later, *if* access is granted.
Swapping those must be a configuration change, not a redesign.

Three rules hold for every implementation in this package.

**Provenance is not optional.** Every record returned carries a
:class:`~app.core.enums.DataProvenance` label, a concrete ``source`` string and
an ``observed_at`` timestamp. A caller cannot accidentally present generated
data as observed data, because there is no code path that produces a record
without saying which it is.

**"Not configured" is a distinct state from "broken".** A restricted government
feed we have no credentials for is represented by a real class that raises
:class:`ProviderNotConfigured`. It never returns plausible-looking numbers. The
class exists to document the integration point and to make enabling it a matter
of supplying a base URL and a key — not to imply we have access.

**Imports stay cheap.** This module and its siblings import only the standard
library and ``app.core.enums``. HTTP clients are imported inside the methods
that use them, so the interfaces and the synthetic implementations can be
exercised by tests with no database, no network and no web framework present.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional

from app.core.enums import DataProvenance

__all__ = [
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderUnavailable",
    "ProviderResponseError",
    "ProviderStatus",
    "Provider",
    "UnconfiguredProvider",
]


class ProviderError(RuntimeError):
    """Base class for every failure raised by an adapter.

    Callers catch this to degrade gracefully. An engine that cannot reach a
    weather source should lower its confidence and say so, not return a 500.
    """


class ProviderNotConfigured(ProviderError):
    """The integration exists but no credentials or endpoint were supplied.

    Raised by adapters for sources we do not currently have authorised access
    to. Distinguished from :class:`ProviderUnavailable` because the operator
    response differs: this one is fixed by configuration, that one by waiting.
    """


class ProviderUnavailable(ProviderError):
    """A configured source could not be reached — timeout, DNS, refused."""


class ProviderResponseError(ProviderError):
    """A configured source answered, but not with something usable."""


@dataclass(frozen=True)
class ProviderStatus:
    """What an adapter reports about itself.

    Surfaced by ``/health/ready`` and by the provenance panel in the dashboard,
    so an operator looking at a number on screen can find out where it came
    from without reading the source.
    """

    kind: str
    implementation: str
    provenance: DataProvenance
    configured: bool
    healthy: Optional[bool] = None
    detail: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "implementation": self.implementation,
            "provenance": str(self.provenance),
            "configured": self.configured,
            "healthy": self.healthy,
            "detail": self.detail,
        }


class Provider(abc.ABC):
    """Common behaviour: identity, provenance, and a self-report.

    Subclasses set the three class variables and inherit a consistent
    :meth:`describe`. ``implementation`` deliberately matches the token used in
    the corresponding setting (``WEATHER_PROVIDER=synthetic``), so a log line
    and an ``.env`` file name the same thing.
    """

    kind: ClassVar[str] = "provider"
    implementation: ClassVar[str] = "unnamed"
    provenance: ClassVar[DataProvenance] = DataProvenance.SYNTHETIC
    configured: ClassVar[bool] = True

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.implementation}"

    def describe(
        self, *, healthy: Optional[bool] = None, detail: Optional[str] = None
    ) -> ProviderStatus:
        return ProviderStatus(
            kind=self.kind,
            implementation=self.implementation,
            provenance=self.provenance,
            configured=self.configured,
            healthy=healthy,
            detail=detail,
        )

    async def healthcheck(self) -> ProviderStatus:
        """Cheap liveness answer.

        The default is a static description: a generator that computes values
        in-process is always healthy, and asking it costs nothing. Adapters
        that cross a network override this and must not raise — a health probe
        that throws is worse than one that reports a failure.
        """

        return self.describe(healthy=True)

    async def aclose(self) -> None:
        """Release anything held. No-op unless an adapter owns a connection."""


class UnconfiguredProvider(Provider):
    """Base for integrations that are declared but not connected.

    Subclasses supply ``reason`` — the specific thing that is missing and, where
    relevant, what authorisation would be required to supply it. Every data
    method calls :meth:`_unavailable`, so there is no path through the class
    that returns a fabricated record.
    """

    configured: ClassVar[bool] = False
    reason: ClassVar[str] = "no endpoint or credentials configured"

    def _unavailable(self) -> ProviderNotConfigured:
        return ProviderNotConfigured(f"{self.name} is not configured: {self.reason}")

    async def healthcheck(self) -> ProviderStatus:
        return self.describe(healthy=None, detail=self.reason)
