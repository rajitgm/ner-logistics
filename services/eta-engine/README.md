# eta-engine

Deterministic ETA today (base + traffic + weather + incident + surface delay), behind an interface an ML model can implement.

**Phase 1 status: not a separate deployable.** The code lives in
`services/api/app/services/eta/` and is called in-process. This directory is reserved for
the point at which extraction is justified by evidence rather than by
architecture diagrams.

See [../README.md](../README.md) for why the engines are packages today and what
keeps extraction cheap.
