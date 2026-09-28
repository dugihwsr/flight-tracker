"""Local airport lookup: IATA/ICAO code -> name, city, country, coordinates.

Loaded once from `data/airports.json` (built by scripts/build_airports.py from OurAirports, public
domain) so airport metadata never depends on a network call or an external quota - only *which two
airports a flight connects* needs a live source (adsbdb/schedule), and that's frequently wrong; where
they are never is.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "data", "airports.json")


def _load() -> dict[str, dict]:
    try:
        with open(_PATH, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return {}
    by_code: dict[str, dict] = {}
    for a in rows:
        for code in (a.get("iata"), a.get("icao")):
            if code:
                by_code.setdefault(code, a)
    return by_code


_BY_CODE = _load()


def by_code(code: str | None) -> dict | None:
    """Airport dict ({iata, icao, name, city, country, lat, lon}) for an IATA or ICAO code, or None."""
    return _BY_CODE.get((code or "").strip().upper()) or None
