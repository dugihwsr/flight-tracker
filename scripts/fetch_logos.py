#!/usr/bin/env python3
"""Download airline logos into frontend/logos/<IATA>.png (the file names the UI looks for).

Run it on a machine with normal internet access (stdlib only, no installs):

    python scripts/fetch_logos.py                 # every airline in backend/app/airlines.py
    python scripts/fetch_logos.py UA DL BA        # just these IATA codes
    python scripts/fetch_logos.py --url "https://example.com/logos/{iata}.png"

The default source is a public logo image CDN (Kiwi.com). It is unofficial and undocumented, so it may
change or disappear; use --url to point at any host that serves `<IATA>.png` (or any template with
{iata} / {icao}). Airline logos are trademarks of their owners: this is meant for a private, self-hosted
tracker. Existing files are kept unless you pass --force. Missing logos are fine: the UI falls back to
coloured initials.
"""
import argparse
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.app.airlines import AIRLINES  # noqa: E402

DEFAULT_URL = "https://images.kiwi.com/airlines/128x128/{iata}.png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def fetch(url: str, timeout: float) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "flighttracker-logo-fetch/1.0 (self-hosted)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"    HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"    {e}")
        return None
    return data if data.startswith(PNG_MAGIC) else None  # reject HTML error pages etc.


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="*", help="IATA codes to fetch (default: all known airlines)")
    ap.add_argument("--url", default=DEFAULT_URL, help="URL template with {iata} and/or {icao}")
    ap.add_argument("--out", default=os.path.join(ROOT, "frontend", "logos"))
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between requests (be polite)")
    ap.add_argument("--timeout", type=float, default=15)
    a = ap.parse_args()

    wanted = {c.upper() for c in a.codes}
    airlines = [x for x in AIRLINES if not wanted or x["iata"] in wanted]
    unknown = wanted - {x["iata"] for x in AIRLINES}
    for c in sorted(unknown):  # allow codes that aren't in our table
        airlines.append({"iata": c, "icao": "", "name": c})
    os.makedirs(a.out, exist_ok=True)

    ok = skipped = missing = 0
    missed = []
    for al in airlines:
        dest = os.path.join(a.out, f"{al['iata']}.png")
        if os.path.exists(dest) and not a.force:
            skipped += 1
            continue
        print(f"{al['iata']:>3} {al['name']}")
        data = fetch(a.url.format(iata=al["iata"], icao=al["icao"]), a.timeout)
        if data:
            with open(dest, "wb") as f:
                f.write(data)
            ok += 1
        else:
            missing += 1
            missed.append(al["iata"])
        time.sleep(a.delay)
    print(f"\nsaved {ok}, already present {skipped}, not found {missing}" + (f": {' '.join(missed)}" if missed else ""))
    return 0 if ok or skipped else 1


if __name__ == "__main__":
    sys.exit(main())
