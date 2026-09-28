"""Heuristic flight classification: commercial / cargo / military / government / private / helicopter.

ADS-B carries no operator type, so this combines the readsb database flag (military bit),
ICAO24 address blocks, callsign patterns and the ADS-B emitter category. It is a best-effort
guess: expect the occasional misfile, and prefer "unknown" over guessing when signals are weak.
"""
import re

from .airlines import BY_ICAO

CATEGORIES = {
    "commercial": "Commercial airline",
    "cargo": "Cargo",
    "military": "Military",
    "government": "Government / state",
    "private": "Private / general aviation",
    "helicopter": "Helicopter",
    "unknown": "Unknown",
}

CARGO_ICAO = {"UPS", "FDX", "GTI", "CLX", "ABX", "ATN", "PAC", "DHK", "DHX", "BOX", "CKS", "NCA", "GEC",
              "MTN", "SQC", "CKK", "CAO", "TAY", "BCS", "PCM", "SWN", "ICL", "LCO", "AJT"}
# ICAO24 blocks allocated to military users (US and UK; others omitted rather than guessed).
MIL_HEX_RANGES = [(0xAE0000, 0xAFFFFF), (0x43C000, 0x43CFFF)]
MIL_PREFIX = re.compile(
    r"^(RCH|REACH|CNV|PAT|NAVY|ARMY|AIO|RRR|CFC|GAF|FAF|IAM|NATO|MMF|BAF|HKY|LAGR|ASCOT|TARTN|KING|JAKE|"
    r"TOPCAT|BOLT|COBRA|DOOM|HAVOC|REDEYE|RAIDER|VIPER|HAWK|EAGLE|TITAN|GORDO|SLAM|PACK|ETHYL|CASA|MOOSE|"
    r"SPAR|FORTE|LAGR|USAF|USN|USMC|CTM|COMET|NCHO)\d*[A-Z]{0,2}$")
GOV_PREFIX = re.compile(r"^(SAM|EXEC|EXEC1F|VENUS|AF[12]|NASA|FBI|CBP|DHS|BLKHK|SHERIFF|POLICE|LIFEGUARD|"
                        r"MEDEVAC|EVAC|TFO|JUSTICE|FEDERAL|GOVT)\d*[A-Z]{0,2}$")
AIRLINE_CS = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]{0,2}$")
N_NUMBER = re.compile(r"^N\d{1,5}[A-Z]{0,2}$")
REG_LIKE = re.compile(r"^(?:[A-Z]{1,2}-?[A-Z0-9]{3,5}|C[FGI][A-Z]{3})$")


def _mil_hex(hex_: str | None) -> bool:
    try:
        v = int(hex_ or "", 16)
    except ValueError:
        return False
    return any(lo <= v <= hi for lo, hi in MIL_HEX_RANGES)


def classify(callsign: str | None, hex_: str | None = None, category: str | None = None,
             db_flags: int | None = None) -> str:
    cs = (callsign or "").strip().upper()
    if (db_flags or 0) & 1 or _mil_hex(hex_) or MIL_PREFIX.match(cs):
        return "military"
    if GOV_PREFIX.match(cs):
        return "government"
    if category == "A7":
        return "helicopter"
    if cs[:3] in CARGO_ICAO and cs[3:4].isdigit():
        return "cargo"
    if cs[:3] in BY_ICAO and cs[3:4].isdigit():
        return "commercial"
    if AIRLINE_CS.match(cs):
        return "commercial"  # unlisted airline, still airline-style flight number
    if N_NUMBER.match(cs) or REG_LIKE.match(cs) or category in ("A1", "B1", "B2", "B4"):
        return "private"
    return "unknown"
