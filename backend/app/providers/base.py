"""Common position-state shape and provider interface.

All providers normalise to aviation units: feet, knots, feet/minute, degrees true.
"""
from dataclasses import dataclass, asdict, field
import time


@dataclass
class State:
    hex: str                      # ICAO 24-bit address, lowercase
    callsign: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_ft: float | None = None   # barometric altitude
    geo_alt_ft: float | None = None
    gs_kt: float | None = None    # ground speed
    track: float | None = None    # degrees true
    vrate_fpm: float | None = None
    on_ground: bool = False
    squawk: str | None = None
    country: str | None = None    # country of registration (OpenSky)
    reg: str | None = None
    type: str | None = None       # ICAO aircraft type, e.g. B738
    category: str | None = None
    ias_kt: float | None = None   # indicated airspeed, when the provider has it
    tas_kt: float | None = None
    mach: float | None = None
    wind_dir: float | None = None
    wind_kt: float | None = None
    db_flags: int | None = None   # readsb database flags (bit 0 = military)
    ts: float = field(default_factory=time.time)  # time of position
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PositionProvider:
    name = "base"

    async def by_callsigns(self, callsigns: list[str]) -> dict[str, State]:
        return {}

    async def by_hex(self, hexes: list[str]) -> dict[str, State]:
        return {}

    async def in_bbox(self, south: float, west: float, north: float, east: float) -> tuple[list[State], str | None]:
        """Return (states, note). Note explains any coverage limitation."""
        return [], None

    async def all_states(self) -> list[State] | None:
        """Global snapshot, if the provider supports it (used for airline browsing)."""
        return None


M_TO_FT = 3.28084
MS_TO_KT = 1.943844
MS_TO_FPM = 196.8504
