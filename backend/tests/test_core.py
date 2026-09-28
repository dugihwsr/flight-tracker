import asyncio

from backend.app.airlines import parse_query
from backend.app.geo import haversine_km, bearing_deg
from backend.app.providers.adsb import parse_ac
from backend.app.providers.opensky import parse_state
from backend.app.tracker import derive


def test_parse_iata_and_icao():
    assert parse_query("UA123")["callsign"] == "UAL123"
    assert parse_query("baw117")["iata"] == "BA117"
    assert parse_query("united")["kind"] == "airline"
    assert parse_query("a1b2c3")["kind"] == "hex"


def test_geo():
    assert abs(haversine_km(0, 0, 0, 1) - 111.2) < 0.5
    assert round(bearing_deg(0, 0, 1, 0)) == 0


def test_parse_adsb():
    s = parse_ac({"hex": "abc123", "flight": "UAL1 ", "lat": 1, "lon": 2, "alt_baro": "ground"}, 1000.0)
    assert s.callsign == "UAL1" and s.on_ground and s.alt_ft == 0
    assert parse_ac({"hex": "x"}, 0) is None


def test_parse_opensky():
    v = ["abc123", "DAL9 ", "United States", 1, 1, -100.0, 30.0, 10000.0, False, 200.0, 90.0, 0.0, None, 10100.0, "1200", False, 0, 4]
    s = parse_state(v)
    assert s.callsign == "DAL9" and round(s.alt_ft) == 32808 and round(s.gs_kt) == 389


def test_derive_estimates_total_time_without_schedule():
    state = {"lat": 40, "lon": -100, "gs_kt": 450, "track": 90, "vrate_fpm": 0, "on_ground": False}
    route = {"origin": {"lat": 34, "lon": -118}, "destination": {"lat": 40.6, "lon": -73.8}}
    d = derive(state, route, None)
    assert d["total_source"] == "estimate" and d["total_min"] > 200
    assert d["eta_source"] == "groundspeed" and d["delay_arr_min"] is None


def test_classify():
    from backend.app.classify import classify
    assert classify("UAL123") == "commercial"
    assert classify("FDX1234") == "cargo"
    assert classify("RCH405") == "military"
    assert classify("XYZ1", "ae1234") == "military"
    assert classify("ABC12", db_flags=1) == "military"
    assert classify("SAM28000") == "government"
    assert classify("N123AB") == "private"
    assert classify("LIFE1", category="A7") == "helicopter"
    assert classify(None) == "unknown"


# ---------------- API budgeting ----------------
import httpx
import pytest

from backend.app.budget import Budget, BudgetExceeded, Meter, RESERVE, purpose_var


def _budget(limit=100):
    b = Budget()
    b.meters = {"opensky": Meter("opensky", "OpenSky", "opensky-network.org", limit),
                "adsb": Meter("adsb", "ADS-B", "api.adsb.lol", None)}
    return b


def test_opensky_cost_by_area():
    b = _budget()
    m = b.meters["opensky"]
    u = lambda q: httpx.URL("https://opensky-network.org/api/states/all", params=q)  # noqa: E731
    assert b.cost(m, u({"extended": 1})) == 4
    assert b.cost(m, u({"lamin": 30, "lamax": 32, "lomin": -112, "lomax": -110})) == 1
    assert b.cost(m, u({"lamin": 30, "lamax": 40, "lomin": -120, "lomax": -100})) == 3
    assert b.cost(m, u([("icao24", "abc"), ("extended", 1)])) == 1


def test_exhausted_and_reserve():
    b = _budget(100)
    m = b.meters["opensky"]
    b.check(m, "region", 1)
    m.used = 100 - RESERVE * 100 + 1           # inside the reserve
    with pytest.raises(BudgetExceeded):
        b.check(m, "region", 1)
    b.check(m, "track", 1)                     # tracked flights may still use it
    m.used = 100
    with pytest.raises(BudgetExceeded):
        b.check(m, "track", 1)
    b.check(b.meters["adsb"], "region", 1)     # unlimited source is never refused


def test_backoff_after_429_and_provider_reported_remaining():
    import asyncio
    b = _budget()
    m = b.meters["opensky"]
    req = httpx.Request("GET", "https://opensky-network.org/api/states/all")
    asyncio.run(b.on_response(httpx.Response(200, headers={"x-rate-limit-remaining": "42"}, request=req)))
    assert m.remaining() == 42
    asyncio.run(b.on_response(httpx.Response(429, headers={"x-rate-limit-retry-after-seconds": "120"}, request=req)))
    with pytest.raises(BudgetExceeded):
        b.check(m, "track", 1)


def test_hook_meters_and_refuses_requests():
    import asyncio
    b = _budget(2)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                                     event_hooks={"request": [b.on_request], "response": [b.on_response]}) as c:
            await c.get("https://opensky-network.org/api/states/all", params={"icao24": "a"})
            await c.get("https://opensky-network.org/api/states/all", params={"icao24": "b"})
            with pytest.raises(BudgetExceeded):
                await c.get("https://opensky-network.org/api/states/all", params={"icao24": "c"})
            await c.get("https://api.adsb.lol/v2/hex/abc")  # unmetered host still passes
    asyncio.run(run())
    assert b.meters["opensky"].used == 2 and b.meters["adsb"].calls == 1


def test_adaptation_stretches_intervals():
    b = _budget(400)
    m = b.meters["opensky"]
    assert b.ttl_for("opensky", 60, cost=4) < b.ttl_for("opensky", 60, cost=4, items=10)
    m.used = 390
    assert b.saver_interval({"opensky": 2.0}, 10) > 3000          # ~10 credits left for the day
    assert b.saver_interval({"opensky": 0.0}, 10) == 10           # no cost, no stretching
    b.mode = "live"
    assert b.adaptive("opensky", 60, cost=4) == 60
    b.mode = "saver"
    assert b.adaptive("opensky", 60, cost=4) > 60


# ---------------- position-glitch filter ----------------
from backend.app.providers.base import State
from backend.app.tracker import TrackedFlight, _fix_flight_iata, effective_route


def _state(lat, lon, ts, gs=450):
    return State(hex="abc123", callsign="UAL1", lat=lat, lon=lon, gs_kt=gs, on_ground=False, ts=ts)


def test_rejects_impossible_jump_but_not_forever():
    fl = TrackedFlight("UAL1", "callsign")
    fl.update(_state(40.0, -100.0, 1000.0))
    assert fl.state["lat"] == 40.0

    fl.update(_state(10.0, 100.0, 1010.0))          # ~15,000 km in 10s: impossible
    assert fl.state["lat"] == 40.0 and fl._reject_count == 1

    fl.update(_state(10.0, 100.0, 1020.0))           # same bad fix again
    assert fl.state["lat"] == 40.0 and fl._reject_count == 2

    fl.update(_state(10.0, 100.0, 1030.0))           # third time: accept rather than lock up forever
    assert fl.state["lat"] == 10.0 and fl._reject_count == 0


def test_accepts_plausible_motion():
    fl = TrackedFlight("UAL1", "callsign")
    fl.update(_state(40.0, -100.0, 1000.0))
    fl.update(_state(40.1, -99.9, 1030.0))           # a few km in 30s: a normal position update
    assert fl.state["lat"] == 40.1 and fl._reject_count == 0


def test_accepts_big_jump_after_a_long_gap():
    fl = TrackedFlight("UAL1", "callsign")
    fl.update(_state(40.0, -100.0, 1000.0))
    fl.update(_state(45.0, -80.0, 1000.0 + 900))     # real distance, but after 15 minutes of signal loss
    assert fl.state["lat"] == 45.0


# ---------------- per-host request pacing ----------------
def test_min_interval_paces_requests():
    import asyncio

    import httpx

    from backend.app.budget import Budget, Meter

    b = Budget()
    b.meters = {"adsb": Meter("adsb", "ADS-B", "api.adsb.lol", None, min_interval=0.05)}

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport, event_hooks={"request": [b.on_request]}) as c:
            start = asyncio.get_event_loop().time()
            await asyncio.gather(*(c.get("https://api.adsb.lol/v2/hex/abc") for _ in range(3)))
            return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(run())
    assert elapsed >= 0.1                            # 3 calls, 0.05s apart, must take >= 2 gaps
    assert b.meters["adsb"].calls == 3


# ---------------- route-plausibility flag ----------------
from backend.app.geo import cross_track_km


def test_cross_track_zero_on_the_line():
    # Midpoint of Sydney -> Orange, sitting exactly on the great circle
    o, d = (-33.9461, 151.1772), (-33.3809, 149.1000)
    mid_lat, mid_lon = (o[0] + d[0]) / 2, (o[1] + d[1]) / 2
    assert cross_track_km(*o, *d, mid_lat, mid_lon) < 1.0


def test_derive_flags_a_route_that_does_not_match_the_position():
    # Real case: adsbdb lists SWA826 as DEN->RDU, but it was actually flying BUR->PHX - a position
    # anywhere over Arizona is ~600 km (~324 NM) off the DEN-RDU line, well past the 200 NM threshold.
    route = {"origin": {"lat": 39.8617, "lon": -104.6733}, "destination": {"lat": 35.8776, "lon": -78.7875}}
    state = {"lat": 34.5, "lon": -112.0, "gs_kt": 420, "track": 250, "vrate_fpm": 0, "on_ground": False}
    d = derive(state, route, None)
    assert d.get("route_suspect", 0) > 200 * 1.852


def test_derive_does_not_flag_a_position_within_200nm_of_the_line():
    route = {"origin": {"lat": 39.8617, "lon": -104.6733}, "destination": {"lat": 35.8776, "lon": -78.7875}}
    # A modest, realistic deviation from the direct DEN-RDU line (routing/weather) - well under 200 NM.
    state = {"lat": 38.0, "lon": -92.0, "gs_kt": 420, "track": 110, "vrate_fpm": 0, "on_ground": False}
    d = derive(state, route, None)
    assert "route_suspect" not in d


def test_derive_ignores_on_ground_deviation():
    # Taxiing/parked far from where the route line passes shouldn't warn while on the ground.
    route = {"origin": {"lat": 39.8617, "lon": -104.6733}, "destination": {"lat": 35.8776, "lon": -78.7875}}
    state = {"lat": 34.5, "lon": -112.0, "gs_kt": 15, "track": 250, "vrate_fpm": 0, "on_ground": True}
    d = derive(state, route, None)
    assert "route_suspect" not in d


# ---------------- schedule-vs-route mismatch (adsbdb's static route can just be wrong) ----------------
def test_effective_route_prefers_real_schedule_over_adsbdb():
    # Real case: adsbdb lists ENY3353 as BUF->ORD (confirmed live), but AviationStack's schedule for
    # today's flight 3353 says RNO->PHX (confirmed against FlightAware) - adsbdb's entry is simply wrong.
    route = {"flight_iata": "MQ3353", "origin": {"iata": "BUF", "icao": "KBUF", "lat": 42.9, "lon": -78.7},
             "destination": {"iata": "ORD", "icao": "KORD", "lat": 42.0, "lon": -87.9}}
    schedule = {"source": "aviationstack",
                "departure": {"iata": "RNO", "icao": "KRNO"}, "arrival": {"iata": "PHX", "icao": "KPHX"}}
    fixed = effective_route(route, schedule)
    assert fixed["origin"]["iata"] == "RNO" and fixed["destination"]["iata"] == "PHX"
    assert fixed["flight_iata"] == "MQ3353"                     # other route fields carry over
    assert fixed["corrected_from"] == {"origin": "BUF", "destination": "ORD"}

    d = derive({"lat": 39.5, "lon": -119.8, "gs_kt": 300, "track": 200, "vrate_fpm": 0, "on_ground": False},
               fixed, schedule)
    assert d["route_corrected"] == {"origin": "BUF", "destination": "ORD"}
    assert d["dist_total_km"] < 1000                            # measuring against RNO-PHX (~600km), not BUF-ORD


def test_effective_route_unchanged_when_schedule_agrees():
    route = {"origin": {"iata": "BUF", "lat": 42.9, "lon": -78.7}, "destination": {"iata": "ORD", "lat": 42.0, "lon": -87.9}}
    schedule = {"source": "aviationstack", "departure": {"iata": "BUF"}, "arrival": {"iata": "ORD"}}
    fixed = effective_route(route, schedule)
    assert "corrected_from" not in fixed
    assert derive({"lat": 42.0, "lon": -83.0, "gs_kt": 300, "track": 250, "vrate_fpm": 0, "on_ground": False},
                  fixed, schedule).get("route_corrected") is None


def test_effective_route_marks_unverified_without_a_real_schedule():
    # Real case: adsbdb genuinely has SWA826 as DEN->RDU (confirmed live); it's actually BUR->PHX. Without
    # a schedule to check that against, the app can't fix it - but it must say so, not show it as fact.
    route = {"origin": {"iata": "DEN", "lat": 39.86, "lon": -104.67}, "destination": {"iata": "RDU", "lat": 35.88, "lon": -78.79}}
    for schedule in (None, {"source": "demo", "departure": {"iata": "BUR"}, "arrival": {"iata": "PHX"}}):
        fixed = effective_route(route, schedule)
        assert fixed["origin"]["iata"] == "DEN" and fixed["unverified"] is True
        assert "corrected_from" not in fixed
        d = derive({"lat": 36, "lon": -100, "gs_kt": 400, "track": 90, "vrate_fpm": 0, "on_ground": False}, fixed, schedule)
        assert d["route_unverified"] is True and "route_corrected" not in d


def test_effective_route_marks_unverified_when_airport_code_unknown():
    route = {"origin": {"iata": "BUF", "lat": 42.9, "lon": -78.7}, "destination": {"iata": "ORD", "lat": 42.0, "lon": -87.9}}
    schedule = {"source": "aviationstack", "departure": {"iata": "ZZZ9"}, "arrival": {"iata": "PHX"}}
    fixed = effective_route(route, schedule)
    assert fixed["origin"]["iata"] == "BUF" and fixed["unverified"] is True


def test_effective_route_none_route_stays_none():
    assert effective_route(None, {"source": "aviationstack"}) is None


# ---------------- adsbdb's bare-digit flight_iata (missing airline IATA code) ----------------
def test_fix_flight_iata_reconstructs_from_our_own_airline_table():
    # Real case: adsbdb has no IATA code for Envoy ("ENY"), so /v0/callsign/ENY3780 returns
    # flight_iata "3780" - not a usable schedule query, and not a recognisable flight number either.
    route = {"flight_iata": "3780", "origin": {"iata": "BOS"}, "destination": {"iata": "ORD"}}
    _fix_flight_iata(route, "ENY3780")
    assert route["flight_iata"] == "MQ3780"          # Envoy's real IATA code, from airlines.py
    assert route["flight_iata_unreliable"] is True


def test_fix_flight_iata_leaves_a_normal_one_alone():
    route = {"flight_iata": "UA1386"}
    _fix_flight_iata(route, "UAL1386")
    assert route["flight_iata"] == "UA1386"
    assert "flight_iata_unreliable" not in route


def test_fix_flight_iata_drops_it_when_the_airline_is_unknown():
    route = {"flight_iata": "4321"}
    _fix_flight_iata(route, "QQQ4321")               # not in our airline table either
    assert route["flight_iata"] is None
    assert route["flight_iata_unreliable"] is True


def test_fix_flight_iata_noop_without_one():
    route = {"origin": {"iata": "BOS"}}
    _fix_flight_iata(route, "ENY3780")
    assert "flight_iata" not in route and "flight_iata_unreliable" not in route
