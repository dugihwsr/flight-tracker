"""Flight enrichment: route (origin/destination), aircraft details, schedule & delays.

- Routes + aircraft: adsbdb.com (free, no key).
- Schedule/delays/gates: AviationStack (optional, needs AVIATIONSTACK_API_KEY).
All results are cached in-memory with TTLs to stay within free-tier limits.
"""
import logging
import time
from datetime import datetime, timezone

import httpx

from ..budget import BudgetExceeded, budget

log = logging.getLogger(__name__)


class TTLCache:
    def __init__(self, ttl: float, miss_ttl: float | None = None):
        self.ttl, self.miss_ttl = ttl, miss_ttl if miss_ttl is not None else ttl / 4
        self._d: dict[str, tuple[float, object]] = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit is None:
            return False, None
        exp, val = hit
        if time.time() > exp:
            del self._d[key]
            return False, None
        return True, val

    def put(self, key, val, ttl: float | None = None):
        self._d[key] = (time.time() + (ttl if ttl is not None else self.ttl if val is not None else self.miss_ttl), val)


def _airport(a: dict | None) -> dict | None:
    if not a:
        return None
    return {
        "iata": a.get("iata_code"), "icao": a.get("icao_code"), "name": a.get("name"),
        "city": a.get("municipality"), "country": a.get("country_name"),
        "lat": a.get("latitude"), "lon": a.get("longitude"),
    }


class Enricher:
    def __init__(self, client: httpx.AsyncClient, route_base: str, avstack_key: str,
                 avstack_base: str, schedule_ttl: int):
        self.client = client
        self.route_base = route_base
        self.avstack_key, self.avstack_base = avstack_key, avstack_base
        self.routes = TTLCache(6 * 3600, 1800)
        self.aircraft_cache = TTLCache(24 * 3600, 6 * 3600)
        self.schedules = TTLCache(schedule_ttl, schedule_ttl)

    @property
    def has_schedule(self) -> bool:
        return bool(self.avstack_key)

    async def route(self, callsign: str | None) -> dict | None:
        if not callsign:
            return None
        ok, val = self.routes.get(callsign)
        if ok:
            return val
        val = None
        try:
            r = await self.client.get(f"{self.route_base}/v0/callsign/{callsign}")
            if r.status_code == 200:
                fr = (r.json().get("response") or {}).get("flightroute") or {}
                al = fr.get("airline") or {}
                val = {
                    "callsign": fr.get("callsign_icao") or callsign,
                    "flight_iata": fr.get("callsign_iata"),
                    "airline": {"name": al.get("name"), "icao": al.get("icao"), "iata": al.get("iata"),
                                "country": al.get("country"), "radio": al.get("callsign")} if al else None,
                    "origin": _airport(fr.get("origin")),
                    "destination": _airport(fr.get("destination")),
                }
        except BudgetExceeded:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("route lookup %s failed: %s", callsign, e)
            return None  # don't cache transport errors
        self.routes.put(callsign, val)
        return val

    async def aircraft(self, hex_: str | None) -> dict | None:
        if not hex_:
            return None
        ok, val = self.aircraft_cache.get(hex_)
        if ok:
            return val
        val = None
        try:
            r = await self.client.get(f"{self.route_base}/v0/aircraft/{hex_}")
            if r.status_code == 200:
                a = (r.json().get("response") or {}).get("aircraft") or {}
                val = {
                    "type": a.get("type"), "icao_type": a.get("icao_type"),
                    "manufacturer": a.get("manufacturer"), "registration": a.get("registration"),
                    "owner": a.get("registered_owner"), "owner_country": a.get("registered_owner_country_name"),
                    "photo": a.get("url_photo_thumbnail") or a.get("url_photo"),
                    "photo_large": a.get("url_photo"),
                }
        except BudgetExceeded:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("aircraft lookup %s failed: %s", hex_, e)
            return None
        self.aircraft_cache.put(hex_, val)
        return val

    async def schedule(self, callsign: str | None, flight_iata: str | None) -> dict | None:
        if not self.avstack_key or not (callsign or flight_iata):
            return None
        key = flight_iata or callsign
        ok, val = self.schedules.get(key)
        if ok:
            return val
        # Stretch the refresh interval so the plan's remaining requests last until it resets.
        ttl = budget.adaptive("aviationstack", self.schedules.ttl, items=max(1, budget.items.get("aviationstack", 1)))
        params = {"access_key": self.avstack_key}
        params["flight_iata" if flight_iata else "flight_icao"] = flight_iata or callsign
        val = None
        try:
            r = await self.client.get(f"{self.avstack_base}/flights", params=params)
            j = r.json()
            if "error" in j:
                log.warning("aviationstack error: %s", j["error"])
                if (j["error"] or {}).get("code") in ("usage_limit_reached", "rate_limit_reached"):
                    budget.meters["aviationstack"].remote = 0.0  # plan exhausted: stop asking until it resets
            data = j.get("data") or []
            if data:
                today = datetime.now(timezone.utc).date().isoformat()
                data.sort(key=lambda d: (d.get("flight_status") != "active", d.get("flight_date") != today))
                val = _schedule(data[0])
        except BudgetExceeded:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("schedule lookup %s failed: %s", key, e)
            return None
        self.schedules.put(key, val, ttl)
        return val


def _leg(d: dict) -> dict:
    return {k: d.get(k) for k in ("airport", "iata", "icao", "terminal", "gate", "baggage", "delay",
                                  "scheduled", "estimated", "actual", "timezone")}


def _schedule(d: dict) -> dict:
    return {
        "status": d.get("flight_status"),
        "date": d.get("flight_date"),
        "departure": _leg(d.get("departure") or {}),
        "arrival": _leg(d.get("arrival") or {}),
        "airline": (d.get("airline") or {}).get("name"),
        "codeshared": ((d.get("flight") or {}).get("codeshared") or {}).get("flight_iata"),
        "source": "aviationstack",
    }
