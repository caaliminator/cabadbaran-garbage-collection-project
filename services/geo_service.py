"""
The single source of truth for every coordinate in the system.

No template, route, or JS file may contain a coordinate, a polygon, or a zone
colour. They all come from `data/geo/` at runtime, through this module, and are
served to the browser by `/api/geo/*`:

    barangay_zones.geojson   31 polygons: barangay_id, name, zone_group
    mrf_locations.json       one { barangay_id, name, lat, lng } per barangay
    hotspots.geojson         points/polygons: hotspot_id, barangay_id, purok,
                             severity, type, last_reported, notes

Every loader degrades gracefully. A missing, empty, malformed, or
still-placeholder file yields an empty result plus a `meta` block explaining
why -- the map keeps rendering tiles, zones, and live markers, and the UI shows
an empty state instead of an error.

Hotspots have two sources, chosen by `Config.HOTSPOT_SOURCE`:

    "file"     read hotspots.geojson as supplied
    "derived"  compute them from data the system already has -- Not Collected
               collection entries plus public reports, grouped by barangay and
               purok over a date window

`derived` is the default so the layer shows something real before any external
hotspot data arrives.
"""

import json
import math
import re
import threading
from datetime import timedelta
from pathlib import Path

from config import Config
from services import storage, timeutil

ZONES_FILE = "barangay_zones.geojson"
MRFS_FILE = "mrf_locations.json"
HOTSPOTS_FILE = "hotspots.geojson"

_cache: dict[str, tuple[tuple[int, int], object]] = {}
_cache_guard = threading.Lock()


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def geo_path(filename: str) -> Path:
    return Path(Config.GEO_DIR) / filename


def _load(filename: str):
    """
    Parse a geo file, cached on mtime so the map does not re-read from disk on
    every request but still picks up a swapped-in file without a restart.

    Returns (data, problem). `problem` is None on success, or a short reason
    string the API passes through to the UI's empty state.
    """
    file = geo_path(filename)
    try:
        # Nanoseconds and size together, not the float mtime alone: two writes
        # inside one timestamp tick otherwise look like no change at all, and
        # the older copy keeps being served.
        stat = file.stat()
        mtime = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None, "missing"

    with _cache_guard:
        cached = _cache.get(filename)
        if cached and cached[0] == mtime:
            return cached[1], None

    try:
        text = file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"unreadable ({exc.strerror or exc})"
    if not text:
        return None, "empty"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON on line {exc.lineno}"

    with _cache_guard:
        _cache[filename] = (mtime, data)
    return data, None


def clear_cache() -> None:
    """Drop the mtime cache. Used by tests and after a data swap."""
    with _cache_guard:
        _cache.clear()


def _is_placeholder(data) -> bool:
    """True when the shipped placeholder is still in place, unedited."""
    if isinstance(data, dict):
        return bool(data.get("_placeholder"))
    if isinstance(data, list):
        return any(isinstance(row, dict) and row.get("_placeholder") for row in data)
    return False


def _features(data) -> list[dict]:
    """Features from a FeatureCollection, tolerating a bare list of features."""
    if isinstance(data, dict):
        found = data.get("features")
        return found if isinstance(found, list) else []
    return data if isinstance(data, list) else []


def _has_geometry(feature: dict) -> bool:
    geom = feature.get("geometry")
    return bool(geom and geom.get("coordinates"))


# ---------------------------------------------------------------------------
# Barangay zones
# ---------------------------------------------------------------------------

def zone_group_for(number: int) -> dict:
    """The colour group a barangay number falls into. Never coordinate-based."""
    for first, last, key, label in Config.ZONE_GROUPS:
        if first <= number <= last:
            return {"key": key, "label": label, "first": first, "last": last}
    return {"key": "zone-none", "label": "Unassigned", "first": 0, "last": 0}


def zone_groups() -> list[dict]:
    return [{"key": key, "label": label, "first": first, "last": last}
            for first, last, key, label in Config.ZONE_GROUPS]


def barangay_zones() -> dict:
    """
    A FeatureCollection of the 31 barangay polygons, with each feature's
    properties enriched from `barangays.json` (number, zone group, purok count)
    so the client never has to join two endpoints itself.

    Features whose geometry has not been supplied yet are returned with
    `geometry: null` and counted in `meta.without_geometry`, so the UI can say
    "31 barangays known, 0 boundaries loaded" rather than silently drawing an
    empty map.
    """
    data, problem = _load(ZONES_FILE)
    placeholder = _is_placeholder(data)
    known = {b.get("id"): b for b in storage.read("barangays")}

    features, with_geometry = [], 0
    for feature in _features(data):
        props = dict(feature.get("properties") or {})
        bid = props.get("barangay_id")
        record = known.get(bid)
        if record:
            props.setdefault("name", record.get("name"))
            props["number"] = record.get("number")
            props["purok_count"] = len(record.get("puroks") or [])
            group = zone_group_for(record.get("number") or 0)
            props.setdefault("zone_group", group["key"])
            props["zone_label"] = group["label"]
        geometry = feature.get("geometry") if _has_geometry(feature) else None
        if geometry:
            with_geometry += 1
        features.append({"type": "Feature", "properties": props, "geometry": geometry})

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "source": ZONES_FILE,
            "placeholder": placeholder,
            "problem": problem,
            "total": len(features),
            "with_geometry": with_geometry,
            "without_geometry": len(features) - with_geometry,
            "zone_groups": zone_groups(),
        },
    }


def zone_centroid(barangay_id: str) -> tuple[float, float] | None:
    """
    Rough centre of a barangay polygon -- the mean of its outer-ring vertices.

    Good enough to anchor a marker or a derived hotspot; not a true centroid,
    and deliberately not used for anything that needs geodesic accuracy.
    """
    data, _ = _load(ZONES_FILE)
    for feature in _features(data):
        props = feature.get("properties") or {}
        if props.get("barangay_id") != barangay_id or not _has_geometry(feature):
            continue
        geom = feature["geometry"]
        coords = geom.get("coordinates") or []
        kind = geom.get("type")
        if kind == "Polygon":
            ring = coords[0] if coords else []
        elif kind == "MultiPolygon":
            ring = coords[0][0] if coords and coords[0] else []
        elif kind == "Point":
            return (coords[1], coords[0])
        else:
            continue
        points = [p for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not points:
            continue
        # GeoJSON stores [lng, lat]; the rest of the app speaks (lat, lng).
        return (sum(p[1] for p in points) / len(points),
                sum(p[0] for p in points) / len(points))
    return None


# ---------------------------------------------------------------------------
# MRF locations
# ---------------------------------------------------------------------------

SURVEY_FILE = "mrf_surveyed.json"

# Cabadbaran with room to spare. A coordinate outside this is a typo, a
# swapped lat/lng pair, or a reading taken in the wrong town -- none of which
# should be storable.
CITY_BOUNDS = {"lat": (8.95, 9.35), "lng": (125.40, 125.75)}


def surveyed_points() -> dict:
    """
    Coordinates measured at the facility, or set by hand by a City Hall Admin,
    keyed by barangay id.

    This is an overlay read at request time rather than a file the generator
    bakes in. That is what lets an admin correct a pin and see it move on the
    next page load: the generated file stays the fallback for facilities nobody
    has been to yet, and never overwrites a known position.
    """
    data, _ = _load(SURVEY_FILE)
    rows = (data or {}).get("surveyed") if isinstance(data, dict) else None
    return {row["barangay_id"]: row for row in rows or []
            if row.get("barangay_id") and row.get("lat") is not None}


def _write_survey(rows: list) -> None:
    from services import timeutil

    path = Path(Config.GEO_DIR) / SURVEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_note": (
            "SURVEYED MRF COORDINATES -- measured on the ground or set by a "
            "City Hall Admin. Every coordinate here is used exactly as given. "
            "Barangays absent from this list have no known facility position "
            "and are approximated near their barangay centre."),
        "_imported": timeutil.today_str(),
        "_surveyed_count": len(rows),
        "_barangay_count": storage.count("barangays"),
        "surveyed": sorted(rows, key=lambda r: r.get("barangay_id") or ""),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # This process just changed the file, so it knows the cache is stale --
    # no need to trust the file clock to notice. Without this, an admin who
    # saved and then cleared a location in quick succession could be shown
    # the location they had just cleared.
    with _cache_guard:
        _cache.pop(SURVEY_FILE, None)


def set_mrf_location(barangay_id: str, lat, lng, actor: str | None = None,
                     note: str = "") -> dict:
    """
    Record where a barangay's MRF actually is.

    Raises ValidationError with per-field messages, because this is driven by a
    form and an admin who typed 9.12 into the longitude box needs to be told
    which box was wrong, not handed a stack trace.
    """
    from services.validation import ValidationError

    errors = {}
    barangay = storage.find_one("barangays", id=barangay_id)
    if not barangay:
        errors["form"] = "That barangay no longer exists."

    values = {}
    for field, raw, span in (("lat", lat, CITY_BOUNDS["lat"]),
                             ("lng", lng, CITY_BOUNDS["lng"])):
        parsed = parse_coordinate(raw)
        label = "Latitude" if field == "lat" else "Longitude"
        if parsed is None:
            errors[field] = (
                f"{label} must be a number in decimal degrees (9.123456) or "
                f"degrees-minutes-seconds (9\u00b007'24.3\"N).")
        elif not (span[0] <= parsed <= span[1]):
            errors[field] = (
                f"{parsed:.6f} is outside Cabadbaran. {label} should be "
                f"between {span[0]} and {span[1]} -- check the two values are "
                f"not swapped.")
        else:
            values[field] = round(parsed, 6)

    if errors:
        raise ValidationError(errors)

    from services import timeutil

    rows = [row for row in surveyed_points().values()
            if row["barangay_id"] != barangay_id]
    rows.append({
        "barangay_id": barangay_id,
        "name": barangay.get("name"),
        "lat": values["lat"],
        "lng": values["lng"],
        "source": "Set by City Hall Admin",
        "note": (note or "").strip()[:200],
        "updated_by": actor,
        "updated_at": timeutil.stamp(),
    })
    _write_survey(rows)
    return next(r for r in rows if r["barangay_id"] == barangay_id)


def clear_mrf_location(barangay_id: str) -> bool:
    """
    Forget a facility's position, falling back to the approximation.

    Deliberately available: an admin who pins the wrong building needs a way
    back to "we do not know" rather than being stuck with a wrong figure that
    the system presents as surveyed.
    """
    rows = list(surveyed_points().values())
    kept = [row for row in rows if row["barangay_id"] != barangay_id]
    if len(kept) == len(rows):
        return False
    _write_survey(kept)
    return True


def parse_coordinate(value) -> float | None:
    """
    One coordinate from whatever an admin pasted in.

    Decimal degrees, or degrees-minutes-seconds in any of the spellings people
    actually use -- 9\u00b007'24.26"N, 9 07 24.26 N, 9:07:24.26. The city sends
    coordinates in DMS, so refusing it would mean every admin converting by
    hand, which is a step that introduces the errors this validates for.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    hemisphere = -1 if re.search(r"[SW]\s*$", text, re.I) else 1
    text = re.sub(r"[NSEW]\s*$", "", text, flags=re.I).strip()

    dms = re.fullmatch(
        r"(\d{1,3})\s*[\u00b0:\s]\s*(\d{1,2})\s*['\u2032:\s]\s*"
        r"(\d{1,2}(?:\.\d+)?)\s*[\"\u2033]?", text)
    if dms:
        degrees, minutes, seconds = dms.groups()
        return hemisphere * (int(degrees) + int(minutes) / 60
                             + float(seconds) / 3600)

    try:
        return hemisphere * abs(float(text)) if hemisphere < 0 else float(text)
    except ValueError:
        return None


def mrf_locations() -> dict:
    """
    Every barangay MRF with its coordinates, name resolved against
    `barangays.json`. Entries still awaiting coordinates come back with
    `lat`/`lng` of null and `located: false`.

    `surveyed` says whether a facility's position was measured on the ground or
    approximated from its barangay's centre. The two are not interchangeable --
    an approximated pin is in the right barangay but not at the building -- and
    anything computing distances between facilities has to be able to tell them
    apart. The meta block counts both, so a caller can say "18 of 31 surveyed"
    without walking the list.
    """
    data, problem = _load(MRFS_FILE)
    placeholder = _is_placeholder(data)
    rows = data if isinstance(data, list) else (data or {}).get("mrfs", [])
    known = {b.get("id"): b for b in storage.read("barangays")}
    # Known positions win over generated ones, every time.
    overlay = surveyed_points()

    items, located = [], 0
    for row in rows or []:
        if not isinstance(row, dict) or row.get("_placeholder_note"):
            continue
        bid = row.get("barangay_id")
        record = known.get(bid) or {}
        known_point = overlay.get(bid)
        if known_point:
            lat, lng = _coerce_point(known_point["lat"], known_point["lng"])
        else:
            lat, lng = _coerce_point(row.get("lat"), row.get("lng"))
        if lat is not None:
            located += 1
        items.append({
            "barangay_id": bid,
            "barangay_name": record.get("name"),
            "number": record.get("number"),
            "name": row.get("name") or (f"{record.get('name')} MRF"
                                        if record.get("name") else None),
            "lat": lat,
            "lng": lng,
            "located": lat is not None,
            # Absent means not surveyed -- the safe reading, since a pin
            # that cannot say where it came from is not one to trust with a
            # distance.
            "surveyed": bool(known_point or row.get("surveyed")),
            "source": ((known_point or {}).get("source") or "Field survey"
                       if known_point or row.get("surveyed")
                       else "Approximated from the barangay centre"),
            "updated_at": (known_point or {}).get("updated_at"),
        })

    return {
        "mrfs": items,
        "meta": {
            "source": MRFS_FILE,
            "placeholder": placeholder,
            "problem": problem,
            "total": len(items),
            "located": located,
            "unlocated": len(items) - located,
            "surveyed": sum(1 for row in items if row["surveyed"]),
            "approximated": sum(1 for row in items if not row["surveyed"]),
        },
    }


def _coerce_point(lat, lng) -> tuple[float | None, float | None]:
    """Validate a lat/lng pair; anything out of range is treated as absent."""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return None, None
    return lat_f, lng_f


def point_for_barangay(barangay_id: str) -> tuple[float, float] | None:
    """
    Best available anchor for a barangay: its MRF if we have one, else the
    centroid of its polygon, else nothing. Used to place derived hotspots and,
    from Phase 7, to measure truck-approach distance.
    """
    for row in mrf_locations()["mrfs"]:
        if row["barangay_id"] == barangay_id and row["located"]:
            return (row["lat"], row["lng"])
    return zone_centroid(barangay_id)


def haversine_metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lng) pairs."""
    radius = 6_371_000.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


# ---------------------------------------------------------------------------
# Hotspots
# ---------------------------------------------------------------------------

def hotspots(barangay=None, date_from=None, date_to=None, severity=None) -> dict:
    """Dispatch to the configured source, applying the same filters to both."""
    if not Config.HOTSPOT_LAYER_ENABLED:
        return _empty_hotspots("disabled", "The hotspot layer is turned off.")

    if str(Config.HOTSPOT_SOURCE).lower() == "file":
        result = _hotspots_from_file(barangay, date_from, date_to, severity)
    else:
        result = _hotspots_derived(barangay, date_from, date_to, severity)
    return result


def _empty_hotspots(source: str, reason: str | None = None) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [],
        "meta": {"source": source, "enabled": Config.HOTSPOT_LAYER_ENABLED,
                 "problem": reason, "total": 0, "unplaced": 0},
    }


def _hotspots_from_file(barangay, date_from, date_to, severity) -> dict:
    data, problem = _load(HOTSPOTS_FILE)
    if problem or _is_placeholder(data):
        return _empty_hotspots(
            "file", problem or "hotspots.geojson still holds placeholder data")

    features = []
    for feature in _features(data):
        props = feature.get("properties") or {}
        if barangay and props.get("barangay_id") != barangay:
            continue
        if severity and str(props.get("severity", "")).lower() != severity.lower():
            continue
        if (date_from or date_to) and not timeutil.in_range(
                props.get("last_reported"), date_from, date_to):
            continue
        if not _has_geometry(feature):
            continue
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {"source": "file", "enabled": True, "problem": None,
                 "total": len(features), "unplaced": 0},
    }


def _severity_for(hits: int) -> str:
    thresholds = Config.HOTSPOT_SEVERITY_THRESHOLDS
    if hits >= thresholds.get("high", 6):
        return "high"
    if hits >= thresholds.get("medium", 3):
        return "medium"
    return "low"


def _hotspots_derived(barangay, date_from, date_to, severity) -> dict:
    """
    Build hotspots from what the system already knows: Not Collected entries
    and resident reports, grouped by barangay + purok over a date window.

    Positioning is the honest limitation here. We have no purok geometry, so
    every purok in a barangay anchors to that barangay's MRF (or polygon
    centroid). Hotspots for a barangay we cannot place at all are dropped from
    `features` and counted in `meta.unplaced`, so the UI can report them
    instead of pretending they do not exist. Supplying purok centroids later
    upgrades this to true per-purok placement with no code change.
    """
    if not date_from and not date_to:
        date_to = timeutil.today()
        date_from = date_to - timedelta(days=Config.HOTSPOT_DEFAULT_WINDOW_DAYS)

    buckets: dict[tuple[str, str], dict] = {}

    def tally(barangay_id, purok, when, kind):
        if not barangay_id:
            return
        if barangay and barangay_id != barangay:
            return
        if not timeutil.in_range(when, date_from, date_to):
            return
        key = (barangay_id, purok or "Unspecified")
        bucket = buckets.setdefault(key, {
            "barangay_id": barangay_id, "purok": purok or "Unspecified",
            "not_collected": 0, "reports": 0, "last_reported": "",
        })
        bucket[kind] += 1
        stamp = timeutil.date_str(when)
        if stamp > bucket["last_reported"]:
            bucket["last_reported"] = stamp

    properties = {p.get("id"): p for p in storage.read("properties")}
    for row in storage.read("collections"):
        if row.get("status") != "Not Collected":
            continue
        prop = properties.get(row.get("property_id")) or {}
        tally(row.get("barangay_id") or prop.get("barangay_id"),
              row.get("purok") or prop.get("purok"), row.get("date"), "not_collected")

    for row in storage.read("public_reports"):
        if row.get("status_reported") != "Not Collected":
            continue
        tally(row.get("barangay_id"), row.get("purok"),
              row.get("date") or row.get("created_at"), "reports")

    names = {b.get("id"): b.get("name") for b in storage.read("barangays")}
    features, unplaced = [], 0
    for (barangay_id, purok), bucket in buckets.items():
        hits = bucket["not_collected"] + bucket["reports"]
        level = _severity_for(hits)
        if severity and level != severity.lower():
            continue
        point = point_for_barangay(barangay_id)
        if not point:
            unplaced += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [point[1], point[0]]},
            "properties": {
                "hotspot_id": f"derived:{barangay_id}:{purok}",
                "barangay_id": barangay_id,
                "barangay_name": names.get(barangay_id),
                "purok": purok,
                "severity": level,
                "type": "uncollected",
                "hits": hits,
                "not_collected": bucket["not_collected"],
                "reports": bucket["reports"],
                "last_reported": bucket["last_reported"],
                "notes": (f"{bucket['not_collected']} not-collected entries and "
                          f"{bucket['reports']} resident reports"),
                "anchored_to": "barangay",
            },
        })

    features.sort(key=lambda f: f["properties"]["hits"], reverse=True)
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "source": "derived", "enabled": True, "problem": None,
            "total": len(features), "unplaced": unplaced,
            "window": {"from": timeutil.date_str(date_from),
                       "to": timeutil.date_str(date_to)},
            "note": "Derived hotspots anchor to the barangay MRF; supply purok "
                    "centroids for per-purok placement.",
        },
    }


# ---------------------------------------------------------------------------
# Status, for the admin UI and DATA_REQUIREMENTS instructions
# ---------------------------------------------------------------------------

def status() -> dict:
    """A one-glance summary of which geo data is real and which is still stubbed."""
    zones = barangay_zones()
    mrfs = mrf_locations()
    spots = hotspots()
    return {
        "zones": {"loaded": zones["meta"]["with_geometry"],
                  "total": zones["meta"]["total"],
                  "placeholder": zones["meta"]["placeholder"],
                  "problem": zones["meta"]["problem"]},
        "mrfs": {"loaded": mrfs["meta"]["located"],
                 "total": mrfs["meta"]["total"],
                 "placeholder": mrfs["meta"]["placeholder"],
                 "problem": mrfs["meta"]["problem"]},
        "hotspots": {"enabled": Config.HOTSPOT_LAYER_ENABLED,
                     "source": Config.HOTSPOT_SOURCE,
                     "total": spots["meta"]["total"],
                     "problem": spots["meta"].get("problem")},
        "map": {"center": list(Config.MAP_DEFAULT_CENTER),
                "zoom": Config.MAP_DEFAULT_ZOOM,
                "tile_url": Config.MAP_TILE_URL,
                "attribution": Config.MAP_TILE_ATTRIBUTION},
    }
