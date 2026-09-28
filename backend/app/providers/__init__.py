"""Provider chain: query providers in order, letting later ones fill gaps."""
import logging

import httpx

from ..config import settings
from .adsb import AdsbProvider
from .base import PositionProvider, State
from .demo import DemoProvider
from .enrich import Enricher
from .opensky import OpenSkyProvider

log = logging.getLogger(__name__)


class Chain(PositionProvider):
    def __init__(self, providers: list[PositionProvider]):
        self.providers = providers
        self.name = "+".join(p.name for p in providers)

    async def by_callsigns(self, callsigns):
        out: dict[str, State] = {}
        for p in self.providers:
            missing = [c for c in callsigns if c not in out]
            if not missing:
                break
            try:
                out.update(await p.by_callsigns(missing))
            except Exception as e:  # noqa: BLE001
                log.warning("%s.by_callsigns failed: %s", p.name, e)
        return out

    async def by_hex(self, hexes):
        out: dict[str, State] = {}
        for p in self.providers:
            missing = [h for h in hexes if h not in out]
            if not missing:
                break
            try:
                out.update(await p.by_hex(missing))
            except Exception as e:  # noqa: BLE001
                log.warning("%s.by_hex failed: %s", p.name, e)
        return out

    async def in_bbox(self, south, west, north, east):
        errors = []
        partial = None  # a source that answered but only covers part of the area
        for p in self.providers:
            try:
                states, note = await p.in_bbox(south, west, north, east)
                if note and p is not self.providers[-1]:
                    partial = partial or (states, note, p.name)
                    continue  # try a source with full coverage; keep this as the fallback
                return states, note, p.name
            except Exception as e:  # noqa: BLE001
                log.warning("%s.in_bbox failed: %s", p.name, e)
                errors.append(f"{p.name}: {e}")
        if partial:
            return partial
        raise RuntimeError("; ".join(errors) or "no region provider configured")

    async def all_states(self):
        for p in self.providers:
            try:
                s = await p.all_states()
                if s is not None:
                    return s
            except Exception as e:  # noqa: BLE001
                log.warning("%s.all_states failed: %s", p.name, e)
        return None


def build(client: httpx.AsyncClient):
    """Returns (track_chain, region_chain, global_chain, enricher)."""
    if settings.demo_mode:
        demo = DemoProvider()
        c = Chain([demo])
        return c, c, c, demo

    made: dict[str, PositionProvider] = {
        "adsb": AdsbProvider(client, settings.adsb_api_base),
        "opensky": OpenSkyProvider(client, settings.opensky_client_id, settings.opensky_client_secret,
                                   settings.opensky_global_ttl),
    }

    def chain(names):
        return Chain([made[n] for n in names if n in made])

    enricher = Enricher(client, settings.route_api_base, settings.aviationstack_api_key,
                        settings.aviationstack_base, settings.schedule_ttl)
    # Only OpenSky can return a global snapshot (used for "all flights of airline X").
    return chain(settings.track_providers), chain(settings.region_providers), chain(["opensky"]), enricher
