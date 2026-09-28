"""Airline lookup: IATA (2-char) <-> ICAO (3-char, used as the ADS-B callsign prefix).

Flights broadcast their ICAO callsign (e.g. "BAW123"), while tickets show the IATA
flight number ("BA123"), so manual entry needs this mapping. Extend freely.
"""
import json
import os
import re

# iata, icao, name, country
_AIRLINES = [
    ("AA", "AAL", "American Airlines", "US"), ("UA", "UAL", "United Airlines", "US"),
    ("DL", "DAL", "Delta Air Lines", "US"), ("WN", "SWA", "Southwest Airlines", "US"),
    ("B6", "JBU", "JetBlue", "US"), ("AS", "ASA", "Alaska Airlines", "US"),
    ("NK", "NKS", "Spirit Airlines", "US"), ("F9", "FFT", "Frontier Airlines", "US"),
    ("G4", "AAY", "Allegiant Air", "US"), ("HA", "HAL", "Hawaiian Airlines", "US"),
    ("SY", "SCX", "Sun Country Airlines", "US"), ("MX", "MXY", "Breeze Airways", "US"),
    ("OO", "SKW", "SkyWest Airlines", "US"), ("YX", "RPA", "Republic Airways", "US"),
    ("9E", "EDV", "Endeavor Air", "US"), ("MQ", "ENY", "Envoy Air", "US"),
    ("OH", "JIA", "PSA Airlines", "US"), ("YV", "ASH", "Mesa Airlines", "US"),
    ("QX", "QXE", "Horizon Air", "US"), ("PT", "PDT", "Piedmont Airlines", "US"),
    ("5X", "UPS", "UPS Airlines", "US"), ("FX", "FDX", "FedEx Express", "US"),
    ("5Y", "GTI", "Atlas Air", "US"),
    ("AC", "ACA", "Air Canada", "CA"), ("WS", "WJA", "WestJet", "CA"),
    ("TS", "TSC", "Air Transat", "CA"), ("PD", "POE", "Porter Airlines", "CA"),
    ("AM", "AMX", "Aeromexico", "MX"), ("Y4", "VOI", "Volaris", "MX"),
    ("VB", "VIV", "Viva Aerobus", "MX"), ("CM", "CMP", "Copa Airlines", "PA"),
    ("AV", "AVA", "Avianca", "CO"), ("LA", "LAN", "LATAM Airlines", "CL"),
    ("JJ", "TAM", "LATAM Brasil", "BR"), ("G3", "GLO", "Gol", "BR"),
    ("AD", "AZU", "Azul", "BR"), ("AR", "ARG", "Aerolineas Argentinas", "AR"),
    ("BA", "BAW", "British Airways", "GB"), ("VS", "VIR", "Virgin Atlantic", "GB"),
    ("U2", "EZY", "easyJet", "GB"), ("LS", "EXS", "Jet2", "GB"),
    ("FR", "RYR", "Ryanair", "IE"), ("EI", "EIN", "Aer Lingus", "IE"),
    ("W6", "WZZ", "Wizz Air", "HU"), ("AF", "AFR", "Air France", "FR"),
    ("KL", "KLM", "KLM", "NL"), ("LH", "DLH", "Lufthansa", "DE"),
    ("EW", "EWG", "Eurowings", "DE"), ("DE", "CFG", "Condor", "DE"),
    ("LX", "SWR", "Swiss", "CH"), ("OS", "AUA", "Austrian Airlines", "AT"),
    ("SN", "BEL", "Brussels Airlines", "BE"), ("LG", "LGL", "Luxair", "LU"),
    ("IB", "IBE", "Iberia", "ES"), ("VY", "VLG", "Vueling", "ES"),
    ("UX", "AEA", "Air Europa", "ES"), ("TP", "TAP", "TAP Air Portugal", "PT"),
    ("AZ", "ITY", "ITA Airways", "IT"), ("SK", "SAS", "SAS", "SE"),
    ("AY", "FIN", "Finnair", "FI"), ("DY", "NAX", "Norwegian", "NO"),
    ("FI", "ICE", "Icelandair", "IS"), ("LO", "LOT", "LOT Polish Airlines", "PL"),
    ("BT", "BTI", "airBaltic", "LV"), ("JU", "ASL", "Air Serbia", "RS"),
    ("TK", "THY", "Turkish Airlines", "TR"), ("PC", "PGT", "Pegasus", "TR"),
    ("A3", "AEE", "Aegean Airlines", "GR"), ("SU", "AFL", "Aeroflot", "RU"),
    ("EK", "UAE", "Emirates", "AE"), ("EY", "ETD", "Etihad Airways", "AE"),
    ("FZ", "FDB", "flydubai", "AE"), ("G9", "ABY", "Air Arabia", "AE"),
    ("QR", "QTR", "Qatar Airways", "QA"), ("SV", "SVA", "Saudia", "SA"),
    ("GF", "GFA", "Gulf Air", "BH"), ("WY", "OMA", "Oman Air", "OM"),
    ("RJ", "RJA", "Royal Jordanian", "JO"), ("LY", "ELY", "El Al", "IL"),
    ("MS", "MSR", "EgyptAir", "EG"), ("ET", "ETH", "Ethiopian Airlines", "ET"),
    ("KQ", "KQA", "Kenya Airways", "KE"), ("SA", "SAA", "South African Airways", "ZA"),
    ("AT", "RAM", "Royal Air Maroc", "MA"), ("KC", "KZR", "Air Astana", "KZ"),
    ("AI", "AIC", "Air India", "IN"), ("IX", "AXB", "Air India Express", "IN"),
    ("6E", "IGO", "IndiGo", "IN"), ("SG", "SEJ", "SpiceJet", "IN"),
    ("UL", "ALK", "SriLankan Airlines", "LK"), ("PK", "PIA", "Pakistan International", "PK"),
    ("SQ", "SIA", "Singapore Airlines", "SG"), ("TR", "TGW", "Scoot", "SG"),
    ("CX", "CPA", "Cathay Pacific", "HK"), ("MH", "MAS", "Malaysia Airlines", "MY"),
    ("AK", "AXM", "AirAsia", "MY"), ("TG", "THA", "Thai Airways", "TH"),
    ("VN", "HVN", "Vietnam Airlines", "VN"), ("VJ", "VJC", "VietJet Air", "VN"),
    ("PR", "PAL", "Philippine Airlines", "PH"), ("5J", "CEB", "Cebu Pacific", "PH"),
    ("GA", "GIA", "Garuda Indonesia", "ID"), ("NH", "ANA", "All Nippon Airways", "JP"),
    ("JL", "JAL", "Japan Airlines", "JP"), ("KE", "KAL", "Korean Air", "KR"),
    ("OZ", "AAR", "Asiana Airlines", "KR"), ("CI", "CAL", "China Airlines", "TW"),
    ("BR", "EVA", "EVA Air", "TW"), ("CA", "CCA", "Air China", "CN"),
    ("MU", "CES", "China Eastern", "CN"), ("CZ", "CSN", "China Southern", "CN"),
    ("HU", "CHH", "Hainan Airlines", "CN"), ("3U", "CSC", "Sichuan Airlines", "CN"),
    ("QF", "QFA", "Qantas", "AU"), ("VA", "VOZ", "Virgin Australia", "AU"),
    ("JQ", "JST", "Jetstar", "AU"), ("NZ", "ANZ", "Air New Zealand", "NZ"),
    ("FJ", "FJI", "Fiji Airways", "FJ"), ("CV", "CLX", "Cargolux", "LU"),
]

AIRLINES = [dict(iata=a, icao=b, name=c, country=d) for a, b, c, d in _AIRLINES]


def _load_extra() -> list[dict]:
    """Extra airlines generated by scripts/build_airlines.py (OpenFlights data); optional."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "data", "airlines_extra.json"), encoding="utf-8") as f:
            have = {a["icao"] for a in AIRLINES}
            return [a for a in json.load(f) if a["icao"] not in have]
    except (OSError, ValueError, KeyError):
        return []


_BUILTIN = len(AIRLINES)
AIRLINES += _load_extra()
BY_IATA = {a["iata"]: a for a in AIRLINES}
BY_ICAO = {a["icao"]: a for a in AIRLINES}

_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2,3}?)\s*-?\s*(\d{1,4}[A-Z]?)$")
_HEX_RE = re.compile(r"^[0-9A-F]{6}$")


def find_airline(text: str) -> dict | None:
    t = text.strip().upper()
    if t in BY_ICAO:
        return BY_ICAO[t]
    if t in BY_IATA:
        return BY_IATA[t]
    matches = [a for a in AIRLINES if a["name"].upper().startswith(t)] if len(t) >= 3 else []
    if len(matches) > 1:
        exact = [a for a in matches if a["name"].upper() == t]
        # "united" also matches obscure carriers; a single major (built-in) match wins, otherwise stay ambiguous.
        matches = exact or [a for a in matches if a in AIRLINES[:_BUILTIN]]
    return matches[0] if len(matches) == 1 else None


def parse_query(q: str) -> dict:
    """Classify a free-text query.

    Returns one of:
      {"kind": "callsign", "callsign": "BAW123", "iata": "BA123", "airline": {...}}
      {"kind": "hex", "hex": "4010ee"}
      {"kind": "airline", "airline": {...}}
      {"kind": "unknown", "text": ...}
    """
    raw = q.strip().upper()
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return {"kind": "unknown", "text": q}

    # "United 123" / "british airways 117"
    m = re.match(r"^(.*?)[\s-]*(\d{1,4}[A-Z]?)$", raw)
    if m and len(m.group(1).strip()) > 3:
        al = find_airline(m.group(1).strip())
        if al:
            return _flight(al, m.group(2))

    m = _FLIGHT_RE.match(compact)
    if m:
        prefix, num = m.groups()
        # Try 3-char ICAO first ("BAW123"), then 2-char IATA ("BA123"). The lazy regex
        # grabs 2 chars, so also test the 3-char split explicitly.
        if len(compact) >= 4 and compact[:3] in BY_ICAO and compact[3:].isalnum():
            return _flight(BY_ICAO[compact[:3]], compact[3:])
        if prefix in BY_IATA:
            return _flight(BY_IATA[prefix], num)

    if _HEX_RE.match(compact) and not compact.isdigit():
        return {"kind": "hex", "hex": compact.lower(), "callsign_guess": compact}

    al = find_airline(raw)
    if al:
        return {"kind": "airline", "airline": al}

    # Unknown operator but callsign-shaped (e.g. N123AB, private/GA or unlisted airline).
    if re.match(r"^[A-Z0-9]{2,8}$", compact):
        return {"kind": "callsign", "callsign": compact, "iata": None, "airline": None}
    return {"kind": "unknown", "text": q}


def _flight(al: dict, num: str) -> dict:
    num = num.lstrip("0") or "0"
    return {
        "kind": "callsign",
        "callsign": f"{al['icao']}{num}",
        "iata": f"{al['iata']}{num}",
        "airline": al,
    }


def airline_for_callsign(callsign: str | None) -> dict | None:
    if not callsign or len(callsign) < 4:
        return None
    return BY_ICAO.get(callsign[:3].upper()) if callsign[3].isdigit() else None
