"""Outbound message transports.

Channel selection and recipient resolution belong to the Phase 10 alert engine: it
decides that a CRITICAL landslide alert for Ri-Bhoi goes to the district officer
by SMS and to the command centre in-app. A provider here only delivers an already
rendered message, which keeps the policy in one place and makes every transport
swappable.

Nothing here sends anything today. SMS to Indian numbers needs a gateway account
and DLT template registration; email needs SMTP credentials; push needs an FCM
service account. None of those are configured, so those three classes raise. What
ships is :class:`LoggingNotificationProvider`, which writes the message to the
application log so the alert pipeline is observable end to end in development.

It carries ``real_delivery = False``, and that flag exists so nothing downstream
has to guess. The alert engine records a log-sink result as SENT, never DELIVERED:
a message that was written to stdout did not reach a district officer, and a
notifications table that says otherwise would be a lie told to whoever audits the
response to an incident later.
"""

from __future__ import annotations

import abc
import logging
from typing import ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.enums import DataProvenance, NotificationChannel
from app.providers.base import Provider, ProviderStatus, UnconfiguredProvider
from app.providers.models import NotificationRequest, NotificationResult

__all__ = [
    "CompositeNotificationProvider",
    "EmailNotificationProvider",
    "LoggingNotificationProvider",
    "NotificationProvider",
    "PushNotificationProvider",
    "SmsNotificationProvider",
    "default_channels",
]

logger = logging.getLogger("app.notifications")


class NotificationProvider(Provider):
    """One or more delivery channels."""

    kind: ClassVar[str] = "notification"
    provenance: ClassVar[DataProvenance] = DataProvenance.DERIVED
    #: Channels this transport claims to handle. Not a ``ClassVar``: the composite
    #: below computes its own set from the transports it was given.
    channels: Tuple[NotificationChannel, ...] = ()
    #: False when accepting a message does not mean it left this system. The
    #: alert engine reads this to choose between SENT and DELIVERED.
    real_delivery: bool = True

    def supports(self, channel: str) -> bool:
        return any(channel == str(known) for known in self.channels)

    @abc.abstractmethod
    async def send(self, request: NotificationRequest) -> NotificationResult:
        """Attempt one delivery.

        Returns a result rather than raising for an ordinary rejection — a wrong
        phone number is data, not an outage — and raises
        :class:`~app.providers.base.ProviderError` only when the transport itself
        is unusable.
        """


class LoggingNotificationProvider(NotificationProvider):
    """Writes messages to the application log. Development and test default.

    Deliberately not silent: an operator running the stack locally should be able
    to watch the alert engine fire and see exactly what text would have gone out,
    to which recipient, on which channel. That makes the Phase 10 pipeline
    verifiable without buying an SMS gateway.
    """

    implementation: ClassVar[str] = "logging"
    real_delivery: bool = False
    channels: Tuple[NotificationChannel, ...] = (
        NotificationChannel.IN_APP,
        NotificationChannel.WEBSOCKET,
        NotificationChannel.EMAIL,
        NotificationChannel.SMS,
        NotificationChannel.PUSH,
    )

    async def send(self, request: NotificationRequest) -> NotificationResult:
        logger.info(
            "notification (log sink, not delivered): channel=%s recipient=%s "
            "severity=%s subject=%s body=%s",
            request.channel,
            request.recipient,
            request.severity or "-",
            request.subject or "-",
            request.body,
        )
        return NotificationResult(
            channel=request.channel,
            delivered=True,
            provider=self.name,
            detail="written to the application log; no message left this system",
        )

    async def healthcheck(self) -> ProviderStatus:
        return self.describe(healthy=True, detail="log sink; no real delivery")


class CompositeNotificationProvider(NotificationProvider):
    """Routes each message to the first transport that claims its channel.

    The fallback is what keeps the alert engine simple: it asks for SMS, and if no
    SMS transport is configured the message still lands in the log with a note
    saying the requested channel was unavailable. An alert that vanishes because
    nothing was configured to carry it is worse than one that is visibly
    misrouted.
    """

    implementation: ClassVar[str] = "composite"

    def __init__(
        self,
        providers: Iterable[NotificationProvider],
        *,
        fallback: Optional[NotificationProvider] = None,
    ) -> None:
        self._providers: List[NotificationProvider] = [
            p for p in providers if p.configured
        ]
        self._fallback = fallback or LoggingNotificationProvider()
        # True only if at least one configured transport actually delivers.
        self.real_delivery = any(p.real_delivery for p in self._providers)
        seen: Dict[NotificationChannel, None] = {}
        for provider in (*self._providers, self._fallback):
            for channel in provider.channels:
                seen.setdefault(channel, None)
        self.channels = tuple(seen)

    async def send(self, request: NotificationRequest) -> NotificationResult:
        for provider in self._providers:
            if provider.supports(request.channel):
                return await provider.send(request)
        result = await self._fallback.send(request)
        return NotificationResult(
            channel=result.channel,
            delivered=result.delivered,
            provider=result.provider,
            detail=(
                f"no transport configured for channel {request.channel}; "
                f"{result.detail or 'handled by fallback'}"
            ),
            external_id=result.external_id,
        )

    async def aclose(self) -> None:
        for provider in (*self._providers, self._fallback):
            await provider.aclose()

    async def healthcheck(self) -> ProviderStatus:
        names = ", ".join(p.name for p in self._providers) or "none"
        return self.describe(
            healthy=True,
            detail=f"transports: {names}; fallback {self._fallback.implementation}",
        )


class _UnconfiguredTransport(UnconfiguredProvider, NotificationProvider):
    """Shared body for the transports that are declared but not connected."""

    async def send(self, request: NotificationRequest) -> NotificationResult:
        raise self._unavailable()


class SmsNotificationProvider(_UnconfiguredTransport):
    """SMS. Not connected.

    Reaching Indian mobile numbers at scale needs a gateway account and, under
    TRAI's DLT regime, sender-id and template registration for every message
    pattern used. That is a procurement step, not a code change, and it matters
    for this platform specifically: SMS is the channel that works when a district
    officer is somewhere with no data coverage, which is exactly when a road
    closure alert is most needed.
    """

    implementation: ClassVar[str] = "sms"
    channels: Tuple[NotificationChannel, ...] = (NotificationChannel.SMS,)
    reason: ClassVar[str] = (
        "no SMS gateway credentials; Indian delivery also requires DLT sender-id "
        "and template registration"
    )


class EmailNotificationProvider(_UnconfiguredTransport):
    """SMTP email. Not connected: no server, credentials or sender identity."""

    implementation: ClassVar[str] = "email"
    channels: Tuple[NotificationChannel, ...] = (NotificationChannel.EMAIL,)
    reason: ClassVar[str] = "no SMTP host, credentials or from-address configured"


class PushNotificationProvider(_UnconfiguredTransport):
    """Mobile push for the field app. Not connected.

    Needs an FCM (or equivalent) service account and device tokens registered by
    the Flutter app, neither of which exists yet. Until then field devices receive
    alerts when they poll or reconnect, which is what the offline sync layer is
    for — and that is a real limitation, not a hidden one.
    """

    implementation: ClassVar[str] = "push"
    channels: Tuple[NotificationChannel, ...] = (NotificationChannel.PUSH,)
    reason: ClassVar[str] = "no push service account configured and no device tokens registered"


def default_channels() -> Sequence[NotificationChannel]:
    """Channels that work in the default deployment: in-app and websocket only.

    Both are served by this application itself — a row in ``notifications`` and a
    frame on an open socket — so they need no external transport. Naming them here
    keeps the alert engine from offering a channel that cannot carry anything.
    """

    return (NotificationChannel.IN_APP, NotificationChannel.WEBSOCKET)
