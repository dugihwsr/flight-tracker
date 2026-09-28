# Flight Tracker

A self-hosted flight tracker that runs in one Docker container: a live map, a full-screen departures
board, and per-flight detail — all built on free flight-data APIs, no paid subscription required.

- **Track up to 10 flights** at once, with position, route, speed, altitude, and delay.
- **Global search** by callsign, flight number, airline, or ICAO24 hex.
- **Regional view** — pan or zoom the map to any area (e.g. Phoenix, AZ) to see the flights currently
  overhead, with filters for airline, airport, and aircraft category (commercial, cargo, military,
  government, private, helicopter).
- **Flight detail** — a great-circle route from origin to destination, live position and trail, and a
  card with altitude, speed, heading, vertical rate, progress, ETA, and delay.
- **Ticker / departures board** — a full-screen board of your tracked flights, styled after a real
  airport display, good for a second screen or a wall-mounted monitor.
- **Data-conscious by default** — refresh rates automatically stretch to fit a free API plan's daily or
  monthly limits, with a usage panel and manual override.
- **Route sanity-checking** — free route lookups are matched by callsign only and are wrong more often
  than you'd expect (confirmed against FlightAware for several real flights during development). Where
  possible this app corrects the route using a real schedule provider instead, and otherwise says
  plainly when a route hasn't been verified rather than showing a guess as fact.

## Quick start

Requires [Docker](https://www.docker.com/) and Docker Compose.

```bash
git clone https://github.com/dugihwsr/flight-tracker.git
cd flight-tracker
docker compose up --build
```

Open **http://localhost:8000**. No API keys are required — by default it uses [adsb.lol](https://adsb.lol)
and [adsbdb.com](https://api.adsbdb.com), both free and keyless.

To try it with simulated traffic instead of live data (useful offline, or to see 10 tracked flights
without waiting for real ones):

```bash
DEMO_MODE=true docker compose up --build
```

## Configuration

Copy `.env.example` to `.env` to customize anything; every setting is optional; all of the API sources
have a free tier and none require a card. The two worth adding for a noticeably better experience:

| Variable | What it unlocks |
|---|---|
| `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` | A free [OpenSky Network](https://opensky-network.org) account raises your position-data limit from ~400 to ~4,000 requests/day, and enables the worldwide zoomed-out view. |
| `AVIATIONSTACK_API_KEY` | A free [AviationStack](https://aviationstack.com) key (100 requests/month) adds real scheduled arrival times and delay status, and lets the app correct routes that free lookups get wrong. |

See `.env.example` for the full list, including per-provider rate limits, refresh intervals, and the
maximum number of tracked flights (`MAX_TRACKED`, default 10).

## How it stays inside free-tier limits

This app is built to run entirely on free API plans, which have real daily/monthly caps. By default it
runs in **data saver** mode: it tracks how many requests each provider allows, stretches its own refresh
rate to make that allowance last until it resets, and gives your tracked flights priority over map
browsing when a plan is running low. The **⚙ Data** panel in the app shows live usage per provider and
lets you switch to a faster ("live") or fully manual refresh mode instead.

## Project structure

```
backend/app/    FastAPI backend - providers (adsb.lol, OpenSky, adsbdb, AviationStack), the tracked-
                flight manager, API-budget tracking, and the HTTP/WebSocket API
frontend/       No-build-step HTML/CSS/JS frontend (Leaflet map, ticker, flight page)
scripts/        One-off data-refresh scripts (airline list, airport database, airline logos)
backend/tests/  Unit tests (no live network calls)
```

`CLAUDE.md` has the full architecture writeup, including the reasoning behind some of the less obvious
decisions (why routes get cross-checked against a schedule, why request pacing is centralized, etc.).

## Running without Docker

```bash
pip install -r backend/requirements.txt
DATA_DIR=./data FRONTEND_DIR=./frontend uvicorn backend.app.main:app --reload
```

## Testing

```bash
pip install -r backend/requirements-dev.txt
pytest
```

## Limitations

- Free ADS-B feeds don't include scheduled times, so arrival estimates are calculated from ground speed
  and distance remaining unless a schedule provider (AviationStack) is configured.
- Free route lookups are matched by callsign alone and can be outright wrong, not just stale - this is a
  limitation of the free data source, not something a client-side fix can fully correct. The app flags
  this when it can detect it (mismatched schedule, or a position far from the claimed route) and is
  upfront when it can't check at all.
- Aircraft category (military/government/private/etc.) is a best-effort heuristic from callsign
  patterns and public ICAO24 address ranges, not authoritative data.

## License

[MIT](LICENSE)
