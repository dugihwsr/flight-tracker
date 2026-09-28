#!/usr/bin/env python3
"""Build backend/app/data/airports.json from the OurAirports dataset (public domain).

    python scripts/build_airports.py [path/to/airports.csv]     # downloads it when no path is given

This is our own, reliable source of airport *coordinates and names* - looked up by IATA/ICAO code -
kept deliberately separate from *which airports a flight connects*, which free ADS-B/route sources
(adsbdb) get wrong surprisingly often for reassigned mainline flight numbers (see tracker.py's
`effective_route`). Regenerate occasionally; airports rarely move or get renamed.
"""
import csv
import io
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
OUT = os.path.join(ROOT, "backend", "app", "data", "airports.json")


def main() -> int:
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read()
    else:
        raw = urllib.request.urlopen(URL, timeout=60).read().decode("utf-8")
    out = []
    for r in csv.DictReader(io.StringIO(raw)):
        iata, icao = r["iata_code"].strip().upper(), r["icao_code"].strip().upper()
        if not (iata or icao):
            continue
        try:
            lat, lon = float(r["latitude_deg"]), float(r["longitude_deg"])
        except ValueError:
            continue
        out.append({"iata": iata or None, "icao": icao or None, "name": r["name"],
                    "city": r["municipality"] or None, "country": r["iso_country"] or None,
                    "lat": lat, "lon": lon})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"wrote {len(out)} airports to {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
