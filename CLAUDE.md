# Flight Tracker

A self-hosted, Dockerised web app that tracks live flights on a map. Runs as one container and is viewed in any browser.

## Goals

1. **Global reference** – browse/search flights worldwide (by callsign, flight number, airline, ICAO24 hex).
2. **Track up to 10 flights** at once (`MAX_TRACKED`, default 10). Tracked flights persist across restarts (`DATA_DIR`, default `/data`).
3. **Flight detail map** – selecting a flight draws origin → destination (great-circle route, flown portion solid, remaining dashed, live plane marker rotated to heading, trail of past positions) plus a detail card: airline, flight no., aircraft type/registration, origin/destination airports, altitude, speed, heading, vertical rate, phase (climb/cruise/descent), distance flown/remaining, progress %, ETA, delay.
4. **Regional overhead view** – panning/zooming the map to a region (e.g. Phoenix, AZ) shows the flights currently inside the visible bounds. Re-query on `moveend` (debounced); cap results at `MAX_REGION_FLIGHTS`.
5. **Ticker view** – a text-first board of tracked flights: airline logo/image, flight number, ETA, delay status, total flight time, heading, speed. Updates live; usable full-screen like an airport departures board.
6. Use **free flight-data APIs only**. Every paid/keyed source must be optional and degrade gracefully.

## Free-plan behaviour

Defaults are conservative out of the box (`POLL_INTERVAL` 20s, `REGION_INTERVAL` 30s, `OPENSKY_GLOBAL_TTL` 5 min, `SCHEDULE_TTL` 6h, plus the `*_MIN_INTERVAL` request-rate floors above) and mode is **data saver** by default: poll intervals, the OpenSky worldwide-snapshot TTL and AviationStack schedule TTLs stretch automatically to fit what's left of each plan. The header **↻ Refresh** button and **⚙ Data** panel (usage bars, mode, tracked/map refresh speed) let users override; **manual** mode does no background refreshing, and panning the map then just highlights Refresh. Exhausted providers are skipped (chains fall back to the next source); the worldwide view and schedules/delays degrade with a visible message rather than erroring.

## Known frontend gotcha: antimeridian / world-copy mismatch

A route's shorter great-circle path can cross ±180° (e.g. Tokyo→LAX); the origin/destination pins (raw
airport coordinates) and the drawn route line (built by walking continuously from the origin so Leaflet
doesn't stretch it across the whole map) can then land 360° apart even though they're the same real
point. `app.js`'s `renderRoute()` fixes this by rebasing every pin and line segment (`rebase`/`nearestLon`)
onto the same "world copy" as the aircraft's own current position before drawing, and passes the same
rebased points into `fitBounds` so it zooms to the short way round instead of the long one. Any new
map-drawing code that mixes raw airport/waypoint coordinates with a continuously-unwrapped line needs the
same treatment.

## UI routes (hash based)

Selecting a flight starts zoomed in on the aircraft with just its recent trail, not the full origin-to-destination line (which for a long-haul flight would force the map out to a barely-readable world view); "Show full flight path" in the detail card reveals that line and fits the map to it, and resets on every new selection (`app.js`: `S.showFullPath`, `togglePath()`).

Every place a tracked flight appears (sidebar card, ticker row, flight page) has its own untrack control - sidebar and ticker use an inline `×`, the flight page a "Stop tracking" button that then moves to the next tracked flight or back to the ticker if none remain.

`#/map` (default), `#/ticker`, `#/flight/<id>` – full-screen page for one tracked flight (big logo/badge, route progress, tiles for ETA/delay/total time/heading/speed/altitude/aircraft; ←/→ cycle flights, F full screen, Esc back, optional auto-rotate). Ticker rows link to the flight page, not the map; "View on map" is a button there.

## Current state

Implemented and smoke-tested in `DEMO_MODE` (API, WebSocket push, 10-flight cap → 409, unit tests pass):
`backend/app/main.py` (API + WS + static), `backend/requirements*.txt`, `backend/tests/`, `frontend/{index.html,style.css,app.js}`, `Dockerfile`, `docker-compose.yml`, `.env.example`.

Data-budget layer (`budget.py`, `runtime.py`, ⚙ Data panel) added after that: unit-tested and smoke-tested in demo mode (settings/refresh endpoints); the panel UI is untested in a browser and real provider headers (OpenSky `X-Rate-Limit-Remaining`) are unverified.

Filters (category chips, airline, airport, hide-on-ground; saved in `localStorage`) and the flight page were added later; the filter API was smoke-tested in demo mode, the JS only syntax-checked.

**Not yet verified:** the `docker build` (Dockerfile downloads Leaflet from jsdelivr at build time, since `frontend/vendor/` is git-ignored), the browser UI against a real browser, and the live adsb.lol/OpenSky/adsbdb calls (the dev sandbox firewall blocked them). Airline logos live in `frontend/logos/<IATA>.png` (fetch with `python scripts/fetch_logos.py`, which needs internet access the dev sandbox lacks; the UI falls back to coloured initials for any missing file).
This directory is not a git repo yet.

## Architecture

- **Backend**: Python 3.12 + FastAPI + `httpx` (async) + uvicorn. It also serves the static frontend (`FRONTEND_DIR`) so a single container/port is enough.
- **Frontend**: no build step – plain HTML/CSS/ES modules with **Leaflet** (OpenStreetMap/CARTO tiles, with attribution). Keep it dependency-light so it works from the container without npm.
- **Visual identity**: grounded in real departures-board/instrument-panel language, not a generic dark SaaS dashboard - warm near-black (`#0b0d09`) instead of navy, amber accent (`#e8a33d`, the actual color of an airport board) instead of cyan, Overpass (rooted in US wayfinding/highway-signage letterforms) for UI text, IBM Plex Mono for every number so data reads as instrumentation. Fonts are self-hosted (`frontend/fonts/`, fetched by the Dockerfile's vendor stage like Leaflet is - gitignored, never committed) rather than loaded from Google's CDN at runtime. The Ticker and single-flight page are the one deliberate "hero" moment (large scale, amber, mono digits, real board conventions like ALL-CAPS column headers - the one place that's authentic rather than a generic template tell); the map/sidebar stay a quiet, disciplined tool around it.
- **Live updates**: backend polls upstream APIs and pushes to browsers via WebSocket or SSE (`tracker.py` supports listener callbacks); the browser never calls upstream APIs directly (avoids CORS, key leakage, and rate-limit multiplication).
- **Container**: one image, port 8000, `/data` volume for persisted tracked flights, config only via env vars. `docker compose up` must produce a working app with no keys (uses adsb.lol + adsbdb).

### Data flow

```
browser ──HTTP/WS──> FastAPI ──> providers.Chain (position data, first success wins, later fill gaps)
                          │        ├─ adsb   (readsb v2: adsb.lol default; airplanes.live / adsb.fi compatible)
                          │        └─ opensky (global snapshot + bbox; OAuth2 optional)
                          └──> providers.Enricher (route, aircraft, schedule/delay; TTL-cached)
                                   ├─ adsbdb.com   (callsign → origin/destination, hex → aircraft)  no key
                                   └─ AviationStack (schedule, delay, gates)  optional key, ~100 req/month
```

### Key modules

- `providers/base.py` – `State` dataclass and `PositionProvider` interface. **All providers normalise to aviation units**: feet, knots, feet/min, degrees true. Convert at the provider boundary only.
- `providers/__init__.py` – `Chain` fallback logic and `build()`, which returns `(track_chain, region_chain, global_chain, enricher)`. Only OpenSky supplies the global snapshot.
- `providers/adsb.py` – readsb v2 endpoints `/v2/callsign/{cs}`, `/v2/hex/{hex}`, `/v2/point/{lat}/{lon}/{radius_nm}` (radius capped at 250 nm; bbox queries are converted to point+radius).
- `providers/opensky.py` – `/states/all` (+ bbox params). Anonymous is heavily rate limited; `OPENSKY_CLIENT_ID/SECRET` enable OAuth2.
- `providers/enrich.py` – `TTLCache`, route/aircraft/schedule lookups. Cache aggressively (negative results too, with a shorter TTL).
- `providers/demo.py` – deterministic simulated traffic (`DEMO_MODE=true`) for offline dev, demos and tests.
- `tracker.py` – tracked-flight manager: polling loop, trails (`TRAIL_MAX`), `derive()` for display metrics (phase, distances, progress, ETA, emergency squawks 7500/7600/7700), disk persistence, "lost" after `LOST_AFTER` seconds without a position. `TrackedFlight.update()` rejects a position implying >`MAX_PLAUSIBLE_KT` ground speed from the last fix (an ADS-B decode glitch, not real motion) unless it repeats `MAX_GLITCH_REJECTS` times in a row, in which case it accepts it rather than getting stuck.
- `airlines.py` – IATA↔ICAO airline table (~120 hand-picked majors plus ~650 more loaded from `data/airlines_extra.json`, generated by `scripts/build_airlines.py` from OpenFlights, ODbL; regenerate to refresh). Logos are matched by IATA code, so an airline missing from this table never gets a logo. Aircraft broadcast the **ICAO** callsign (`BAW123`) while users type the **IATA** flight number (`BA123`); always map between them.
- `budget.py` – API call metering for free plans. httpx event hooks on the shared client count every request per provider host (OpenSky cost by bbox area: 1–4 credits; AviationStack 1), honour provider headers (`X-Rate-Limit-Remaining`, 429 back-off), and raise `BudgetExceeded` when a plan is exhausted so the provider chains fall through. Hosts with no published daily quota (adsb.lol, adsbdb) still get a hard floor on request *rate* (`Meter.min_interval`, `ADSB_MIN_INTERVAL`/`ROUTE_MIN_INTERVAL`), enforced centrally across every caller (tracked polling, map browsing, route warm-up) so bursts from several features hitting the same host at once can't trip the provider's own abuse limits. Map browsing (`purpose_var="region"`) can't spend the last 35% of a limited allowance – tracked flights win. `ttl_for`/`saver_interval` stretch cache TTLs and poll intervals so allowances last until reset. Counters persist in `DATA_DIR/usage.json`. Default limits (OpenSky 400/day anonymous, 4000 with client id; AviationStack 100/month) are approximations – configurable via env.
- `runtime.py` – user-adjustable refresh `mode` (live / saver (default) / manual) and tracked-poll interval, persisted in `DATA_DIR/runtime.json`.
- `classify.py` – heuristic category (commercial / cargo / military / government / private / helicopter / unknown) from readsb `dbFlags`, ICAO24 blocks (US/UK military), callsign patterns and emitter category. Best-effort only; extend the prefix lists there.
- `airports.py` – local IATA/ICAO -> {name, city, country, lat, lon} lookup, loaded from `data/airports.json` (built by `scripts/build_airports.py` from OurAirports, public domain; regenerate to refresh). The one thing about an airport free sources are never wrong about is *where it is* - kept deliberately separate from the unreliable "which two airports does this flight connect" lookup.
- `geo.py` – haversine, bearing, great-circle interpolation, cardinal direction, `cross_track_km` (perpendicular distance from a point to a great-circle route, used to flag a route that doesn't match the live position).

## Free APIs

| Purpose | Source | Notes |
|---|---|---|
| Live positions, callsign/hex/area lookup | adsb.lol (`api.adsb.lol`), airplanes.live, adsb.fi | No key. Set via `ADSB_API_BASE`. Be polite: ~1 req/s, identify with `USER_AGENT`. |
| Global snapshot, bbox | OpenSky Network | Free; registered OAuth2 client gives far higher limits. Cache global snapshot (`OPENSKY_GLOBAL_TTL`). |
| Route origin/destination, aircraft info | adsbdb.com | No key. Routes are callsign-based and can be stale/wrong for reused callsigns – treat as best-effort. |
| Airports (coords, names) | OurAirports CSV (public domain) | Bundle into `data/`; don't fetch at runtime. |
| Schedule, delay, gates | AviationStack free tier | Optional. HTTP-only, ~100 req/month → refresh rarely (`SCHEDULE_TTL`). Delay UI must show "unknown" when unavailable. |
| Place search (map "jump to") | Nominatim (OSM) | Called from the browser, one request per user search only. |
| Airline logos | Local static files keyed by IATA/ICAO code, with a text/initials fallback | Never hotlink at runtime; missing logo must not break the ticker. |

Route lookups are matched by **callsign only** (adsbdb has no live flight-plan data), so a reused or stale
callsign - e.g. a regional turboprop flying several different sectors a day whose transponder wasn't reset
between flights - can show a perfectly good live position next to a route it isn't actually flying. When the
live position sits implausibly far off the looked-up route's great-circle line (`cross_track_km` in `geo.py`,
threshold in `tracker.derive()`: a flat `ROUTE_SUSPECT_NM = 200` nautical miles, skipped while on the ground - well
beyond how far a normal flight ever deviates from a direct line for routing/weather/holding), `derived.route_suspect`
(km off-line) is set and the UI shows a warning instead of trusting the progress/ETA numbers.

**adsbdb's route table can simply be wrong, routinely** - confirmed live against several currently-flying
United flights: adsbdb had UAL2168 as JAX->IAH (actually Denver->LaGuardia) and UAL2662 as DEN->PDX
(actually Orlando->Newark), both confirmed against FlightAware. This isn't a stale/reused-callsign edge
case; mainline flight numbers get reassigned to different city pairs routinely and adsbdb's static,
callsign-only lookup doesn't track that. `tracker.effective_route(route, schedule)` corrects for it: when
a real (AviationStack) schedule is available, its departure/arrival airports - looked up by flight number
for *today* - replace adsbdb's guess; their coordinates come from our own local `airports.py` database
(adsbdb has no airport-by-code endpoint to ask instead). `TrackedFlight.to_dict()`/`status()` always run
the *effective* route through `derive()`, so progress/distance/ETA math also uses the corrected airports.
`_fix_flight_iata()` handles a related adsbdb gap found the same way: for an operator adsbdb's own
airline table has no IATA code for (confirmed live: Envoy/"ENY", real IATA "MQ" per our own airlines.py),
`callsign_iata` comes back as a bare number ("3780") - not a real flight designator, and not something
AviationStack can be usefully queried with. It's rebuilt from our own table for *display*, but marked
`flight_iata_unreliable` and never used to query a schedule (regional flights are often ticketed under a
different marketing carrier's code, so a wrong guess there risks pulling back someone else's schedule
entirely) - the schedule lookup falls back to the unambiguous ICAO callsign in that case.

When a correction happens, `derived.route_corrected = {origin?, destination?}` carries adsbdb's overridden
codes and the UI shows an informational note. Without an AviationStack key there's no way to correct the
route - `route_suspect` (above) is the only guard, and it only catches mismatches large enough to move the
aircraft's position off the (wrong) line.

Confirmed live again for a second, unrelated flight (SWA826: adsbdb says DEN->RDU, actually BUR->PHX) that
the correction is silent when it can't run - no AviationStack key, no schedule for that flight/date, or a
failed lookup all look the same to `effective_route()`: nothing to check against. That's a different
problem from "wrong and we fixed it" or "wrong and we can tell from position", so it gets a third, quieter
signal: `route.unverified = True` -> `derived.route_unverified = True`, shown as a muted one-line note (not
an alert - this is the default/common case for anyone without a schedule key, not something alarming) below
`route_suspect` in priority. The honest summary: without a real schedule provider, this app has **no way**
to know whether adsbdb's route is right, and says so instead of showing a guess with false confidence.

Free ADS-B feeds do **not** give scheduled times or delays. Without a schedule provider, estimate ETA from great-circle remaining distance ÷ ground speed and label it "estimated"; only show a delay when schedule data actually exists.

## Configuration (env vars, see `config.py`)

`DEMO_MODE`, `TRACK_PROVIDERS` (default `adsb,opensky`), `REGION_PROVIDERS` (default `adsb,opensky` – free unlimited adsb.lol first, OpenSky only when a region exceeds adsb's 250 nm radius), `OPENSKY_DAILY_LIMIT`, `ADSB_DAILY_LIMIT`, `ROUTE_DAILY_LIMIT`, `AVIATIONSTACK_MONTHLY_LIMIT`, `ADSB_API_BASE`, `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`, `OPENSKY_GLOBAL_TTL`, `ROUTE_API_BASE`, `AVIATIONSTACK_API_KEY`, `SCHEDULE_TTL`, `POLL_INTERVAL` (10s), `REGION_INTERVAL` (15s), `MAX_TRACKED` (10), `MAX_REGION_FLIGHTS` (2500), `LOST_AFTER` (300s), `DATA_DIR`, `FRONTEND_DIR`, `HTTP_TIMEOUT`, `USER_AGENT`. Add new settings to `Settings` and to `.env.example` together. Never commit real keys.

## HTTP API (`main.py`)

- `GET /api/health`
- `GET /api/usage` – per-provider used/limit/remaining/reset time, mode, effective poll interval, suggested map interval
- `POST /api/settings` `{mode?, poll_interval?}` – change refresh mode/interval (wakes the poll loop)
- `POST /api/refresh` – refresh tracked flights now in any mode (throttled to one per 5 s); the UI pairs it with `region?fresh=true` (bypasses caches; `max_age=` lets a client accept cached data)
- `GET /api/meta` – category labels + airline list (feeds the filter UI)
- `GET /api/flights/global` – sampled worldwide snapshot (map zoom < 5; needs OpenSky or demo)
- `GET /api/flight/{callsign|hex}/{id}` – full detail for any flight without tracking it (used when clicking a plane)
- `GET /api/flights/search?q=` – callsign / IATA flight no. / hex / airline
- `GET /api/flights/region?south=&west=&north=&east=` – flights in view. Both list endpoints take filters `cats=` (comma list), `airline=` (name/IATA/ICAO), `airport=` (IATA/ICAO of origin or destination), `airborne=`; filtering is server-side and applied before capping/sampling. Airport filtering needs routes, so unknown routes are looked up in the background (60 new callsigns/request, `routes_pending` in the response) and the UI re-polls until resolved.
- `GET/POST/DELETE /api/tracked[/{id}]` – manage tracked set; 409/400 when exceeding `MAX_TRACKED`
- `GET /api/tracked/{id}` – full detail incl. route, trail, derived metrics
- `WS /api/stream` (or SSE) – tracked-flight updates; region updates on request

## Conventions

- Async everywhere on the backend; share one `httpx.AsyncClient`; always set timeouts and `User-Agent`.
- Provider failures must never crash the app: log, fall through the chain, surface a `note`/status to the UI (e.g. "region data limited to 250 nm radius").
- Use `None` for unknown values, never 0 or "". The UI renders unknowns as "—".
- Longitude wrap: routes and bboxes crossing the antimeridian must be split/handled in both geo helpers and Leaflet drawing.
- Mind free-API rate limits: debounce region queries, cache, and coalesce polls for all tracked flights into as few upstream calls as possible.
- Match the existing code style: type-hinted, module docstrings, small pure helpers, `# noqa: BLE001` only where broad excepts are deliberate.

## Running

```bash
# once implemented
docker compose up --build          # http://localhost:8000
DEMO_MODE=true docker compose up   # offline/simulated traffic
```

Local dev without Docker: `pip install -r backend/requirements.txt && uvicorn backend.app.main:app --reload` (set `DATA_DIR=./data`, `FRONTEND_DIR=./frontend`).

Docker is expected to bind `0.0.0.0` (needed for port publishing in the sandbox; publish with `sbx ports <sandbox> --publish 8000:8000/tcp`).

## Testing / verification

- Unit-test `geo.py`, `airlines.py` mappings, `tracker.derive()`, and provider parsers (`parse_ac`, `parse_state`) with recorded JSON fixtures – no live network in tests.
- Use `DEMO_MODE` for end-to-end checks of tracking, region view, and ticker.
- Manual acceptance: track 10 flights (11th rejected); select one → route + details render; zoom to Phoenix, AZ → only flights within view listed; ticker shows logo, flight no., ETA, delay, total time, heading, speed.
