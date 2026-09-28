"""FastAPI entrypoint: JSON API + WebSocket stream + static frontend."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .airlines import AIRLINES, BY_IATA, airline_for_callsign, parse_query
from .budget import budget, fresh_var, purpose_var
from .classify import CATEGORIES, classify
from .config import settings
from .providers import build
from .runtime import runtime
from .providers.base import State
from .tracker import Tracker, TrackedFlight, effective_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("flighttracker")

GLOBAL_LIMIT = 4000
REGION_TTL = 8.0


ROUTE_RETRY = 1800          # re-ask for routes that came back empty after 30 min
WARM_PER_REQUEST = 15       # new callsigns looked up per filtered request (adsbdb politeness)
ROUTE_CATS = {"commercial", "cargo"}


def _ap_code(ap: dict | None) -> str | None:
    return (ap or {}).get("iata") or (ap or {}).get("icao")


def route_of(cs: str | None) -> dict | None:
    """Route from what we already know (route book or the enricher cache); never hits the network."""
    if not cs:
        return None
    tf = app.state.tracker.flights.get(cs)
    if tf and tf.route:
        return effective_route(tf.route, tf.schedule)
    hit = app.state.route_book.get(cs)
    if hit and (hit[1] is not None or time.time() - hit[0] < ROUTE_RETRY):
        return hit[1]
    cache = getattr(app.state.enricher, "routes", None)
    if cache is not None:
        ok, val = cache.get(cs)
        if ok:
            return val
    return None


def route_known(cs: str) -> bool:
    tf = app.state.tracker.flights.get(cs)
    if tf and tf.route:
        return True
    hit = app.state.route_book.get(cs)
    if hit and (hit[1] is not None or time.time() - hit[0] < ROUTE_RETRY):
        return True
    cache = getattr(app.state.enricher, "routes", None)
    return bool(cache and cache.get(cs)[0])


async def _warm_route(cs: str):
    purpose_var.set("enrich")
    async with app.state.route_sem:
        try:
            r = await app.state.enricher.route(cs)
        except Exception:  # noqa: BLE001
            r = None
    book = app.state.route_book
    book[cs] = (time.time(), r)
    if len(book) > 6000:
        for k in sorted(book, key=lambda k: book[k][0])[:2000]:
            del book[k]
    app.state.route_pending.discard(cs)


def compact(s: State, cat: str | None = None) -> dict:
    """Small per-flight record for map layers and search results."""
    al = airline_for_callsign(s.callsign)
    r = route_of(s.callsign)
    return {
        "hex": s.hex, "callsign": s.callsign, "lat": round(s.lat, 4), "lon": round(s.lon, 4),
        "track": s.track, "alt_ft": s.alt_ft, "gs_kt": s.gs_kt, "type": s.type,
        "on_ground": s.on_ground, "airline_icao": al["icao"] if al else None,
        "airline_iata": al["iata"] if al else None,
        "cat": cat or classify(s.callsign, s.hex, s.category, s.db_flags),
        "origin": _ap_code((r or {}).get("origin")), "dest": _ap_code((r or {}).get("destination")),
    }


def airline_codes(text: str) -> set[str]:
    """ICAO prefixes matching user text: an IATA/ICAO code or part of an airline name."""
    t = text.strip().upper()
    if not t:
        return set()
    out = set()
    if t in BY_IATA:
        out.add(BY_IATA[t]["icao"])
    if len(t) == 3:
        out.add(t)
    if len(t) >= 3:
        out |= {a["icao"] for a in AIRLINES if t in a["name"].upper()}
    return out


class Filters:
    def __init__(self, cats: str = "", airline: str = "", airport: str = "", airborne: bool = False):
        self.cats = {c for c in cats.lower().split(",") if c in CATEGORIES}
        self.airline_text = airline.strip()
        self.airlines = airline_codes(airline)
        self.airport = airport.strip().upper()
        self.airborne = airborne

    @property
    def active(self) -> bool:
        return bool(self.cats or self.airline_text or self.airport or self.airborne)

    def apply(self, states: list[State]) -> tuple[list[tuple[State, str]], int]:
        """Returns ([(state, category)], routes_pending)."""
        out, missing = [], []
        for s in states:
            if self.airborne and s.on_ground:
                continue
            cat = classify(s.callsign, s.hex, s.category, s.db_flags)
            if self.cats and cat not in self.cats:
                continue
            cs = (s.callsign or "").upper()
            if self.airline_text and not (cs[:3] in self.airlines and cs[3:4].isdigit()):
                continue
            if self.airport:
                if cat not in ROUTE_CATS or not cs:
                    continue
                if not route_known(cs):
                    missing.append(cs)
                    continue
                r = route_of(cs) or {}
                if self.airport not in {(r.get("origin") or {}).get("iata"), (r.get("origin") or {}).get("icao"),
                                        (r.get("destination") or {}).get("iata"),
                                        (r.get("destination") or {}).get("icao")}:
                    continue
            out.append((s, cat))
        pending = 0
        if missing:
            todo = [c for c in dict.fromkeys(missing) if c not in app.state.route_pending]
            for c in todo[:WARM_PER_REQUEST]:
                app.state.route_pending.add(c)
                asyncio.create_task(_warm_route(c))
            pending = len(set(missing))
        return out, pending


@asynccontextmanager
async def lifespan(app: FastAPI):
    budget.configure()
    runtime.load()
    client = httpx.AsyncClient(timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent},
                               follow_redirects=True,
                               event_hooks={"request": [budget.on_request], "response": [budget.on_response]})
    track, region, glob, enricher = build(client)
    tracker = Tracker(track, enricher)
    tracker.load()
    app.state.client, app.state.tracker = client, tracker
    app.state.region, app.state.glob, app.state.enricher = region, glob, enricher
    app.state.region_cache = {}
    app.state.sockets = set()
    app.state.route_book = {}
    app.state.route_pending = set()
    app.state.route_sem = asyncio.Semaphore(4)

    async def broadcast(snap: dict):
        for ws in list(app.state.sockets):
            try:
                await ws.send_json(snap)
            except Exception:  # noqa: BLE001
                app.state.sockets.discard(ws)

    tracker.listeners.append(broadcast)
    task = asyncio.create_task(tracker.poll_forever())
    log.info("started (demo=%s, track=%s, region=%s)", settings.demo_mode, track.name, region.name)
    yield
    task.cancel()
    budget.save(force=True)
    await client.aclose()


app = FastAPI(title="Flight Tracker", lifespan=lifespan)


@app.middleware("http")
async def no_store(request, call_next):
    """Every response - API data and the frontend's own HTML/JS/CSS alike - must never be served stale
    from a cache. /api/* is live data (e.g. re-selecting a flight shortly after it changed); the frontend
    files change across updates to this app, and a browser silently serving a cached app.js after an
    update is a real, confusing failure mode (a fix looks like it "didn't apply"). This app is small and
    self-hosted, so the bandwidth cost of never caching is negligible next to that confusion."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health():
    t: Tracker = app.state.tracker
    return {"ok": True, "demo": settings.demo_mode, "tracked": len(t.flights), "max_tracked": settings.max_tracked,
            "last_poll": t.last_poll, "error": t.last_error, "mode": runtime.mode,
            "has_schedule": bool(getattr(app.state.enricher, "has_schedule", False)),
            "providers": {"track": t.positions.name, "region": app.state.region.name}}


# ---------------- discovery ----------------

@app.get("/api/meta")
async def meta():
    return {"categories": CATEGORIES,
            "airlines": sorted(({"iata": a["iata"], "icao": a["icao"], "name": a["name"]} for a in AIRLINES),
                               key=lambda a: a["name"])}


@app.get("/api/flights/region")
async def flights_region(south: float = Query(..., ge=-90, le=90), north: float = Query(..., ge=-90, le=90),
                         west: float = Query(..., ge=-180, le=180), east: float = Query(..., ge=-180, le=180),
                         cats: str = "", airline: str = "", airport: str = "", airborne: bool = False,
                         max_age: float = Query(REGION_TTL, ge=0, le=3600), fresh: bool = False):
    if south >= north:
        raise HTTPException(400, "south must be less than north")
    purpose_var.set("region")
    fresh_var.set(fresh)
    key = tuple(round(v, 1) for v in (south, west, north, east))
    cache = app.state.region_cache
    hit = cache.get(key)
    if hit and not fresh and time.time() - hit[0] < max_age:
        _, states, note, source = hit
    else:
        try:
            states, note, source = await app.state.region.in_bbox(south, west, north, east)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"No region data source available: {e}")
        cache[key] = (time.time(), states, note, source)
        if len(cache) > 64:
            for k in sorted(cache, key=lambda k: cache[k][0])[:32]:
                del cache[k]
    f = Filters(cats, airline, airport, airborne)
    matched, pending = f.apply(states)
    total = len(matched)
    if total > settings.max_region_flights:
        matched = sorted(matched, key=lambda m: m[0].alt_ft or 0, reverse=True)[:settings.max_region_flights]
        note = (note + " " if note else "") + f"Showing {settings.max_region_flights} of {total} flights."
    return {"ts": cache[key][0],
            "source": source, "note": note, "total": total, "in_view": len(states),
            "filtered": f.active, "routes_pending": pending,
            "flights": [compact(s, c) for s, c in matched]}


@app.get("/api/flights/global")
async def flights_global(cats: str = "", airline: str = "", airport: str = "", airborne: bool = True,
                         fresh: bool = False):
    """Worldwide snapshot for the zoomed-out map (needs a provider with a global feed)."""
    purpose_var.set("region")
    fresh_var.set(fresh)
    states = await app.state.glob.all_states()
    if states is None:
        raise HTTPException(502, "Worldwide view unavailable right now (data plan limit or source down); "
                                 "zoom in to browse by region.")
    data_ts = max((getattr(p, "_global_at", 0) for p in app.state.glob.providers), default=0) or time.time()
    f = Filters(cats, airline, airport, airborne)
    matched, pending = f.apply(states)
    step = max(1, len(matched) // GLOBAL_LIMIT)
    return {"ts": data_ts, "total": len(matched), "in_view": len(states), "filtered": f.active,
            "routes_pending": pending, "flights": [compact(s, c) for s, c in matched[::step]]}


@app.get("/api/flights/search")
async def flights_search(q: str = Query(..., min_length=2)):
    parsed = parse_query(q)
    kind = parsed["kind"]
    out: dict = {"query": q, "parsed": parsed, "results": []}
    if kind == "unknown":
        return out
    if kind == "airline":
        states = await app.state.glob.all_states()
        if states is None:
            out["note"] = "Browsing by airline needs the global feed (OpenSky) which is unavailable right now."
            return out
        icao = parsed["airline"]["icao"]
        found = [s for s in states if (s.callsign or "").upper().startswith(icao) and not s.on_ground]
        found.sort(key=lambda s: s.callsign or "")
        out["total"] = len(found)
        out["results"] = [compact(s) for s in found[:60]]
        return out
    chain = app.state.tracker.positions
    if kind == "hex":
        found = (await chain.by_hex([parsed["hex"]])).values()
    else:
        cs = parsed["callsign"]
        found = (await chain.by_callsigns([cs])).values()
    out["results"] = [compact(s) for s in found]
    if kind == "callsign" and not out["results"]:
        out["note"] = f"{parsed['callsign']} is not airborne (or not visible to the free feeds) right now."
    return out


# ---------------- tracking ----------------

def _norm(id: str, kind: str) -> str:
    return id.strip().upper() if kind == "callsign" else id.strip().lower()


@app.get("/api/tracked")
async def tracked_list():
    return app.state.tracker.snapshot()


@app.post("/api/tracked")
async def tracked_add(body: dict):
    kind = body.get("kind", "callsign")
    ident = (body.get("id") or "").strip()
    if kind not in ("callsign", "hex") or not ident:
        raise HTTPException(400, "Provide id and kind ('callsign' or 'hex')")
    try:
        fl = await app.state.tracker.add(ident, kind, body.get("label"))
    except OverflowError as e:
        raise HTTPException(409, str(e))
    return fl.to_dict()


@app.delete("/api/tracked/{id}")
async def tracked_remove(id: str):
    if not await app.state.tracker.remove(id):
        raise HTTPException(404, "Not tracked")
    return {"ok": True}


@app.get("/api/tracked/{id}")
async def tracked_get(id: str):
    flights = app.state.tracker.flights
    fl = flights.get(id) or flights.get(id.upper()) or flights.get(id.lower())
    if not fl:
        raise HTTPException(404, "Not tracked")
    return fl.to_dict()


@app.get("/api/flight/{kind}/{id}")
async def flight_preview(kind: str, id: str):
    """Full detail for any flight (tracked or not) without adding it to the tracked set."""
    if kind not in ("callsign", "hex"):
        raise HTTPException(400, "kind must be 'callsign' or 'hex'")
    t: Tracker = app.state.tracker
    ident = _norm(id, kind)
    if ident in t.flights:
        return t.flights[ident].to_dict()
    fl = TrackedFlight(ident, kind)
    await t._poll([fl])  # noqa: SLF001
    if not fl.state:
        raise HTTPException(404, "Flight not currently visible")
    return fl.to_dict()


# ---------------- data budget & refresh controls ----------------

def _usage() -> dict:
    t: Tracker = app.state.tracker
    floor = float(settings.region_interval)
    return {"mode": runtime.mode, "poll_interval": runtime.poll_interval,
            "effective_poll_interval": t.interval(), "last_poll": t.last_poll, "tracked": len(t.flights),
            "region_interval": budget.region_interval(floor) if runtime.mode == "saver" else floor,
            "providers": budget.usage(), "ts": time.time()}


@app.get("/api/usage")
async def usage():
    return _usage()


@app.post("/api/settings")
async def update_settings(body: dict):
    mode, poll = body.get("mode"), body.get("poll_interval")
    if mode is not None and mode not in ("live", "saver", "manual"):
        raise HTTPException(400, "mode must be live, saver or manual")
    if poll is not None and not (isinstance(poll, (int, float)) and 5 <= poll <= 3600):
        raise HTTPException(400, "poll_interval must be 5-3600 seconds")
    runtime.update(mode, poll)
    app.state.tracker.wake()
    return _usage()


@app.post("/api/refresh")
async def refresh():
    """Refresh tracked flights now (any mode). Rate-limited to one real refresh every few seconds."""
    ran = await app.state.tracker.refresh_now()
    return {"refreshed": ran, "usage": _usage()}


@app.websocket("/api/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    app.state.sockets.add(ws)
    try:
        await ws.send_json(app.state.tracker.snapshot())
        while True:
            await ws.receive_text()  # keepalive / ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        app.state.sockets.discard(ws)


# ---------------- frontend ----------------

_front = os.path.abspath(settings.frontend_dir)
if os.path.isdir(_front):
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(_front, "index.html"))

    app.mount("/", StaticFiles(directory=_front), name="static")
