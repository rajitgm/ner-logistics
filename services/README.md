# services/

The requirements name seven services. Phase 1 ships one deployable — `api` —
and the six engines live inside it as packages under `services/api/app/services/`.

This is a deliberate deviation, and it is worth being precise about why, because
"microservices" is the kind of word that wins a slide and loses a demo.

## What the engines actually are

Each engine is a bounded set of pure-ish functions over the same PostGIS
database:

| Engine | Responsibility | Phase 1 location |
| --- | --- | --- |
| `risk-engine` | Combine weather, hazard, incident and terrain factors into a 0–100 score with its contributing factors | `app/services/risk/` |
| `routing-engine` | Score candidate routes on reliability, risk, ETA, distance and cost under a configurable weight profile | `app/services/routing/` |
| `weather-engine` | Pull observations and forecasts through `WeatherProvider`, normalise, store with provenance | `app/services/weather/` |
| `incident-engine` | Field-report intake, verification workflow, authoritative-override propagation | `app/services/incidents/` |
| `eta-engine` | Deterministic ETA now, with the interface an ML model will implement later | `app/services/eta/` |
| `notification-engine` | Alert de-duplication, severity routing, fan-out to WebSocket and mobile | `app/services/notifications/` |

The directory for each one exists here, empty, because that is where it goes
when it is extracted.

## Why they are not containers yet

They share one PostGIS database and one Redis. Splitting them into six
containers on day one would buy independent deployment — which nobody needs
during a hackathon — and cost six health checks, six log streams, network hops
inside a single request, and distributed transactions across a risk score, the
route it invalidates and the alert that follows. The demo's critical path is

    incident created → risk recalculated → route recalculated → shipment
    identified → alert raised

and that chain is a single database transaction today. Across services it
becomes an eventual-consistency problem with a visible failure mode: a map
showing a blocked road and a route still using it.

## What makes extraction cheap later

The boundary is enforced in code, not by a network:

- Every engine is called through a module-level function with typed Pydantic
  inputs and outputs — never by reaching into another engine's ORM objects.
- Every external dependency is behind an adapter in `app/providers/`
  (`WeatherProvider`, `OSMProvider`, `HazardProvider`, `IncidentProvider`,
  `RoutingProvider`, `LLMProvider`, `NotificationProvider`), so an engine talks
  to an interface whether the implementation is in-process or across a socket.
- No engine imports a FastAPI router or a `Request`. The HTTP layer in
  `app/api/v1/routers/` is a thin shell that validates, authorises, and calls.

Extraction is then: wrap the module in its own FastAPI app, replace the
in-process call with an HTTP client that satisfies the same signature, and move
the directory. Nothing in the risk or routing logic changes.

If a phase produces a reason to split — a training job starving the API of CPU,
say — the split happens then, on evidence.
