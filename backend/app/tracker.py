"""Tracked-flight manager: polls positions for up to MAX_TRACKED flights, keeps trails,
enriches with route/aircraft/schedule, persists to disk and notifies listeners."""
import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Awaitable, Callable

from .airports import by_code
from .budget import budget
from .runtime import runtime
from .airlines import airline_for_callsign
from .classify import classify
from .config import settings
from .geo import bearing_deg, cardinal, cross_track_km, haversine_km
from .providers.base import State

log = logging.getLogger(__name__)
EMERGENCY_SQUAWKS = {"7500": "Hijack", "7600": "Radio failure", "7700": "General emergency"}
TRAIL_MAX = 4000
MAX_PLAUSIBLE_KT = 1200   # implied speed above this between two fixes is a decode glitch, not real flight
NM_TO_KM = 1.852
ROUTE_SUSPECT_NM = 200    # cross-track deviation from the route line beyond this flags the route as suspect
MAX_GLITCH_REJECTS = 2    # give up rejecting after this many in a row (the aircraft may really have moved)


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fix_flight_iata(route: dict, cs: str | None) -> None:
    """adsbdb sometimes returns a bare number for `flight_iata` (e.g. "3780") because its own airline
    table has no IATA code for the operator - confirmed live for Envoy/"ENY", whose real IATA is "MQ"
    per our own table (airlines.py). A bare number isn't a real flight designator, so this rebuilds one
    for *display* (a big improvement over "3780" alone), but flags it `flight_iata_unreliable`: regional
    flights are frequently ticketed under a different carrier's code than the physical operator's, so
    querying a schedule with a guessed prefix risks a wrong match rather than no match. Mutates in place.
    """
    raw = route.get("flight_iata")
    if not raw or not raw[0].isdigit():
        return
    al = airline_for_callsign(cs)
    route["flight_iata"] = f"{al['iata']}{raw}" if al and al.get("iata") else None
    route["flight_iata_unreliable"] = True


def effective_route(route: dict | None, schedule: dict | None) -> dict | None:
    """The route to actually display/measure against: a real schedule's departure/arrival over adsbdb's guess.

    adsbdb matches routes by callsign alone, and that's frequently just wrong for mainline flight numbers
    that get reassigned to different city pairs (confirmed live: several United flight numbers pointed to
    completely different cities than they were actually flying - not a stale/reused-callsign edge case,
    a routine occurrence). A real schedule provider (AviationStack) looks the flight up by flight number
    for today specifically, so when one is available it wins; airport coordinates for its departure/arrival
    codes come from our own local database (`airports.py`), not from adsbdb, since adsbdb has no endpoint
    for looking up an airport by code on its own. Falls back to adsbdb's route when there's no schedule, or
    its airports aren't in our local database.
    """
    if not route:
        return route
    dep = schedule.get("departure") or {} if schedule and schedule.get("source") == "aviationstack" else {}
    arr = schedule.get("arrival") or {} if schedule and schedule.get("source") == "aviationstack" else {}
    o, d = by_code(dep.get("iata") or dep.get("icao")), by_code(arr.get("iata") or arr.get("icao"))
    if not o or not d:
        # No real schedule to check against (no AviationStack key, no data for this flight/date, or a
        # lookup that failed) - adsbdb's route is unconfirmed, not necessarily wrong. Say so rather than
        # silently showing it with the same confidence as a checked one.
        out = dict(route)
        out["unverified"] = True
        return out
    out = dict(route)
    out["origin"], out["destination"] = o, d
    codes = lambda a: {c for c in (a.get("iata"), a.get("icao")) if c}  # noqa: E731
    was_o, was_d = (route or {}).get("origin") or {}, (route or {}).get("destination") or {}
    corrected = {}
    if codes(was_o) and not (codes(was_o) & codes(o)):
        corrected["origin"] = was_o.get("iata") or was_o.get("icao")
    if codes(was_d) and not (codes(was_d) & codes(d)):
        corrected["destination"] = was_d.get("iata") or was_d.get("icao")
    if corrected:
        out["corrected_from"] = corrected
    return out


def derive(state: dict | None, route: dict | None, schedule: dict | None) -> dict:
    """Computed, display-ready metrics for a flight card. `route` should already be `effective_route()`'d."""
    out: dict = {}
    if not state:
        return out
    lat, lon, gs = state.get("lat"), state.get("lon"), state.get("gs_kt")
    out["heading_cardinal"] = cardinal(state.get("track"))
    vr = state.get("vrate_fpm") or 0
    if state.get("on_ground"):
        out["phase"] = "On ground"
    elif vr > 400:
        out["phase"] = "Climbing"
    elif vr < -400:
        out["phase"] = "Descending"
    else:
        out["phase"] = "Cruise"
    sq = state.get("squawk")
    if sq in EMERGENCY_SQUAWKS:
        out["emergency"] = f"Squawk {sq}: {EMERGENCY_SQUAWKS[sq]}"

    o = (route or {}).get("origin") or {}
    d = (route or {}).get("destination") or {}
    dep = (schedule or {}).get("departure") or {}
    arr = (schedule or {}).get("arrival") or {}
    if route and route.get("corrected_from"):
        out["route_corrected"] = route["corrected_from"]
    elif route and route.get("unverified"):
        out["route_unverified"] = True
    if lat is not None and o.get("lat") is not None and d.get("lat") is not None:
        flown = haversine_km(o["lat"], o["lon"], lat, lon)
        remaining = haversine_km(lat, lon, d["lat"], d["lon"])
        out["dist_total_km"] = round(haversine_km(o["lat"], o["lon"], d["lat"], d["lon"]))
        out["dist_flown_km"] = round(flown)
        out["dist_remaining_km"] = round(remaining)
        out["progress"] = round(flown / (flown + remaining), 4) if flown + remaining else None
        # A position far off the origin-destination line usually means the *route* is wrong, not the
        # position: adsbdb matches routes by callsign alone, and a reused/stale callsign (a real aircraft
        # that took off again under an old flight number - common for regional turboprops flying several
        # sectors a day) will show a perfectly good live position next to a route it isn't actually flying.
        # 200 NM is well beyond normal deviation from a direct line (holding, weather, SID/STAR routing).
        xtk = cross_track_km(o["lat"], o["lon"], d["lat"], d["lon"], lat, lon)
        if not state.get("on_ground") and xtk > ROUTE_SUSPECT_NM * NM_TO_KM:
            out["route_suspect"] = round(xtk)
        brg = bearing_deg(lat, lon, d["lat"], d["lon"])
        out["bearing_to_dest"] = round(brg)
        if state.get("track") is not None:
            off = (state["track"] - brg + 540) % 360 - 180
            out["off_course_deg"] = round(off)
        if gs and gs > 50 and not state.get("on_ground"):
            out["eta_ts"] = time.time() + remaining / (gs * 1.852) * 3600
            out["eta_source"] = "groundspeed"

    sched_eta = _parse_ts(arr.get("estimated")) or _parse_ts(arr.get("scheduled"))
    # AviationStack labels local airport times as +00:00, so only trust it for demo/UTC data.
    if sched_eta and (schedule or {}).get("source") == "demo":
        out["eta_ts"], out["eta_source"] = sched_eta, "schedule"
    if out.get("eta_ts"):
        out["time_left_min"] = max(0, round((out["eta_ts"] - time.time()) / 60))
    sched_dep = _parse_ts(dep.get("scheduled"))
    sched_arr = _parse_ts(arr.get("scheduled"))
    if sched_dep and sched_arr and sched_arr > sched_dep and (schedule or {}).get("source") == "demo":
        out["total_min"], out["total_source"] = round((sched_arr - sched_dep) / 60), "schedule"
    elif out.get("dist_total_km"):
        # No schedule: distance at a typical airliner block speed (~780 km/h) plus 25 min taxi/climb.
        out["total_min"], out["total_source"] = round(out["dist_total_km"] / 780 * 60 + 25), "estimate"
    out["delay_dep_min"] = dep.get("delay")
    out["delay_arr_min"] = arr.get("delay")
    return out


class TrackedFlight:
    def __init__(self, id: str, kind: str, label: str | None = None, hex: str | None = None,
                 added_at: float | None = None, trail=None, last_seen=None, state=None):
        self.id, self.kind = id, kind          # kind: "callsign" or "hex"
        self.label = label or id
        self.hex = hex
        self.added_at = added_at or time.time()
        self.state: dict | None = state
        self.trail: deque = deque(trail or [], maxlen=TRAIL_MAX)
        self.last_seen: float | None = last_seen
        self.route: dict | None = None
        self.aircraft: dict | None = None
        self.schedule: dict | None = None
        self.was_airborne = bool(state and not state.get("on_ground"))
        self._reject_count = 0

    @property
    def callsign(self) -> str | None:
        return self.id if self.kind == "callsign" else (self.state or {}).get("callsign")

    def update(self, s: State):
        prev = self.state
        if prev and prev.get("lat") is not None:
            dt = s.ts - (self.last_seen or s.ts)
            if 0 < dt < 600:
                implied_kt = haversine_km(prev["lat"], prev["lon"], s.lat, s.lon) / dt * 1943.844  # km/s -> kt
                if implied_kt > MAX_PLAUSIBLE_KT and self._reject_count < MAX_GLITCH_REJECTS:
                    # A single bad ADS-B position report (CPR decode glitch) can look like the aircraft
                    # teleported; ignore it and wait for the next fix rather than drawing a route to nowhere.
                    self._reject_count += 1
                    log.info("%s: ignoring implausible position (%.0f kt implied over %.0fs)", self.id, implied_kt, dt)
                    return
        self._reject_count = 0
        d = s.to_dict()
        if self.hex and s.hex != self.hex:
            self.aircraft = None  # different airframe flying this callsign now
        self.hex = s.hex
        self.state = d
        self.last_seen = s.ts
        if not s.on_ground:
            self.was_airborne = True
        last = self.trail[-1] if self.trail else None
        if (last is None or s.ts - last[3] > 60
                or haversine_km(last[0], last[1], s.lat, s.lon) > 0.5):
            self.trail.append([round(s.lat, 5), round(s.lon, 5), round(s.alt_ft or 0), round(s.ts)])

    def status(self) -> str:
        if not self.state:
            return "searching"
        route = effective_route(self.route, self.schedule)
        derived = derive(self.state, route, self.schedule)
        near_dest = (derived.get("dist_remaining_km") or 1e9) < 40
        stale = time.time() - (self.last_seen or 0) > settings.lost_after
        if stale:
            return "landed" if near_dest and self.was_airborne else "signal_lost"
        if self.state.get("on_ground"):
            return "landed" if near_dest and self.was_airborne else "on_ground"
        return "airborne"

    def to_dict(self, trail: bool = True) -> dict:
        cs = self.callsign
        route = effective_route(self.route, self.schedule) or {}
        airline = route.get("airline") or airline_for_callsign(cs)
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "callsign": cs, "hex": self.hex,
            "flight_iata": route.get("flight_iata"), "airline": airline,
            "category": classify(cs, self.hex, (self.state or {}).get("category"), (self.state or {}).get("db_flags")),
            "status": self.status(), "added_at": self.added_at, "last_seen": self.last_seen,
            "state": self.state, "route": route or None, "aircraft": self.aircraft, "schedule": self.schedule,
            "derived": derive(self.state, route, self.schedule),
            "trail": list(self.trail) if trail else None,
        }

    def persist(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "hex": self.hex,
                "added_at": self.added_at, "last_seen": self.last_seen, "state": self.state,
                "trail": list(self.trail)}


class Tracker:
    def __init__(self, positions, enricher):
        self.positions = positions
        self.enricher = enricher
        self.flights: dict[str, TrackedFlight] = {}
        self.listeners: list[Callable[[dict], Awaitable[None]]] = []
        self._lock = asyncio.Lock()
        self._path = os.path.join(settings.data_dir, "tracked.json")
        self.last_poll: float | None = None
        self.last_error: str | None = None
        self.effective_interval: float | None = None
        self._cycle_cost: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._last_refresh = 0.0

    # ---- persistence ----
    def load(self):
        try:
            with open(self._path) as f:
                for d in json.load(f):
                    self.flights[d["id"]] = TrackedFlight(**d)
            log.info("restored %d tracked flights", len(self.flights))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("could not load tracked flights: %s", e)

    def save(self):
        try:
            os.makedirs(settings.data_dir, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump([fl.persist() for fl in self.flights.values()], f)
            os.replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001
            log.warning("could not save tracked flights: %s", e)

    # ---- mutations ----
    async def add(self, id: str, kind: str, label: str | None = None) -> TrackedFlight:
        id = id.upper() if kind == "callsign" else id.lower()
        async with self._lock:
            if id in self.flights:
                return self.flights[id]
            if len(self.flights) >= settings.max_tracked:
                raise OverflowError(f"Already tracking the maximum of {settings.max_tracked} flights")
            fl = TrackedFlight(id, kind, label)
            self.flights[id] = fl
        await self._poll([fl])
        self.save()
        await self._notify()
        return fl

    async def remove(self, id: str) -> bool:
        async with self._lock:
            fl = self.flights.pop(id, None) or self.flights.pop(id.upper(), None) or self.flights.pop(id.lower(), None)
        if fl:
            self.save()
            await self._notify()
        return fl is not None

    def snapshot(self) -> dict:
        return {"type": "tracked", "ts": time.time(), "last_poll": self.last_poll, "error": self.last_error,
                "flights": [f.to_dict() for f in sorted(self.flights.values(), key=lambda f: f.added_at)]}

    # ---- polling ----
    async def _poll(self, flights: list[TrackedFlight]):
        by_cs = [f.id for f in flights if f.kind == "callsign"]
        by_hx = [f.id for f in flights if f.kind == "hex"]
        cs_states, hx_states = await asyncio.gather(
            self.positions.by_callsigns(by_cs) if by_cs else _empty(),
            self.positions.by_hex(by_hx) if by_hx else _empty(),
        )
        # Fallback: a callsign flight whose airframe we know may be findable by hex.
        missing = [f for f in flights if f.kind == "callsign" and f.id not in cs_states and f.hex]
        if missing:
            extra = await self.positions.by_hex([f.hex for f in missing])
            for f in missing:
                s = extra.get(f.hex)
                if s and (s.callsign or "").upper() == f.id:
                    cs_states[f.id] = s
        for f in flights:
            s = cs_states.get(f.id) if f.kind == "callsign" else hx_states.get(f.id)
            if s:
                f.update(s)
        await asyncio.gather(*(self._enrich(f) for f in flights))

    async def _enrich(self, f: TrackedFlight):
        cs = f.callsign
        try:
            if f.route is None and cs:
                f.route = await self.enricher.route(cs)
                if f.route:
                    _fix_flight_iata(f.route, cs)
                if f.route and f.route.get("flight_iata") and f.label == f.id:
                    f.label = f.route["flight_iata"]
            if f.aircraft is None and f.hex:
                f.aircraft = await self.enricher.aircraft(f.hex)
            if cs and self.enricher.has_schedule:
                route = f.route or {}
                # A reconstructed (unreliable) flight_iata is fine to *display* but risky to query a
                # schedule with - regional carriers are often ticketed under a different marketing
                # carrier's code, so a wrong guess could pull back a completely different flight's
                # schedule. Fall back to the (unambiguous) ICAO callsign instead in that case.
                query_iata = None if route.get("flight_iata_unreliable") else route.get("flight_iata")
                # enricher caches with its own TTL, so this is cheap on most polls
                f.schedule = await self.enricher.schedule(cs, query_iata) or f.schedule
        except Exception as e:  # noqa: BLE001
            log.warning("enrich %s failed: %s", f.id, e)

    async def refresh(self):
        """One poll cycle of every tracked flight; feeds the per-cycle API cost used by data-saver mode."""
        budget.items["aviationstack"] = len(self.flights)
        before = budget.limited_used()
        try:
            await self._poll(list(self.flights.values()))
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            log.exception("poll failed")
            self.last_error = str(e)
        spent = {k: v - before.get(k, 0.0) for k, v in budget.limited_used().items()}
        # smooth so one-off schedule lookups don't whipsaw the interval
        self._cycle_cost = {k: 0.5 * self._cycle_cost.get(k, spent[k]) + 0.5 * spent[k] for k in spent}
        self.last_poll = self._last_refresh = time.time()
        self.save()
        await self._notify()

    async def refresh_now(self, min_gap: float = 5.0) -> bool:
        """User-triggered refresh (works in every mode). Returns False if one just ran."""
        if not self.flights or time.time() - self._last_refresh < min_gap:
            return False
        await self.refresh()
        return True

    def interval(self) -> float | None:
        """Seconds until the next background poll, or None when only manual refresh is wanted."""
        if runtime.mode == "manual":
            return None
        base = float(runtime.poll_interval)
        if runtime.mode == "saver":
            base = budget.saver_interval(self._cycle_cost, base)
        self.effective_interval = base
        return base

    def wake(self):
        self._wake.set()

    async def poll_forever(self):
        while True:
            self._wake.clear()
            iv = self.interval()
            if self.flights and iv is not None:
                await self.refresh()
                iv = self.interval()
            if iv is None:
                wait = 3600.0
            elif not self.flights:
                wait = iv
            else:
                wait = max(1.0, iv - (time.time() - self._last_refresh))
            try:  # a settings change or manual refresh wakes the loop early
                await asyncio.wait_for(self._wake.wait(), timeout=min(wait, 3600.0))
            except asyncio.TimeoutError:
                pass

    async def _notify(self):
        snap = self.snapshot()
        for fn in list(self.listeners):
            try:
                await fn(snap)
            except Exception:  # noqa: BLE001
                pass


async def _empty():
    return {}
