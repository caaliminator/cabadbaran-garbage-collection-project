"""
Import surveyed MRF coordinates into data/geo/mrf_surveyed.json.

    python tools/import_mrf_survey.py survey.txt            # parse and check
    python tools/import_mrf_survey.py survey.txt --write    # save them

Paste what the city sends you into a text file, one facility per line, and run
this. It reads the format they actually write in rather than asking anyone to
reformat by hand:

    3. Bayabas  - 9°08'05.49"N 125°35'29.63"E
    Poblacion 5 - 9 07 08.16 N, 125 31 59.50 E
    Cabinet, 9.124722, 125.527119

Degrees-minutes-seconds or decimal, comma or dash, degree symbols or
not, numbered or not. A line starting with # is a note and is skipped.

WHY A SEPARATE FILE

Surveyed coordinates and generated ones must never sit in the same file
looking alike. This one holds only what somebody actually measured on the
ground; tools/make_demo_geo.py reads it, uses every coordinate in it verbatim,
and falls back to a position near the barangay's centre only for the
facilities still unsurveyed. So the split between "known" and "approximated"
is visible in the data rather than remembered by whoever last touched it.

That matters beyond tidiness: a route computed between approximated points is
not a route between the places a truck stops, and anything measuring distance
has to be able to tell which is which.

WHAT IT CHECKS

A coordinate that parses is not the same as a coordinate that is right. Each
one is tested against Cabadbaran's bounding box, and against the published
centre of the barangay it claims to belong to -- a facility should be within a
couple of kilometres of its own barangay, and anything further is far more
likely to be a transcription slip than a genuinely remote MRF.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config                                        # noqa: E402
from services import storage, timeutil                           # noqa: E402

OUT = Path(Config.GEO_DIR) / "mrf_surveyed.json"
CENTRES = Path(Config.GEO_DIR) / "barangay_centres.json"

BOUNDS = {"lat": (8.95, 9.35), "lng": (125.40, 125.75)}

# How far from its barangay's published centre a facility may sit before this
# asks a human to look again. Cabadbaran's rural barangays are large, so this
# is deliberately generous -- it is a typo catcher, not a rule.
SUSPICIOUS_KM = 3.0

# 9°08'05.49"N  |  9 08 05.49 N  |  9:08:05.49N -- degrees, minutes, seconds,
# any separator, hemisphere letter optional.
DMS = re.compile(r"""(?P<deg>\d{1,3})\s*[°:\s]\s*
                     (?P<min>\d{1,2})\s*['′:\s]\s*
                     (?P<sec>\d{1,2}(?:\.\d+)?)\s*["″]?\s*
                     (?P<hemi>[NSEW])?""", re.X | re.I)

DECIMAL = re.compile(r"(-?\d{1,3}\.\d+)\s*(?P<hemi>[NSEW])?", re.I)


def to_decimal(deg: str, minutes: str, seconds: str, hemisphere: str | None) -> float:
    value = int(deg) + int(minutes) / 60 + float(seconds) / 3600
    return -value if (hemisphere or "").upper() in ("S", "W") else value


def parse_line(line: str) -> tuple[str, float, float] | None:
    """
    One line -> (name, lat, lng), or None for a blank or a comment.

    The name is whatever precedes the first coordinate, which is how the city
    writes these: a facility name, then a separator, then the numbers.
    """
    line = line.strip().strip('"').strip()
    if not line or line.startswith("#"):
        return None

    dms = list(DMS.finditer(line))
    if len(dms) >= 2:
        first, second = dms[0], dms[1]
        lat = to_decimal(first["deg"], first["min"], first["sec"], first["hemi"])
        lng = to_decimal(second["deg"], second["min"], second["sec"], second["hemi"])
        name = line[:first.start()]
    else:
        decimals = list(DECIMAL.finditer(line))
        if len(decimals) < 2:
            raise ValueError("no coordinate pair found")
        lat, lng = float(decimals[0].group(1)), float(decimals[1].group(1))
        name = line[:decimals[0].start()]

    # The city numbers its lists ("1. Antonio Luna - ..."), and that number
    # is not part of the facility's name. Stripped here rather than asked to
    # be removed by hand, because retyping a list of 31 names is exactly how
    # a coordinate ends up filed under the wrong barangay.
    name = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", name.strip())
    return name.strip(" \t-–—:,"), lat, lng


def centres() -> dict:
    if not CENTRES.exists():
        return {}
    data = json.loads(CENTRES.read_text(encoding="utf-8"))
    return {row["name"].lower(): row for row in data.get("centres", [])}


def km_apart(a: tuple, b: tuple) -> float:
    return math.hypot((a[0] - b[0]) * 111.32,
                      (a[1] - b[1]) * 111.32 * math.cos(math.radians(a[0])))


def collect(lines: list[str]) -> tuple[list, list]:
    known = {b["name"].lower(): b for b in storage.read("barangays")}
    published = centres()
    rows, problems = [], []

    for number, raw in enumerate(lines, start=1):
        try:
            parsed = parse_line(raw)
        except ValueError as exc:
            problems.append(f"line {number}: {exc} -- {raw.strip()!r}")
            continue
        if not parsed:
            continue

        name, lat, lng = parsed
        barangay = known.get(name.lower())
        if not barangay:
            problems.append(f"line {number}: no barangay named {name!r}")
            continue

        if not (BOUNDS["lat"][0] <= lat <= BOUNDS["lat"][1]
                and BOUNDS["lng"][0] <= lng <= BOUNDS["lng"][1]):
            problems.append(f"{name}: {lat:.6f},{lng:.6f} is outside Cabadbaran")
            continue

        note = ""
        centre = published.get(name.lower())
        if centre:
            gap = km_apart((lat, lng), (centre["lat"], centre["lng"]))
            note = f"{gap:.2f} km from the barangay centre"
            if gap > SUSPICIOUS_KM:
                problems.append(f"{name}: {note} -- check for a transcription slip")
                continue

        rows.append({
            "barangay_id": barangay["id"],
            "name": barangay["name"],
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "as_given": raw.strip(),
        })
        print(f"  {barangay['name']:<14} {lat:.6f}, {lng:.6f}"
              + (f"   ({note})" if note else ""))

    return rows, problems


def merge(rows: list) -> list:
    """
    Keep anything already imported that this batch does not mention.

    Surveys arrive a few barangays at a time, and a later batch must not quietly
    delete an earlier one. A barangay named again is updated -- a re-measured
    facility is exactly the case where the newer reading should win.
    """
    if not OUT.exists():
        return rows
    existing = json.loads(OUT.read_text(encoding="utf-8")).get("surveyed", [])
    replaced = {row["barangay_id"] for row in rows}
    kept = [row for row in existing if row["barangay_id"] not in replaced]
    if kept:
        print(f"\n  keeping {len(kept)} previously imported facilities")
    return sorted(rows + kept, key=lambda r: r["barangay_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="text file of surveyed coordinates")
    parser.add_argument("--write", action="store_true", help=f"save to {OUT.name}")
    args = parser.parse_args()

    lines = Path(args.source).read_text(encoding="utf-8").splitlines()
    print(f"Reading {args.source}\n")
    rows, problems = collect(lines)

    print(f"\n{len(rows)} facilities parsed, {len(problems)} rejected")
    for problem in problems:
        print("  !", problem)

    if not args.write:
        print("\n(dry run -- pass --write to save)")
        return 1 if problems else 0
    if problems:
        print("\nNothing written: fix the rejected lines first.")
        return 1

    merged = merge(rows)
    total = storage.count("barangays")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_note": (
            "SURVEYED MRF COORDINATES -- measured on the ground, one entry per "
            "facility. Every coordinate here is used verbatim. Barangays absent "
            "from this list have no surveyed facility yet and are approximated "
            "near their barangay centre by tools/make_demo_geo.py."),
        "_imported": timeutil.today_str(),
        "_surveyed_count": len(merged),
        "_barangay_count": total,
        "surveyed": merged,
    }, indent=2), encoding="utf-8")

    print(f"\nWrote {len(merged)} of {total} facilities to {OUT}")
    if len(merged) < total:
        missing = {b["name"] for b in storage.read("barangays")} - {
            r["name"] for r in merged}
        print(f"Still unsurveyed ({len(missing)}): {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
