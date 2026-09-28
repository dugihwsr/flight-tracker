"""Deterministic simulated traffic for offline use, demos and development.

Flights fly great circles between real airports on a repeating cycle, so positions
advance in real time and the same callsign is always the same flight.
"""
import math
import random
import time

from ..airlines import BY_ICAO
from ..geo import bearing_deg, haversine_km, intermediate
from .base import PositionProvider, State

# iata, icao, name, city, country, lat, lon
AIRPORTS = [
    ("JFK", "KJFK", "John F. Kennedy Intl", "New York", "United States", 40.6413, -73.7781),
    ("LAX", "KLAX", "Los Angeles Intl", "Los Angeles", "United States", 33.9416, -118.4085),
    ("ORD", "KORD", "O'Hare Intl", "Chicago", "United States", 41.9742, -87.9073),
    ("ATL", "KATL", "Hartsfield-Jackson Atlanta Intl", "Atlanta", "United States", 33.6407, -84.4277),
    ("DFW", "KDFW", "Dallas/Fort Worth Intl", "Dallas", "United States", 32.8998, -97.0403),
    ("DEN", "KDEN", "Denver Intl", "Denver", "United States", 39.8561, -104.6737),
    ("SFO", "KSFO", "San Francisco Intl", "San Francisco", "United States", 37.6213, -122.3790),
    ("SEA", "KSEA", "Seattle-Tacoma Intl", "Seattle", "United States", 47.4502, -122.3088),
    ("MIA", "KMIA", "Miami Intl", "Miami", "United States", 25.7959, -80.2870),
    ("BOS", "KBOS", "Logan Intl", "Boston", "United States", 42.3656, -71.0096),
    ("LAS", "KLAS", "Harry Reid Intl", "Las Vegas", "United States", 36.0840, -115.1537),
    ("YYZ", "CYYZ", "Toronto Pearson Intl", "Toronto", "Canada", 43.6777, -79.6248),
    ("YVR", "CYVR", "Vancouver Intl", "Vancouver", "Canada", 49.1967, -123.1815),
    ("MEX", "MMMX", "Mexico City Intl", "Mexico City", "Mexico", 19.4361, -99.0719),
    ("GRU", "SBGR", "Sao Paulo/Guarulhos Intl", "Sao Paulo", "Brazil", -23.4356, -46.4731),
    ("LHR", "EGLL", "Heathrow", "London", "United Kingdom", 51.4700, -0.4543),
    ("DUB", "EIDW", "Dublin", "Dublin", "Ireland", 53.4264, -6.2499),
    ("CDG", "LFPG", "Charles de Gaulle", "Paris", "France", 49.0097, 2.5479),
    ("FRA", "EDDF", "Frankfurt am Main", "Frankfurt", "Germany", 50.0379, 8.5622),
    ("AMS", "EHAM", "Schiphol", "Amsterdam", "Netherlands", 52.3105, 4.7683),
    ("MAD", "LEMD", "Adolfo Suarez Madrid-Barajas", "Madrid", "Spain", 40.4983, -3.5676),
    ("ZRH", "LSZH", "Zurich", "Zurich", "Switzerland", 47.4582, 8.5555),
    ("IST", "LTFM", "Istanbul", "Istanbul", "Turkey", 41.2753, 28.7519),
    ("CAI", "HECA", "Cairo Intl", "Cairo", "Egypt", 30.1219, 31.4056),
    ("DXB", "OMDB", "Dubai Intl", "Dubai", "United Arab Emirates", 25.2532, 55.3657),
    ("DOH", "OTHH", "Hamad Intl", "Doha", "Qatar", 25.2731, 51.6081),
    ("DEL", "VIDP", "Indira Gandhi Intl", "Delhi", "India", 28.5562, 77.1000),
    ("SIN", "WSSS", "Changi", "Singapore", "Singapore", 1.3644, 103.9915),
    ("BKK", "VTBS", "Suvarnabhumi", "Bangkok", "Thailand", 13.6900, 100.7501),
    ("HKG", "VHHH", "Hong Kong Intl", "Hong Kong", "Hong Kong", 22.3080, 113.9185),
    ("PEK", "ZBAA", "Beijing Capital Intl", "Beijing", "China", 40.0799, 116.6031),
    ("ICN", "RKSI", "Incheon Intl", "Seoul", "South Korea", 37.4602, 126.4407),
    ("HND", "RJTT", "Haneda", "Tokyo", "Japan", 35.5494, 139.7798),
    ("SYD", "YSSY", "Kingsford Smith", "Sydney", "Australia", -33.9399, 151.1753),
    ("AKL", "NZAA", "Auckland", "Auckland", "New Zealand", -37.0082, 174.7850),
    ("JNB", "FAJS", "O.R. Tambo Intl", "Johannesburg", "South Africa", -26.1367, 28.2411),
    ("NBO", "HKJK", "Jomo Kenyatta Intl", "Nairobi", "Kenya", -1.3192, 36.9278),
]
AIRLINES = ["AAL", "UAL", "DAL", "SWA", "JBU", "ASA", "ACA", "BAW", "VIR", "AFR", "KLM", "DLH",
            "SWR", "IBE", "EIN", "THY", "UAE", "QTR", "ETD", "SIA", "CPA", "ANA", "JAL", "KAL",
            "QFA", "ANZ", "AIC", "SAA", "ETH", "AMX", "LAN", "TAP", "RYR", "EZY"]
TYPES = [("B738", "Boeing 737-800", "A3"), ("A320", "Airbus A320", "A3"), ("A21N", "Airbus A321neo", "A3"),
         ("B789", "Boeing 787-9", "A5"), ("A359", "Airbus A350-900", "A5"), ("B77W", "Boeing 777-300ER", "A5"),
         ("A388", "Airbus A380-800", "A5"), ("E75L", "Embraer E175", "A2")]
COUNT = 450


class _Sim:
    def __init__(self, i: int, rng: random.Random):
        self.airline = rng.choice(AIRLINES)
        self.number = rng.randint(1, 2999)
        self.callsign = f"{self.airline}{self.number}"
        self.hex = f"{rng.randint(0x100000, 0xEFFFFF):06x}"
        while True:
            a, b = rng.sample(AIRPORTS, 2)
            if 400 < haversine_km(a[5], a[6], b[5], b[6]) < 15000:
                break
        self.origin, self.dest = a, b
        self.dist_km = haversine_km(a[5], a[6], b[5], b[6])
        long_haul = self.dist_km > 4000
        t = rng.choice(TYPES[3:7] if long_haul else TYPES[:3] + TYPES[7:])
        self.type, self.type_name, self.category = t
        self.cruise_kt = rng.uniform(440, 510)
        self.cruise_ft = rng.choice([31000, 33000, 35000, 37000, 39000, 41000]) if long_haul \
            else rng.choice([24000, 28000, 32000, 34000, 36000])
        self.duration = self.dist_km / (self.cruise_kt * 1.852) * 3600 + 1200  # s, +taxi/climb slack
        self.ground = 1800  # turnaround time at each end
        self.phase = rng.uniform(0, self.duration + self.ground)
        self.reg = f"{rng.choice(['N', 'G-', 'D-', 'F-', 'JA', 'VH-', 'C-', 'A6-'])}{rng.randint(100, 999)}{chr(65 + i % 26)}"
        self.delay_dep = rng.choice([0, 0, 0, 5, 12, 25, 45, 90])
        self.delay_arr = max(0, self.delay_dep + rng.randint(-15, 10))

    def state(self, now: float) -> State:
        cycle = self.duration + self.ground
        t = (now + self.phase) % cycle
        o, d = self.origin, self.dest
        if t >= self.duration:  # sitting at destination
            return State(hex=self.hex, callsign=self.callsign, lat=d[5], lon=d[6], alt_ft=0, gs_kt=0,
                         track=bearing_deg(o[5], o[6], d[5], d[6]), vrate_fpm=0, on_ground=True,
                         squawk="1200", reg=self.reg, type=self.type, category=self.category,
                         ts=now, source="demo")
        f = t / self.duration
        lat, lon = intermediate(o[5], o[6], d[5], d[6], f)
        la2, lo2 = intermediate(o[5], o[6], d[5], d[6], min(1, f + 0.001))
        climb, desc = 0.08, 0.90
        if f < climb:
            alt, vr, gs = self.cruise_ft * f / climb, 2200, 180 + (self.cruise_kt - 180) * f / climb
        elif f > desc:
            k = (1 - f) / (1 - desc)
            alt, vr, gs = self.cruise_ft * k, -1800, 150 + (self.cruise_kt - 150) * k
        else:
            alt, vr, gs = self.cruise_ft, math.sin(now / 90 + self.number) * 60, self.cruise_kt
        wind = math.sin(lat / 10) * 40
        return State(hex=self.hex, callsign=self.callsign, lat=lat, lon=lon, alt_ft=round(alt / 25) * 25,
                     geo_alt_ft=round(alt + 150), gs_kt=gs + wind, track=bearing_deg(lat, lon, la2, lo2),
                     vrate_fpm=vr, on_ground=False, squawk=f"{self.number % 7777:04d}".replace("8", "1").replace("9", "2"),
                     reg=self.reg, type=self.type, category=self.category, tas_kt=gs,
                     ias_kt=gs * 0.62 if alt > 20000 else gs, mach=round(gs / 580, 2) if alt > 20000 else None,
                     wind_dir=(270 + wind) % 360, wind_kt=abs(wind), ts=now, source="demo")


def _ap(a) -> dict:
    return {"iata": a[0], "icao": a[1], "name": a[2], "city": a[3], "country": a[4], "lat": a[5], "lon": a[6]}


class DemoProvider(PositionProvider):
    name = "demo"

    def __init__(self):
        rng = random.Random(42)
        self.sims = [_Sim(i, rng) for i in range(COUNT)]
        self.by_cs = {s.callsign: s for s in self.sims}
        self.by_hx = {s.hex: s for s in self.sims}

    async def by_callsigns(self, callsigns):
        now = time.time()
        return {c: self.by_cs[c.upper()].state(now) for c in callsigns if c.upper() in self.by_cs}

    async def by_hex(self, hexes):
        now = time.time()
        return {h: self.by_hx[h].state(now) for h in hexes if h in self.by_hx}

    async def all_states(self):
        now = time.time()
        return [s.state(now) for s in self.sims]

    async def in_bbox(self, south, west, north, east):
        def inside(s):
            if not (south <= s.lat <= north):
                return False
            return west <= s.lon <= east if west <= east else (s.lon >= west or s.lon <= east)
        return [s for s in await self.all_states() if inside(s)], None

    # --- enrichment stand-ins (duck-typed to Enricher) ---
    has_schedule = True

    async def route(self, callsign):
        s = self.by_cs.get((callsign or "").upper())
        if not s:
            return None
        al = BY_ICAO.get(s.airline)
        return {
            "callsign": s.callsign,
            "flight_iata": f"{al['iata']}{s.number}" if al else None,
            "airline": {"name": al["name"], "icao": al["icao"], "iata": al["iata"], "country": al["country"],
                        "radio": None} if al else None,
            "origin": _ap(s.origin), "destination": _ap(s.dest),
        }

    async def aircraft(self, hex_):
        s = self.by_hx.get(hex_ or "")
        if not s:
            return None
        return {"type": s.type_name, "icao_type": s.type, "manufacturer": s.type_name.split()[0],
                "registration": s.reg, "owner": (BY_ICAO.get(s.airline) or {}).get("name"),
                "owner_country": None, "photo": None, "photo_large": None}

    async def schedule(self, callsign, flight_iata):
        s = self.by_cs.get((callsign or "").upper())
        if not s:
            return None
        now = time.time()
        cycle = s.duration + s.ground
        t = (now + s.phase) % cycle
        dep = now - t
        arr = dep + s.duration

        def iso(ts):
            return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))
        status = "landed" if t >= s.duration else "active"
        return {
            "status": status, "date": iso(dep)[:10], "airline": None, "codeshared": None, "source": "demo",
            "departure": {"airport": s.origin[2], "iata": s.origin[0], "icao": s.origin[1],
                          "terminal": str(1 + s.number % 5), "gate": f"{'ABCD'[s.number % 4]}{s.number % 40 + 1}",
                          "baggage": None, "delay": s.delay_dep or None, "timezone": "UTC",
                          "scheduled": iso(dep - s.delay_dep * 60), "estimated": iso(dep), "actual": iso(dep)},
            "arrival": {"airport": s.dest[2], "iata": s.dest[0], "icao": s.dest[1],
                        "terminal": str(1 + s.number % 3), "gate": f"{'EFGH'[s.number % 4]}{s.number % 30 + 1}",
                        "baggage": str(s.number % 12 + 1), "delay": s.delay_arr or None, "timezone": "UTC",
                        "scheduled": iso(arr - s.delay_arr * 60), "estimated": iso(arr),
                        "actual": iso(arr) if status == "landed" else None},
        }
