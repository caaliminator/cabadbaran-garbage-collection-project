"""
Fetch each barangay's published coordinate from PhilAtlas.

    python tools/fetch_barangay_centres.py            # fetch and report
    python tools/fetch_barangay_centres.py --write    # save data/geo/barangay_centres.json

WHAT THIS IS, AND WHAT IT IS NOT

It is the real, citable position of each of the 31 barangays, which replaces
the compass-guessed centres that tools/make_demo_geo.py invented. With it the
map opens on the right part of Cabadbaran and no pin lands in Butuan Bay.

It is NOT the position of a barangay's MRF. A barangay is an area, often
kilometres across; its materials recovery facility is one building inside it,
sited wherever land was available -- usually by the barangay hall or out on
the access road a truck can reach. The two can be a kilometre or more apart.

That distinction is the whole reason this data is kept in its own file. Any
MRF position derived from it stays flagged a placeholder until somebody walks
to the facility with a GPS, because a route computed between village centres
is not a route between the places a truck actually stops.

A NOTE ON THE SOURCE

PhilAtlas publishes two different coordinates per page: one in the visible
infobox and one in the page's embedded structured data. They disagree -- for
Del Pilar by about 8 km -- and the structured-data values are inconsistent
between pages. This reads the infobox, which is what the site displays as the
barangay's location and what its own prose repeats.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config                                        # noqa: E402
from services import storage, timeutil                           # noqa: E402

BASE = "https://www.philatlas.com/mindanao/caraga/agusan-del-norte/cabadbaran"
OUT = Path(Config.GEO_DIR) / "barangay_centres.json"

# Politeness: this is someone's free public site and we want 31 pages from it.
DELAY_SECONDS = 0.7
AGENT = "cabadbaran-gcts/1.0 (barangay coordinate lookup; one-off)"

COORDS = re.compile(r"<span id='latitude'>([\d.]+)</span>, "
                    r"<span id='longitude'>([\d.]+)</span>")
ELEVATION = re.compile(r"estimated at ([\d.]+) meters")

# Cabadbaran sits well inside this box. Anything outside it is a parse error or
# a wrong page, not a barangay -- worth catching here rather than discovering it
# as a pin in the sea.
BOUNDS = {"lat": (8.95, 9.35), "lng": (125.40, 125.75)}


def slug_for(name: str) -> str:
    """'Poblacion 1' -> 'poblacion-1'; 'Bay-ang' -> 'bay-ang'."""
    return name.strip().lower().replace(" ", "-")


def fetch(slug: str) -> str:
    request = urllib.request.Request(f"{BASE}/{slug}.html",
                                     headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def collect() -> tuple[list, list]:
    rows, problems = [], []
    barangays = sorted(storage.read("barangays"), key=lambda b: b.get("number", 0))

    for index, barangay in enumerate(barangays):
        slug = slug_for(barangay["name"])
        try:
            html = fetch(slug)
        except urllib.error.HTTPError as exc:
            problems.append(f"{barangay['name']}: HTTP {exc.code} for /{slug}.html")
            continue
        except Exception as exc:                        # noqa: BLE001
            problems.append(f"{barangay['name']}: {exc}")
            continue

        found = COORDS.search(html)
        if not found:
            problems.append(f"{barangay['name']}: no coordinates on the page")
            continue

        lat, lng = float(found.group(1)), float(found.group(2))
        if not (BOUNDS["lat"][0] <= lat <= BOUNDS["lat"][1]
                and BOUNDS["lng"][0] <= lng <= BOUNDS["lng"][1]):
            problems.append(f"{barangay['name']}: {lat},{lng} is outside Cabadbaran")
            continue

        elevation = ELEVATION.search(html)
        rows.append({
            "barangay_id": barangay["id"],
            "name": barangay["name"],
            "lat": lat,
            "lng": lng,
            "elevation_m": float(elevation.group(1)) if elevation else None,
            "source_url": f"{BASE}/{slug}.html",
        })
        print(f"  {barangay['name']:<14} {lat:.4f}, {lng:.4f}")

        if index < len(barangays) - 1:
            time.sleep(DELAY_SECONDS)

    return rows, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help=f"save to {OUT.name}")
    args = parser.parse_args()

    print(f"Fetching {storage.count('barangays')} barangay coordinates from "
          f"PhilAtlas\n")
    rows, problems = collect()

    print(f"\n{len(rows)} fetched, {len(problems)} failed")
    for problem in problems:
        print("  !", problem)

    if not args.write:
        print("\n(dry run -- pass --write to save)")
        return 1 if problems else 0

    if problems:
        print("\nNothing written: fix the failures first, so the file is never "
              "a partial one that looks complete.")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_note": (
            "Barangay centre coordinates, not MRF locations. Published by "
            "PhilAtlas and read from each barangay's infobox. A barangay is an "
            "area; its MRF is one building inside it and has to be surveyed on "
            "the ground. See docs/DATA_REQUIREMENTS.md."),
        "_source": "https://www.philatlas.com/",
        "_fetched": timeutil.today_str(),
        "centres": rows,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote {len(rows)} centres to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
