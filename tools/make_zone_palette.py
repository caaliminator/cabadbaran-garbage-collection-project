"""
Generate the per-barangay map colours in `static/css/components.css`.

    python tools/make_zone_palette.py            # print the CSS block
    python tools/make_zone_palette.py --check    # verify the CSS is in sync

Colours belong in CSS, not in `map.js` -- the map layer emits class names and
never a colour value, so the whole palette is editable in one stylesheet
without touching JavaScript. This script exists so that stylesheet block is
reproducible rather than 31 hand-picked hex codes nobody can safely change.

WHY THE HUES STEP THE WAY THEY DO
---------------------------------
Thirty-one evenly spaced hues sit about 11.6 degrees apart, which the eye reads
as "greenish, slightly different greenish" -- and because barangays are
numbered in roughly geographic runs, near-identical colours would land side by
side on the map, exactly where the distinction matters.

Stepping by the golden angle (137.508 deg) instead means consecutive numbers
land on opposite sides of the colour wheel while the full set still spreads
evenly. Lightness then cycles over three values and saturation over two, so
even two barangays that come back around to a similar hue differ in weight.

Paste the output over the `.map-zone--brgy-*` block in components.css, or run
`--check` in CI to catch the stylesheet drifting from this script.
"""

import argparse
import colorsys
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import storage                                 # noqa: E402

CSS_FILE = ROOT / "static" / "css" / "components.css"
RULE = re.compile(r"^\.map-zone--(brgy-\d+)\s*\{[^}]*\}", re.MULTILINE)

GOLDEN_ANGLE = 137.508
HUE_OFFSET = 14        # nudge so barangay 1 is not pure red
LIGHTNESS = (50, 60, 42)
SATURATION = (66, 76)
STROKE_LIFT = 20       # stroke is the fill, lightened
STROKE_CAP = 78


def hex_of(hue: float, sat: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, light / 100.0, sat / 100.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def palette(barangays: list[dict]) -> list[tuple[str, str, str, str]]:
    """(barangay_id, fill, stroke, name) for each barangay, in number order."""
    out = []
    for i, row in enumerate(sorted(barangays, key=lambda b: b.get("number") or 0)):
        hue = (i * GOLDEN_ANGLE + HUE_OFFSET) % 360
        light = LIGHTNESS[i % len(LIGHTNESS)]
        sat = SATURATION[i % len(SATURATION)]
        out.append((
            row["id"],
            hex_of(hue, sat, light),
            hex_of(hue, min(90, sat + 10), min(STROKE_CAP, light + STROKE_LIFT)),
            row.get("name") or "",
        ))
    return out


def css_block(rows) -> str:
    return "\n".join(
        f".map-zone--{bid} {{ fill: {fill}; stroke: {stroke}; }}  /* {name} */"
        for bid, fill, stroke, name in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if components.css is out of sync")
    args = parser.parse_args()

    storage.bootstrap()
    barangays = storage.read("barangays")
    if not barangays:
        print("No barangays in the store. Run `python seed.py` first.")
        return 1

    rows = palette(barangays)

    if not args.check:
        print(css_block(rows))
        return 0

    css = CSS_FILE.read_text(encoding="utf-8")
    found = {m.group(1): m.group(0) for m in RULE.finditer(css)}
    problems = []
    for bid, fill, stroke, _ in rows:
        rule = found.get(bid)
        if rule is None:
            problems.append(f"{bid}: no rule in components.css")
        elif fill not in rule or stroke not in rule:
            problems.append(f"{bid}: expected fill {fill} / stroke {stroke}")
    for extra in sorted(set(found) - {r[0] for r in rows}):
        problems.append(f"{extra}: rule with no matching barangay")

    if problems:
        print(f"components.css is out of sync ({len(problems)} problem(s)):")
        for line in problems:
            print(f"  {line}")
        return 1

    print(f"components.css in sync: {len(rows)} barangay colours match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
