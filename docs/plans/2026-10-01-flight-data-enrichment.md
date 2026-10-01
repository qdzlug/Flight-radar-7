# Optional Flight Data Enrichment

## Goal

Add route and commercial flight context to aircraft already tracked by the
receiver/OpenSky merge service without making live radar operation depend on a
paid provider.

The live position path remains:

```text
local receiver + OpenSky -> merge -> ESP32
```

Enrichment is an optional side path:

```text
new aircraft identity -> background provider lookup -> persistent cache
                                             |
next merged response <-----------------------+
```

## Data Contract

Preserve state-vector indices 0-19. Append optional fields so older firmware
continues to parse the response:

| Index | Field | Description |
| --- | --- | --- |
| 20 | flight_number | Provider-normalized airline flight number |
| 21 | departure_airport | ICAO airport code |
| 22 | arrival_airport | ICAO airport code |
| 23 | flight_status | Provider-normalized status |
| 24 | estimated_arrival | Unix timestamp or null |
| 25 | airline | Airline name or null |
| 26 | enrichment_provider | Provider name or null |
| 27 | enrichment_updated_at | Unix timestamp or null |
| 28 | enrichment_stale | Boolean |

Every appended field is nullable. Missing enrichment must never remove or alter
live receiver/OpenSky values.

## Matching Rules

Apply matches conservatively in this order:

1. Previously cached provider flight ID.
2. ICAO24 or registration plus an active time window.
3. Callsign plus an active time window.
4. Callsign alone only when the provider returns exactly one active flight.
5. Do not enrich ambiguous results.

## Fallback Requirements

Enrichment is fail-open and disabled by default.

```text
ENRICHMENT_PROVIDER=none|flightaware
ENRICHMENT_REQUIRED=false
```

Response fallback order:

1. Fresh provider enrichment.
2. Fresh persistent cache entry.
3. Stale cache entry, marked stale.
4. Local readsb/tar1090 metadata.
5. OpenSky state-vector data.
6. Basic ICAO24/callsign data.

Provider timeout, authentication failure, quota exhaustion, malformed data, or
database failure must not delay or fail the base state response. The firmware's
existing direct-OpenSky fallback remains unchanged if the whole merge service
is unreachable.

## Cache And Budget Controls

Use SQLite in the merge service for restart-safe enrichment caching.

Suggested defaults:

| Setting | Default |
| --- | --- |
| Active flight TTL | 10 minutes |
| Completed flight TTL | 12 hours |
| Negative result TTL | 15 minutes |
| Stale grace | 24 hours |
| Provider timeout | 2 seconds |
| Maximum requests/hour | 30 |
| Maximum requests/day | 200 |

Only one lookup may be in flight for a given aircraft identity. Provider calls
run in background tasks; the first sighting returns immediately without route
data and a later device poll receives the cached enrichment.

## Implementation Sections

### 1. Provider-Neutral Foundation

- Add the appended state-vector fields and an enrichment model.
- Add configuration with `none` as the default provider.
- Define provider, cache, and pipeline interfaces.
- Add contract and configuration tests.

### 2. Persistent Cache And Safety Controls

- Add SQLite storage and schema initialization.
- Implement fresh, stale, and negative cache records.
- Add request deduplication, rate limits, and a circuit breaker.
- Expose cache/provider status through `/health`.

### 3. FlightAware Adapter

- Add API-key authentication on the server only.
- Normalize active-flight responses into the provider-neutral model.
- Implement conservative matching and mocked tests.
- Never log the API key or send it to firmware.

### 4. Background Enrichment Pipeline

- Detect identities that need lookup after the normal merge.
- Schedule non-blocking, deduplicated lookups.
- Append cached enrichment to subsequent responses.
- Record hit, miss, stale, skipped, failure, and budget counters.

### 5. Firmware Display

- Parse optional indices 20-28.
- Show route, status, and ETA only when present.
- Mark stale enrichment and preserve the existing detail card otherwise.
- Verify compatibility with 20-field responses.

### 6. Operations And Release

- Document provider setup, costs, and local-only mode.
- Test timeouts, invalid credentials, exhausted budgets, ambiguous callsigns,
  database failure, and total provider outage.
- Ship with `ENRICHMENT_PROVIDER=none`; enable FlightAware explicitly after
  credentials and a budget are configured.

### 7. Optional Flightradar24 Adapter

Implement later behind the same provider interface if a separate FR24 API
subscription is purchased. Feeder/Contributor access alone is insufficient.

## Acceptance Criteria

- Base state responses are not blocked by provider calls.
- Existing 20-field consumers continue to work.
- A provider outage produces a normal radar response.
- Setting the provider to `none` performs no external enrichment calls.
- API keys remain server-side and absent from logs/responses.
- Repeated sightings use cached data rather than paid queries.
- Health output reports provider state, cache state, and budget counters.

## Branch Progress

- Sections 1-4 are implemented with offline tests.
- Section 5 parses and displays route, ETA and status while preserving the
  original cards when enrichment is absent.
- Section 6 includes environment documentation, health counters, a persistent
  Docker volume and local-only defaults.
- Live FlightAware validation is pending an API key and must be completed
  before enrichment is enabled in production.
- The optional Flightradar24 adapter remains deferred.
