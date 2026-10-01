# Flight Radar Merge Service

A small FastAPI service that sits between the ESP32 flight radar and the
upstream aircraft data providers. It merges several sources into a single
OpenSky-compatible `/states/all` response.

```
ESP32 radar  ──GET /states/all?lamin=..&lomax=..──▶  merge service  ──┬─▶  OpenSky Network
                                                                   ├─▶  adsb.lol
                                                                   ├─▶  adsb.fi
                                                                   └─▶  tar1090 / dump1090 (local)
```

## Why this exists

The firmware can talk to OpenSky directly, but going through this service buys
four things:

1. **Credentials stay server side.** The OpenSky OAuth2 client id and secret
   live in the container's environment, not in the device's NVS. The ESP32
   never needs them.
2. **Aircraft type and registration appear.** OpenSky's documented REST
   response stops at 17 fields and only sometimes includes a category, so
   `typecode` and `registration` are normally unavailable. The ADS-B sources
   publish them, and the merge fills them in.
3. **More aircraft.** Anything an ADS-B source sees but OpenSky does not gets
   appended, including aircraft from a local receiver.
4. **One upstream request per poll.** Requests are cached for a few seconds, so
    several devices polling at once cost a single OpenSky call.
5. **Optional flight context.** A disabled-by-default enrichment pipeline can
   add route, status and ETA data without making radar positions depend on a
   paid provider.

## Quick start

```bash
cd server
docker compose up -d --build
```

Without Docker:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Check it:

```bash
curl "http://localhost:8000/states/all?lamin=13.10&lamax=13.30&lomin=77.60&lomax=77.80"
```

## Pointing the radar at it

1. Find the service's IP address on the same network as the display.
2. On the radar, connect to WiFi and open `http://<device-ip>/` in a browser.
3. Enter the merge service URL in **Radar Data Source**, for example
   `http://192.168.1.50:8000/states/all`, and save.
4. Reboot. The radar requests that URL each poll, and falls back to OpenSky
   directly if the service is unreachable.

The device appends its own `lamin`/`lamax`/`lomin`/`lomax` query parameters, so
the saved URL must not contain a query string of its own.

## Endpoints

| Method | Path              | Purpose                                                     |
| ------ | ----------------- | ----------------------------------------------------------- |
| GET    | `/states/all`     | Merged state vectors. Requires the four bounding box values. |
| GET    | `/states`         | Alias of `/states/all`.                                      |
| GET    | `/`               | Service index, or merged state vectors if a box is supplied. |
| GET    | `/health`         | Uptime, enabled sources, cache counters.                     |
| GET    | `/docs`           | Interactive OpenAPI documentation.                           |

Bounding box parameters, matching the OpenSky API:

| Parameter | Meaning                     |
| --------- | --------------------------- |
| `lamin`   | lower latitude bound        |
| `lamax`   | upper latitude bound        |
| `lomin`   | lower longitude bound       |
| `lomax`   | upper longitude bound       |

A malformed or inverted box returns `400` with a description. If every upstream
source fails the service returns `502` — unless it has a recent cached response
for that box, in which case it serves the stale data with `X-Merge-Cache: stale`
so the radar keeps showing the last known traffic.

Response headers report how the answer was produced:

| Header          | Meaning                                             |
| --------------- | --------------------------------------------------- |
| `X-Merge-Cache` | `miss`, `hit` or `stale`                            |
| `X-Merge-Age`   | age of the served data in seconds                   |

## Response format

The body is OpenSky's `/states/all` shape, so anything that understands OpenSky
also understands this service:

```json
{
  "time": 1790380593,
  "states": [
    ["801810", "AKJ645Y", "India", 1790380580, 1790380590, 77.6026, 13.2811,
     2141.2, false, 127.04, 7.21, 11.05, null, 2180.0, "2202", false, 0,
     4, "B38M", "VT-YBK", "QP645", "VOBL", "VIDP", "En Route",
     1790384400, "Akasa Air", "flightaware", 1790380592, false]
  ],
  "meta": { "...": "merge counters and per source status" }
}
```

Each `states` row has exactly 29 fields. Indices 0-19 preserve the existing
OpenSky-compatible layout:

| Index | Field           | Unit                    | Index | Field           | Unit             |
| ----- | --------------- | ----------------------- | ----- | --------------- | ---------------- |
| 0     | `icao24`        | hex string              | 10    | `true_track`    | degrees          |
| 1     | `callsign`      | text                    | 11    | `vertical_rate` | m/s              |
| 2     | `origin_country`| text                    | 12    | `sensors`       | always `null`    |
| 3     | `time_position` | unix seconds            | 13    | `geo_altitude`  | m                |
| 4     | `last_contact`  | unix seconds            | 14    | `squawk`        | text             |
| 5     | `longitude`     | degrees                 | 15    | `spi`           | bool             |
| 6     | `latitude`      | degrees                 | 16    | `position_source` | 0 ADS-B, 2 MLAT |
| 7     | `baro_altitude` | m                       | 17    | `category`      | OpenSky integer  |
| 8     | `on_ground`     | bool                    | 18    | `typecode`      | e.g. `A388`      |
| 9     | `velocity`      | m/s                     | 19    | `registration`  | e.g. `N123UA`    |

Indices 18 and 19 extend the documented OpenSky layout; they are what the radar
shows next to the callsign. Optional commercial enrichment is appended without
moving those fields:

| Index | Field | Description |
| ----- | ----- | ----------- |
| 20 | `flight_number` | Provider-normalized flight number. |
| 21 | `departure_airport` | ICAO airport code. |
| 22 | `arrival_airport` | ICAO airport code. |
| 23 | `flight_status` | Provider status text. |
| 24 | `estimated_arrival` | Unix timestamp. |
| 25 | `airline` | Airline/operator name. |
| 26 | `enrichment_provider` | Provider that supplied the fields. |
| 27 | `enrichment_updated_at` | Unix timestamp. |
| 28 | `enrichment_stale` | Whether stale fallback data was used. |

Every row is padded to 29 fields. Older consumers remain compatible because the
first 20 indices are unchanged and enrichment is nullable.

Units are SI everywhere. The ADS-B sources publish feet, knots and feet per
minute, and the service converts on the way in — the firmware multiplies by
`3.28084` and `3.6` for display, which only makes sense if the row is metres and
m/s.

ADS-B emitter categories are also translated. They are offset by one from
OpenSky's integers, so `A5` becomes `6` (heavy), not `5` (high vortex large).

## Merge rules

Sources are consulted in the order given by `ADSB_SOURCES`:

1. The first source is the base. Its values always win.
2. Later sources only fill `null` fields, so a source that lags a few seconds
   behind cannot drag a live position backwards.
3. Aircraft a later source reports but the base does not are appended.
4. Rows outside the requested box, and rows without a position, are dropped.
5. Output is truncated to `MAX_STATES`.

Because ADS-B sources are queried by radius around the box centre, their results
cover the box corners too. Filtering to the box is safe: the radar only draws
inside its configured range, which is the circle inscribed in that box.

## Configuration

All settings are environment variables.

### Sources

| Variable                | Default                 | Description                                                                 |
| ----------------------- | ----------------------- | --------------------------------------------------------------------------- |
| `ADSB_SOURCES`          | `adsb.lol,adsb.fi`      | Comma separated: `adsb.lol`, `adsb.fi`, `tar1090`, `custom=<url>`.             |
| `TAR1090_URL`           | empty                   | Base URL of a local dump1090/tar1090 receiver, e.g. `http://pi.local:8080`.   |
| `ADSB_LOL_URL`          | `https://api.adsb.lol/v2` | Override for testing.                                                      |
| `ADSB_FI_URL`           | `https://opendata.adsb.fi/v2` | Override for testing.                                                  |
| `OPENSKY_CLIENT_ID`     | empty                   | OpenSky OAuth2 client id. Anonymous access is used when both are unset.      |
| `OPENSKY_CLIENT_SECRET` | empty                   | OpenSky OAuth2 client secret.                                               |
| `OPENSKY_BASE_URL`      | `https://opensky-network.org/api` | Override for testing.                                            |
| `OPENSKY_EXTENDED`      | `false`                 | Adds `extended=1` to the OpenSky request. May cost more API credits.        |

`tar1090` in `ADSB_SOURCES` is ignored with a warning unless `TAR1090_URL` is
set. `custom=<url>` points at any readb v2 compatible endpoint, which is how you
add your own ADS-B aggregator.

### Behaviour

| Variable             | Default | Description                                                              |
| -------------------- | ------- | ------------------------------------------------------------------------ |
| `MAX_STATES`         | `150`   | Maximum rows returned.                                                    |
| `CACHE_TTL_S`        | `5`     | How long a merged response is reused.                                     |
| `CACHE_MAX_ENTRIES`  | `64`    | Maximum cached bounding boxes.                                            |
| `STALE_GRACE_S`      | `120`   | How long stale data may be served when all sources fail.                  |
| `REQUIRE_POSITION`   | `true`  | Drop rows that have no latitude/longitude.                                |
| `DROP_ON_GROUND`     | `false` | Drop rows flagged as surface reports.                                     |
| `ADSB_TIMEOUT_S`     | `8`     | Per request timeout for ADS-B sources.                                    |
| `OPENSKY_TIMEOUT_S`  | `12`    | Per request timeout for OpenSky, including token requests.                |
| `HOST`               | `0.0.0.0` | Bind address.                                                           |
| `PORT`               | `8000`  | Bind port.                                                                |
| `LOG_LEVEL`          | `info`  | Python logging level.                                                     |

### Optional flight enrichment

Enrichment is local-only by default. It never blocks provider calls in the
state response path: a new aircraft schedules a background lookup and a later
poll receives cached fields.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `ENRICHMENT_PROVIDER` | `none` | `none` or `flightaware`. |
| `FLIGHTAWARE_API_KEY` | empty | AeroAPI key, retained server-side. |
| `FLIGHTAWARE_BASE_URL` | FlightAware AeroAPI | Override for testing. |
| `ENRICHMENT_CACHE_PATH` | `./data/enrichment.sqlite3` | Persistent SQLite cache. |
| `ENRICHMENT_TTL_S` | `600` | Fresh positive result lifetime. |
| `ENRICHMENT_NEGATIVE_TTL_S` | `900` | No-result lifetime. |
| `ENRICHMENT_STALE_GRACE_S` | `86400` | Maximum stale fallback age. |
| `ENRICHMENT_TIMEOUT_S` | `2` | Provider timeout. |
| `ENRICHMENT_MAX_REQUESTS_PER_HOUR` | `30` | Hourly safety limit; `0` is unlimited. |
| `ENRICHMENT_MAX_REQUESTS_PER_DAY` | `200` | Daily safety limit; `0` is unlimited. |

If the API key is absent, the provider fails, a limit is reached, or SQLite is
unavailable, the service still returns normal receiver/OpenSky state vectors.
SQLite failure falls back to a volatile cache. Three consecutive provider
failures open a one-minute circuit breaker.

### A note on `MAX_STATES`

The firmware fetches into a 64 KiB buffer and fails the whole request if the
response overflows it. A fully enriched row is roughly 220 bytes, so `150` rows
land near 33 KiB. Keep `MAX_STATES` near its default; raising it much beyond
`250` risks overflowing the device response buffer.

## API credit usage

OpenSky `/states/all` costs credits per request, and the quota depends on your
tier: 400/day anonymous, 4,000/day for a standard account, 14,400/hour for a
licensed one. The credit cost rises with bounding box area, from 1 for a small
box to 4 for a global query.

The cache only helps when devices poll close together, so set the radar's poll
interval with that budget in mind. The firmware allows 10-120 s and defaults to
25 s, which is about 3,450 requests a day. At one credit per request that
overflows an anonymous quota, so configure `OPENSKY_CLIENT_ID` and
`OPENSKY_CLIENT_SECRET`, or raise the interval. A wider radar range also costs
more per request, so `Range` and `Poll` trade off against each other.

If OpenSky stops answering with `429`, the service keeps serving the previous
response until `STALE_GRACE_S` expires, and `/health` still reports `ok`.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The suite covers unit conversion, the emitter category map, merge precedence and
the API's cache, stale and failure paths. It makes no network calls.
