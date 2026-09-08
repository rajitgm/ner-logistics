# risk-engine

Risk scoring: factor collection, normalisation to 0-100, confidence, and the deterministic overrides that outrank ML.

**Phase 1 status: not a separate deployable.** The code lives in
`services/api/app/services/risk/` and is called in-process. This directory is reserved for
the point at which extraction is justified by evidence rather than by
architecture diagrams.

See [../README.md](../README.md) for why the engines are packages today and what
keeps extraction cheap.
