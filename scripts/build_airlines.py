#!/usr/bin/env python3
"""Build backend/app/data/airlines_extra.json from the OpenFlights airline list.

    python scripts/build_airlines.py [path/to/airlines.dat]     # downloads it when no path is given

Keeps active airlines that have both an IATA (2 char) and ICAO (3 letter) code, skips anything the
built-in table already covers, and skips IATA codes shared by several active airlines (they'd map
to the wrong logo). OpenFlights data is ODbL licensed: https://openflights.org/data.html
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.app.airlines import _AIRLINES  # noqa: E402  (built-in table only)

URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
OUT = os.path.join(ROOT, "backend", "app", "data", "airlines_extra.json")


def main() -> int:
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read()
    else:
        raw = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8")
    have_icao = {a[1] for a in _AIRLINES}
    have_iata = {a[0] for a in _AIRLINES}
    rows = []
    for r in csv.reader(io.StringIO(raw)):
        if len(r) < 8 or r[7] != "Y":
            continue
        name, iata, icao, country = r[1].strip(), r[3].strip().upper(), r[4].strip().upper(), r[6].strip()
        if not (re.fullmatch(r"[A-Z0-9]{2}", iata) and re.fullmatch(r"[A-Z]{3}", icao)) or icao in have_icao:
            continue
        rows.append((iata, icao, name, country))
    count = {}
    for iata, *_ in rows:
        count[iata] = count.get(iata, 0) + 1
    out = [{"iata": i, "icao": c, "name": n, "country": k} for i, c, n, k in rows
           if count[i] == 1 and i not in have_iata]
    out.sort(key=lambda a: a["name"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"wrote {len(out)} airlines to {os.path.relpath(OUT, ROOT)} "
          f"({len(rows) - len(out)} skipped for duplicate IATA codes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
