"""API call budgeting for free-tier data sources.

Every outbound HTTP request made through the shared httpx client is metered here (via client event
hooks, so providers need no changes). A meter knows a provider's allowance (calls per day or month),
counts usage, honours the provider's own "remaining" header when it sends one, and backs off after a
429. When a provider is out of budget the request is refused with `BudgetExceeded`, which the provider
chains treat like any other failure and fall through to the next source.

Tracked-flight polling has priority: map browsing ("region" purpose) may not eat into the last
RESERVE share of a metered allowance.

The defaults below are approximations of the providers' published free tiers; override them with the
*_LIMIT env vars if your plan differs. Counters persist in DATA_DIR/usage.json.
"""
import asyncio
import contextvars
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import settings

log = logging.getLogger(__name__)

purpose_var: contextvars.ContextVar[str] = contextvars.ContextVar("purpose", default="track")  # track|region|enrich
fresh_var: contextvars.ContextVar[bool] = contextvars.ContextVar("fresh", default=False)      # bypass caches
RESERVE = 0.35          # share of a limited allowance kept for tracked flights
SAFETY = 0.75           # plan to use at most this much of what is left (leaves more headroom for retries)


class BudgetExceeded(Exception):
    """Raised instead of sending a request the plan cannot afford."""


def _key(period: str, now: float) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc)
    return dt.strftime("%Y-%m-%d") if period == "day" else dt.strftime("%Y-%m")


def _next_reset(period: str, now: float) -> float:
    dt = datetime.fromtimestamp(now, timezone.utc)
    if period == "day":
        nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = (dt.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


class Meter:
    def __init__(self, id: str, label: str, host: str, limit: int | None, period: str = "day", note: str = "",
                 min_interval: float = 0.0):
        self.id, self.label, self.host = id, label, host
        self.limit = limit or None          # None = no published limit
        self.period, self.note = period, note
        self.min_interval = min_interval    # seconds between requests, regardless of any daily/monthly limit
        self.key = _key(period, time.time())
        self.used = 0.0
        self.calls = 0
        self.by_purpose: dict[str, float] = {}
        self.region_calls = 0
        self.remote: float | None = None    # provider-reported remaining credits
        self.blocked_until = 0.0
        self.last_request_ts = 0.0
        self.lock = asyncio.Lock()          # serialises pacing across concurrent requests to this host

    def roll(self, now: float):
        k = _key(self.period, now)
        if k != self.key:
            self.key, self.used, self.calls, self.by_purpose, self.region_calls = k, 0.0, 0, {}, 0
            self.remote, self.blocked_until = None, 0.0

    def remaining(self) -> float | None:
        if self.remote is not None:
            return self.remote
        return None if self.limit is None else max(0.0, self.limit - self.used)

    def usable(self, purpose: str) -> float | None:
        """Credits this purpose may still spend (map browsing must leave the reserve alone)."""
        rem = self.remaining()
        if rem is None:
            return None
        if purpose == "region" and self.limit:
            rem -= RESERVE * self.limit
        return max(0.0, rem)

    def resets_at(self, now: float | None = None) -> float:
        return _next_reset(self.period, now or time.time())

    def to_dict(self, now: float) -> dict:
        self.roll(now)
        return {"id": self.id, "label": self.label, "limit": self.limit, "used": round(self.used, 1),
                "remaining": None if self.remaining() is None else round(self.remaining(), 1),
                "period": self.period, "resets_at": self.resets_at(now), "calls": self.calls,
                "blocked_until": self.blocked_until if self.blocked_until > now else None,
                "by_purpose": {k: round(v, 1) for k, v in self.by_purpose.items()},
                "reported_by_provider": self.remote is not None, "note": self.note}


class Budget:
    def __init__(self):
        self.meters: dict[str, Meter] = {}
        self.items: dict[str, int] = {}       # provider id -> how many things we refresh through it (tracked flights)
        self.mode = "saver"                   # live | saver | manual
        self._dirty, self._saved, self._path = False, 0.0, None

    # ---- setup ----
    def configure(self, s=settings):
        host = lambda u: urlparse(u).hostname or ""  # noqa: E731
        os_limit = int(os.getenv("OPENSKY_DAILY_LIMIT", "0")) or (4000 if s.opensky_client_id else 400)
        self.meters = {m.id: m for m in [
            Meter("adsb", "ADS-B feed", host(s.adsb_api_base),
                  int(os.getenv("ADSB_DAILY_LIMIT", "0")) or None, note="No published limit; keep it polite.",
                  min_interval=float(os.getenv("ADSB_MIN_INTERVAL", "1.0"))),
            Meter("opensky", "OpenSky Network", "opensky-network.org", os_limit,
                  note="Credits: 1-4 per request depending on area; a worldwide snapshot costs 4."),
            Meter("route", "Routes & aircraft (adsbdb)", host(s.route_api_base),
                  int(os.getenv("ROUTE_DAILY_LIMIT", "0")) or None, note="No published limit.",
                  min_interval=float(os.getenv("ROUTE_MIN_INTERVAL", "0.5"))),
            Meter("aviationstack", "AviationStack (schedules & delays)", host(s.aviationstack_base),
                  int(os.getenv("AVIATIONSTACK_MONTHLY_LIMIT", "100")) if s.aviationstack_api_key else None,
                  period="month", note="Free plan is roughly 100 requests per month."),
        ]}
        self._path = os.path.join(s.data_dir, "usage.json")
        self.load()

    def load(self):
        try:
            with open(self._path) as f:
                data = json.load(f)
            for id, d in data.items():
                m = self.meters.get(id)
                if m and d.get("key") == m.key:
                    m.used, m.calls, m.by_purpose = d.get("used", 0.0), d.get("calls", 0), d.get("by_purpose", {})
                    m.region_calls = d.get("region_calls", 0)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("could not load usage: %s", e)

    def save(self, force: bool = False):
        if not self._path or not (self._dirty or force):
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({m.id: {"key": m.key, "used": m.used, "calls": m.calls, "by_purpose": m.by_purpose,
                                  "region_calls": m.region_calls} for m in self.meters.values()}, f)
            os.replace(tmp, self._path)
            self._dirty, self._saved = False, time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("could not save usage: %s", e)

    # ---- metering ----
    def meter_for(self, url) -> Meter | None:
        h = getattr(url, "host", None) or urlparse(str(url)).hostname
        return next((m for m in self.meters.values() if m.host and m.host == h), None)

    @staticmethod
    def cost(m: Meter, url) -> float:
        """OpenSky charges by bounding-box area; everything else is 1 per request."""
        if m.id != "opensky" or not str(getattr(url, "path", url)).endswith("/states/all"):
            return 1.0
        p = dict(getattr(url, "params", {}) or {})
        if "icao24" in p:
            return 1.0
        try:
            area = (float(p["lamax"]) - float(p["lamin"])) * (float(p["lomax"]) - float(p["lomin"]))
        except (KeyError, ValueError):
            return 4.0
        return 1.0 if area <= 25 else 2.0 if area <= 100 else 3.0 if area <= 400 else 4.0

    def check(self, m: Meter, purpose: str, cost: float, now: float | None = None):
        now = now or time.time()
        m.roll(now)
        if m.blocked_until > now:
            raise BudgetExceeded(f"{m.label} asked us to back off for {int(m.blocked_until - now)}s")
        rem = m.remaining()
        if rem is None:
            return
        if rem < cost:
            raise BudgetExceeded(f"{m.label} allowance used up until {time.strftime('%H:%M UTC', time.gmtime(m.resets_at(now)))}")
        if purpose == "region" and m.limit and rem - cost < RESERVE * m.limit:
            raise BudgetExceeded(f"{m.label}: remaining calls are reserved for tracked flights")

    def record(self, m: Meter, purpose: str, cost: float):
        m.used += cost
        m.calls += 1
        m.by_purpose[purpose] = m.by_purpose.get(purpose, 0.0) + cost
        if purpose == "region":
            m.region_calls += 1
        if m.remote is not None:
            m.remote = max(0.0, m.remote - cost)
        self._dirty = True
        if time.time() - self._saved > 10:
            self.save()

    async def on_request(self, request):
        m = self.meter_for(request.url)
        if not m:
            return
        purpose = purpose_var.get()
        cost = self.cost(m, request.url)
        self.check(m, purpose, cost)
        if m.min_interval:
            # A hard floor on request rate for hosts with no published quota (e.g. adsb.lol): the daily/monthly
            # accounting above can't protect them, so every caller (tracked polling, map browsing, route
            # warm-up) is serialised here to stay under the pace the docs for these free services ask for.
            async with m.lock:
                wait = m.min_interval - (time.time() - m.last_request_ts)
                if wait > 0:
                    await asyncio.sleep(wait)
                m.last_request_ts = time.time()
        self.record(m, purpose, cost)

    async def on_response(self, response):
        m = self.meter_for(response.request.url)
        if not m:
            return
        h = response.headers
        rem = h.get("x-rate-limit-remaining")
        if rem is not None:
            try:
                m.remote = float(rem)
            except ValueError:
                pass
        if response.status_code == 429:
            wait = h.get("x-rate-limit-retry-after-seconds") or h.get("retry-after") or "60"
            try:
                m.blocked_until = time.time() + min(float(wait), 86400)
            except ValueError:
                m.blocked_until = time.time() + 60
            if m.id == "opensky":
                m.remote = 0.0

    # ---- adaptation ----
    def ttl_for(self, id: str, floor: float, cost: float = 1.0, items: int = 1, purpose: str = "track") -> float:
        """Seconds between refreshes of each item so the remaining budget lasts until it resets."""
        m = self.meters.get(id)
        if not m:
            return floor
        now = time.time()
        m.roll(now)
        usable = m.usable(purpose)
        if usable is None:
            return floor
        t_left = max(60.0, m.resets_at(now) - now)
        if usable <= 0:
            return max(floor, t_left)
        return max(floor, t_left * items * cost / (SAFETY * usable))

    def adaptive(self, id: str, floor: float, **kw) -> float:
        """`ttl_for` in data-saver mode; the plain floor otherwise."""
        return self.ttl_for(id, floor, **kw) if self.mode == "saver" else floor

    def saver_interval(self, cycle_cost: dict[str, float], floor: float) -> float:
        """Poll interval that lets tracked-flight polling last until each metered allowance resets."""
        best = floor
        for id, c in cycle_cost.items():
            m = self.meters.get(id)
            rem = m.remaining() if m else None
            if not m or rem is None or c <= 0 or rem <= 0:
                continue
            t_left = max(60.0, m.resets_at() - time.time())
            best = max(best, c * t_left / (SAFETY * rem))
        return min(best, 3600.0)

    def limited_used(self) -> dict[str, float]:
        return {m.id: m.used for m in self.meters.values() if m.limit is not None or m.remote is not None}

    def region_interval(self, floor: float) -> float:
        """Suggested map auto-refresh (seconds) for the first metered source in the region chain."""
        for id in settings.region_providers:
            m = self.meters.get(id)
            if m and m.limit:
                if m.usable("region") == 0:
                    continue  # exhausted for browsing; the chain falls through to the next source
                avg = (m.by_purpose.get("region", 0.0) / m.region_calls) if m.region_calls else 2.0
                return min(3600.0, self.ttl_for(id, floor, cost=avg, purpose="region"))
            if m:
                return floor
        return floor

    def usage(self) -> list[dict]:
        now = time.time()
        return [m.to_dict(now) for m in self.meters.values()]


budget = Budget()
