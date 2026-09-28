"""OpenSky Network REST provider.

Anonymous access works but is heavily rate-limited; set OPENSKY_CLIENT_ID/SECRET
(OAuth2 client credentials from your OpenSky account page) for higher limits.
"""
import logging
import time

import httpx

from ..budget import budget, fresh_var
from .base import M_TO_FT, MS_TO_FPM, MS_TO_KT, PositionProvider, State

log = logging.getLogger(__name__)
API = "https://opensky-network.org/api"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CATEGORIES = {2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7", 9: "B1",
              10: "B2", 11: "B3", 12: "B4", 14: "B6", 15: "B7", 16: "C1", 17: "C2", 18: "C3"}


def parse_state(v: list) -> State | None:
    if v[5] is None or v[6] is None:
        return None
    return State(
        hex=v[0].lower(),
        callsign=(v[1] or "").strip() or None,
        country=v[2],
        lon=v[5], lat=v[6],
        alt_ft=v[7] * M_TO_FT if v[7] is not None else None,
        on_ground=bool(v[8]),
        gs_kt=v[9] * MS_TO_KT if v[9] is not None else None,
        track=v[10],
        vrate_fpm=v[11] * MS_TO_FPM if v[11] is not None else None,
        geo_alt_ft=v[13] * M_TO_FT if v[13] is not None else None,
        squawk=v[14],
        category=CATEGORIES.get(v[17]) if len(v) > 17 else None,
        ts=v[3] or v[4] or time.time(),
        source="opensky",
    )


class OpenSkyProvider(PositionProvider):
    name = "opensky"

    def __init__(self, client: httpx.AsyncClient, client_id: str, client_secret: str, global_ttl: int):
        self.client = client
        self.client_id, self.client_secret = client_id, client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self._global: list[State] | None = None
        self._global_at = 0.0
        self.global_ttl = global_ttl

    async def _headers(self) -> dict:
        if not self.client_id:
            return {}
        if not self._token or time.time() > self._token_exp - 60:
            r = await self.client.post(TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            })
            r.raise_for_status()
            j = r.json()
            self._token = j["access_token"]
            self._token_exp = time.time() + j.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._token}"}

    async def _states(self, params) -> list[State]:
        r = await self.client.get(f"{API}/states/all", params=params, headers=await self._headers())
        if r.status_code == 429:
            raise RuntimeError("OpenSky rate limit reached")
        r.raise_for_status()
        return [s for s in (parse_state(v) for v in (r.json().get("states") or [])) if s]

    async def all_states(self):
        ttl = budget.adaptive("opensky", self.global_ttl, cost=4, purpose="region")
        if self._global is None or fresh_var.get() or time.time() - self._global_at > ttl:
            self._global = await self._states({"extended": 1})
            self._global_at = time.time()
        return self._global

    async def by_callsigns(self, callsigns):
        # OpenSky has no callsign filter, so use the cached global snapshot.
        want = {c.upper() for c in callsigns}
        if not want:
            return {}
        try:
            states = await self.all_states()
        except Exception as e:  # noqa: BLE001
            log.warning("opensky global fetch failed: %s", e)
            return {}
        return {s.callsign.upper(): s for s in states if s.callsign and s.callsign.upper() in want}

    async def by_hex(self, hexes):
        if not hexes:
            return {}
        try:
            states = await self._states([("icao24", h) for h in hexes] + [("extended", 1)])
        except Exception as e:  # noqa: BLE001
            log.warning("opensky hex lookup failed: %s", e)
            return {}
        return {s.hex: s for s in states}

    async def in_bbox(self, south, west, north, east):
        if east < west:  # antimeridian: split into two requests
            a = await self._states({"lamin": south, "lomin": west, "lamax": north, "lomax": 180, "extended": 1})
            b = await self._states({"lamin": south, "lomin": -180, "lamax": north, "lomax": east, "extended": 1})
            return a + b, None
        return await self._states({"lamin": south, "lomin": west, "lamax": north, "lomax": east, "extended": 1}), None
