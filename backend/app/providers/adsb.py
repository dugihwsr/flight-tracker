"""readsb v2 API provider (adsb.lol by default; airplanes.live / adsb.fi use the same shape).

Endpoints: /v2/callsign/{cs}, /v2/hex/{hex}, /v2/point/{lat}/{lon}/{radius_nm} (radius max 250).
"""
import asyncio
import logging
import math
import time

import httpx

from ..geo import haversine_km
from .base import PositionProvider, State

log = logging.getLogger(__name__)
MAX_RADIUS_NM = 250


def _num(v):
    return v if isinstance(v, (int, float)) else None


def parse_ac(ac: dict, now: float) -> State | None:
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        lp = ac.get("lastPosition") or {}
        lat, lon = lp.get("lat"), lp.get("lon")
    if lat is None or lon is None:
        return None
    alt = ac.get("alt_baro")
    on_ground = alt == "ground"
    return State(
        hex=str(ac.get("hex", "")).lower().lstrip("~"),
        callsign=(ac.get("flight") or "").strip() or None,
        lat=lat, lon=lon,
        alt_ft=0.0 if on_ground else _num(alt),
        geo_alt_ft=_num(ac.get("alt_geom")),
        gs_kt=_num(ac.get("gs")),
        track=_num(ac.get("track")) if ac.get("track") is not None else _num(ac.get("true_heading")),
        vrate_fpm=_num(ac.get("baro_rate")) if ac.get("baro_rate") is not None else _num(ac.get("geom_rate")),
        on_ground=on_ground,
        squawk=ac.get("squawk"),
        reg=ac.get("r"),
        type=ac.get("t"),
        category=ac.get("category"),
        ias_kt=_num(ac.get("ias")),
        tas_kt=_num(ac.get("tas")),
        mach=_num(ac.get("mach")),
        wind_dir=_num(ac.get("wd")),
        wind_kt=_num(ac.get("ws")),
        db_flags=ac.get("dbFlags") if isinstance(ac.get("dbFlags"), int) else None,
        ts=now - float(ac.get("seen_pos") or ac.get("seen") or 0),
        source="adsb",
    )


class AdsbProvider(PositionProvider):
    name = "adsb"

    def __init__(self, client: httpx.AsyncClient, base: str):
        self.client = client
        self.base = base
        self._sem = asyncio.Semaphore(2)  # public APIs rate-limit around 1 req/s

    async def _get(self, path: str) -> list[dict]:
        # Request pacing lives centrally in budget.py (Meter.min_interval), shared across every caller
        # (tracked polling, map browsing, route warm-up); this semaphore just caps concurrency in flight.
        async with self._sem:
            r = await self.client.get(f"{self.base}{path}")
            r.raise_for_status()
        return r.json().get("ac") or []

    async def by_callsigns(self, callsigns):
        out: dict[str, State] = {}
        now = time.time()

        async def one(cs):
            try:
                for ac in await self._get(f"/v2/callsign/{cs}"):
                    s = parse_ac(ac, now)
                    if s and (s.callsign or "").upper() == cs.upper():
                        # Prefer the freshest if several transponders claim the callsign.
                        if cs not in out or s.ts > out[cs].ts:
                            out[cs] = s
            except Exception as e:  # noqa: BLE001
                log.warning("adsb callsign %s failed: %s", cs, e)

        await asyncio.gather(*(one(c) for c in callsigns))
        return out

    async def by_hex(self, hexes):
        out: dict[str, State] = {}
        if not hexes:
            return out
        now = time.time()
        try:
            for ac in await self._get("/v2/hex/" + ",".join(hexes)):
                s = parse_ac(ac, now)
                if s:
                    out[s.hex] = s
        except Exception as e:  # noqa: BLE001
            log.warning("adsb hex lookup failed: %s", e)
        return out

    async def in_bbox(self, south, west, north, east):
        clat = (south + north) / 2
        if east < west:  # crosses antimeridian
            east += 360
        clon = ((west + east) / 2 + 180) % 360 - 180
        corner_km = max(haversine_km(clat, clon, la, lo) for la in (south, north) for lo in (west, east))
        radius = math.ceil(corner_km / 1.852)
        note = None
        if radius > MAX_RADIUS_NM:
            radius = MAX_RADIUS_NM
            note = f"Region larger than this source allows; showing {MAX_RADIUS_NM} nm around its centre."
        now = time.time()
        acs = await self._get(f"/v2/point/{clat:.4f}/{clon:.4f}/{radius}")
        states = [s for s in (parse_ac(a, now) for a in acs) if s]
        return states, note
