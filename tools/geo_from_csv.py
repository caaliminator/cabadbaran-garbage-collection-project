"""
Turn a filled-in spreadsheet into the map's geo files.

The point of this tool: collecting real map data does not have to mean editing
GeoJSON by hand. Type coordinates into a CSV in Excel or Google Sheets, run
this, and the maps pick them up.

    python tools/geo_from_csv.py --template          # write blank CSVs to fill in
    python tools/geo_from_csv.py --check             # validate, change nothing
    python tools/geo_from_csv.py                     # import into data/geo/

Two files, and you can stop after the first:

  data/geo/input/mrf_points.csv       one row per barangay: lat, lng of the MRF
                                      -> data/geo/mrf_locations.json
  data/geo/input/barangay_zones.csv   one row per boundary point
                                      -> data/geo/barangay_zones.geojson

Fill in only the rows you have. A barangay with no coordinates is left as a
placeholder and simply does not draw -- nothing breaks, and /api/geo/status
reports how many are still missing.

Coordinate order in BOTH CSVs is lat, lng -- the order a phone GPS or Google
Maps gives you. GeoJSON wants them the other way round, and this tool does that
flip for you. That single reversal is the most common way this data goes wrong.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from services import geo_service, storage

INPUT_DIR = Path(Config.GEO_DIR) / "input"
MRF_CSV = INPUT_DIR / "mrf_points.csv"
ZONES_CSV = INPUT_DIR / "barangay_zones.csv"

# Cabadbaran sits inside this box. A coordinate outside it is almost always a
# lat/lng swap or a typed digit, and catching it here beats hunting for a
# barangay that rendered off the coast of Africa.
BOUNDS = {"lat": (8.9, 9.4), "lng": (125.3, 125.8)}

MRF_HEADERS = ["barangay_id", "barangay_name", "mrf_name", "lat", "lng", "notes"]
ZONE_HEADERS = ["barangay_id", "barangay_name", "point_order", "lat", "lng"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def barangays() -> list[dict]:
    storage.bootstrap()
    rows = storage.find("barangays")
    if not rows:
        raise SystemExit("No barangays in the store. Run `python seed.py` first.")
    return sorted(rows, key=lambda b: b.get("number") or 0)


def write_templates() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = barangays()

    with MRF_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(MRF_HEADERS)
        for b in rows:
            writer.writerow([b["id"], b["name"], b.get("mrf_name") or "",
                             "", "", ""])

    with ZONES_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(ZONE_HEADERS)
        # Four blank points per barangay as a starting frame -- a polygon needs
        # at least three, and add as many rows as the real boundary needs.
        for b in rows:
            for order in range(1, 5):
                writer.writerow([b["id"], b["name"], order, "", ""])

    print(f"Wrote templates:\n  {MRF_CSV}\n  {ZONES_CSV}")
    print("\nOpen them in Excel or Sheets, fill in lat and lng (in that order),")
    print("save as CSV, then run:  python tools/geo_from_csv.py --check")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_csv(path: Path, headers: list[str]) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [h for h in headers if h not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"{path.name} is missing column(s): {', '.join(missing)}.\n"
                f"Expected: {', '.join(headers)}")
        return [row for row in reader]


def coordinate(row: dict, where: str, problems: list[str]) -> tuple | None:
    """A (lat, lng) pair, or None when the row is simply not filled in yet."""
    raw_lat = (row.get("lat") or "").strip()
    raw_lng = (row.get("lng") or "").strip()
    if not raw_lat and not raw_lng:
        return None
    if not raw_lat or not raw_lng:
        problems.append(f"{where}: only one of lat/lng is filled in")
        return None
    try:
        lat, lng = float(raw_lat), float(raw_lng)
    except ValueError:
        problems.append(f"{where}: '{raw_lat}, {raw_lng}' is not a number pair")
        return None

    lo, hi = BOUNDS["lat"]
    if not lo <= lat <= hi:
        problems.append(
            f"{where}: latitude {lat} is outside Cabadbaran ({lo}-{hi}). "
            "Columns swapped?")
        return None
    lo, hi = BOUNDS["lng"]
    if not lo <= lng <= hi:
        problems.append(
            f"{where}: longitude {lng} is outside Cabadbaran ({lo}-{hi}). "
            "Columns swapped?")
        return None
    return round(lat, 6), round(lng, 6)


def build_mrfs(problems: list[str]) -> tuple[list[dict], int]:
    known = {b["id"]: b for b in barangays()}
    rows = read_csv(MRF_CSV, MRF_HEADERS)
    out, filled = [], 0

    for b in barangays():
        out.append({"barangay_id": b["id"],
                    "name": b.get("mrf_name") or f"{b['name']} MRF"})

    index = {r["barangay_id"]: r for r in out}
    for row in rows:
        bid = (row.get("barangay_id") or "").strip()
        if bid not in known:
            problems.append(f"{MRF_CSV.name}: unknown barangay_id '{bid}'")
            continue
        point = coordinate(row, f"{MRF_CSV.name} {bid}", problems)
        if not point:
            continue
        if (row.get("mrf_name") or "").strip():
            index[bid]["name"] = row["mrf_name"].strip()
        index[bid]["lat"], index[bid]["lng"] = point
        filled += 1

    return out, filled


def build_zones(problems: list[str]) -> tuple[dict, int]:
    known = {b["id"]: b for b in barangays()}
    rows = read_csv(ZONES_CSV, ZONE_HEADERS)

    points: dict[str, list] = {}
    for row in rows:
        bid = (row.get("barangay_id") or "").strip()
        if not bid:
            continue
        if bid not in known:
            problems.append(f"{ZONES_CSV.name}: unknown barangay_id '{bid}'")
            continue
        order = (row.get("point_order") or "").strip()
        point = coordinate(row, f"{ZONES_CSV.name} {bid} point {order}", problems)
        if point:
            points.setdefault(bid, []).append((_as_int(order), point))

    features = []
    for bid, entries in points.items():
        if len(entries) < 3:
            problems.append(
                f"{ZONES_CSV.name}: {bid} has {len(entries)} point(s); "
                "a boundary needs at least 3. Skipped.")
            continue
        entries.sort(key=lambda e: e[0])
        # GeoJSON is [lng, lat] -- the reverse of every other file here -- and
        # a ring has to close by repeating its first point.
        ring = [[lng, lat] for _, (lat, lng) in entries]
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        b = known[bid]
        features.append({
            "type": "Feature",
            "properties": {"barangay_id": bid, "name": b["name"],
                           "zone_group": b.get("zone_group")},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })

    features.sort(key=lambda f: f["properties"]["barangay_id"])
    return {"type": "FeatureCollection", "features": features}, len(features)


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".backup")
    if path.exists() and not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", action="store_true",
                        help="write blank CSVs for all 31 barangays and stop")
    parser.add_argument("--check", action="store_true",
                        help="validate the CSVs without writing anything")
    args = parser.parse_args()

    if args.template:
        write_templates()
        return 0

    if not MRF_CSV.exists() and not ZONES_CSV.exists():
        print("No input CSVs found. Create them with:\n"
              "  python tools/geo_from_csv.py --template")
        return 1

    problems: list[str] = []
    mrfs, mrf_filled = build_mrfs(problems)
    zones, zone_count = build_zones(problems)

    total = len(barangays())
    print(f"MRF points     {mrf_filled}/{total} barangays have coordinates")
    print(f"Zone polygons  {zone_count}/{total} barangays have a boundary")

    if problems:
        print(f"\n{len(problems)} problem(s) found:")
        for line in problems[:25]:
            print(f"  - {line}")
        if len(problems) > 25:
            print(f"  ... and {len(problems) - 25} more")

    if args.check:
        print("\n--check: nothing written.")
        return 1 if problems else 0

    if not mrf_filled and not zone_count:
        print("\nNothing filled in yet, so nothing to import. The placeholders "
              "stay in place.")
        return 1

    if mrf_filled:
        write_json(geo_service.geo_path(geo_service.MRFS_FILE), mrfs)
        print(f"\nWrote {geo_service.MRFS_FILE}")
    if zone_count:
        write_json(geo_service.geo_path(geo_service.ZONES_FILE), zones)
        print(f"Wrote {geo_service.ZONES_FILE}")

    geo_service.clear_cache()
    print("\nNo _placeholder flag is written, so the maps now treat this as "
          "real data.\nRefresh the map -- no restart needed. Check "
          "/api/geo/status to confirm.")
    print("The files replaced are kept alongside as *.backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
