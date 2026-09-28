"""Great-circle math helpers."""
import math

EARTH_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def intermediate(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Point at fraction f along the great circle from 1 to 2."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = haversine_km(lat1, lon1, lat2, lon2) / EARTH_KM
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def cross_track_km(o_lat, o_lon, d_lat, d_lon, p_lat, p_lon) -> float:
    """Perpendicular distance (km) from point p to the great-circle path from o to d.

    Used to flag a flight whose live position doesn't lie anywhere near its looked-up route - a sign
    the route (matched only by callsign) belongs to a different flight than the one actually broadcasting
    that callsign right now (see the routes/aircraft caveat in providers/enrich.py and CLAUDE.md).
    """
    d13 = haversine_km(o_lat, o_lon, p_lat, p_lon) / EARTH_KM
    b13 = math.radians(bearing_deg(o_lat, o_lon, p_lat, p_lon))
    b12 = math.radians(bearing_deg(o_lat, o_lon, d_lat, d_lon))
    return abs(math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(b13 - b12)))) * EARTH_KM)


def cardinal(deg: float | None) -> str:
    if deg is None:
        return ""
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]
