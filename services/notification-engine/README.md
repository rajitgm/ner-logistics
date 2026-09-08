# notification-engine

Alert de-duplication, severity routing and fan-out to WebSocket clients and mobile devices.

**Phase 1 status: not a separate deployable.** The code lives in
`services/api/app/services/notifications/` and is called in-process. This directory is reserved for
the point at which extraction is justified by evidence rather than by
architecture diagrams.

See [../README.md](../README.md) for why the engines are packages today and what
keeps extraction cheap.
