"""
Collection entries -- what a tricycle collector recorded at one property on
one date.

The status model is the spine of the whole system (spec section 7):

    a property is **Pending** for a date simply because no record exists for
    that date. Saving an entry makes it Collected or Not Collected.

Nothing is reset at midnight. Every "today" view queries today's Manila date,
so yesterday keeps its own answer and history is automatic.

Loads are always totalled **sacks and kilos separately**. They are different
units; adding them produces a number that means nothing, and the mockup's
"43 sacks" against rows summing to 35 is exactly that mistake.
"""

import mimetypes
import secrets
from pathlib import Path

from config import Config
from services import schedule_service, storage, timeutil
from services.validation import ValidationError, Validator

COLLECTED = "Collected"
NOT_COLLECTED = "Not Collected"
PENDING = "Pending"
STATUSES = (COLLECTED, NOT_COLLECTED)

UNITS = ("Sack", "Kilo")

SOURCE_COLLECTOR = "collector"
SOURCE_PUBLIC = "public_report"

# Seed reasons from spec 6.4. Free text is not accepted: these feed the
# "Reason" column that admins filter and report on.
NOT_COLLECTED_REASONS = (
    "Not segregated properly",
    "No garbage taken out",
    "Road inaccessible",
    "Has special/hazardous waste",
    "No one at home",
    "Other",
)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def entry_for(property_id: str, date=None) -> dict | None:
    """The record for one property on one date, or None (= Pending)."""
    day = timeutil.date_str(date or timeutil.today())
    return storage.find_one("collections", property_id=property_id, date=day)


def status_for(property_id: str, date=None) -> str:
    entry = entry_for(property_id, date)
    return entry.get("status", PENDING) if entry else PENDING


def entries_for_date(date=None, barangay_id: str | None = None,
                     collector_id: str | None = None) -> list[dict]:
    day = timeutil.date_str(date or timeutil.today())
    return storage.find("collections", date=day, barangay_id=barangay_id,
                        collector_id=collector_id)


def route_with_status(properties: list[dict], date=None) -> list[dict]:
    """
    Properties decorated with their status for a date -- the collector's route
    list and the admin property tables.

    One pass over the day's entries, indexed by property, rather than a lookup
    per property: 1,500 properties would otherwise mean 1,500 file reads.
    """
    day = timeutil.date_str(date or timeutil.today())
    entries = {e["property_id"]: e for e in storage.find("collections", date=day)}

    out = []
    for prop in properties:
        entry = entries.get(prop["id"])
        out.append({
            **prop,
            "status": entry.get("status", PENDING) if entry else PENDING,
            "entry": entry,
            "entry_id": entry["id"] if entry else None,
            "time": timeutil.display_time(timeutil.parse_stamp(entry["timestamp"]))
                    if entry and entry.get("timestamp") else "",
            "load": entry.get("waste") or [] if entry else [],
            "reason": entry.get("reason") or "" if entry else "",
            "note": entry.get("note") or "" if entry else "",
            "disputed": bool(entry.get("disputed")) if entry else False,
            "source": entry.get("source") if entry else None,
        })
    return out


def counts(properties: list[dict], date=None) -> dict:
    """Collected / Pending / Not Collected against the registered total."""
    rows = route_with_status(properties, date)
    collected = sum(1 for r in rows if r["status"] == COLLECTED)
    not_collected = sum(1 for r in rows if r["status"] == NOT_COLLECTED)
    total = len(rows)
    return {
        "total": total,
        "collected": collected,
        "not_collected": not_collected,
        "pending": total - collected - not_collected,
        "disputed": sum(1 for r in rows if r["disputed"]),
        "percent": round(collected / total * 100) if total else 0,
    }


# ---------------------------------------------------------------------------
# Load totals
# ---------------------------------------------------------------------------

def totals(entries: list[dict]) -> dict:
    """
    Aggregate waste lines across entries, grouped by (type, unit).

    Sacks and kilos are summed into separate figures and never combined --
    "35 sacks and 5 kg", never "40".
    """
    buckets: dict[tuple[str, str], int] = {}
    for entry in entries:
        if entry.get("status") != COLLECTED:
            continue
        for line in entry.get("waste") or []:
            key = (line.get("type"), line.get("unit"))
            if not key[0] or not key[1]:
                continue
            buckets[key] = buckets.get(key, 0) + int(line.get("qty") or 0)

    lines = [{"label": label, "unit": unit, "value": qty,
              "display": format_unit(unit, qty)}
             for (label, unit), qty in sorted(buckets.items())]

    sacks = sum(q for (_, u), q in buckets.items() if u == "Sack")
    kilos = sum(q for (_, u), q in buckets.items() if u == "Kilo")

    parts = []
    if sacks:
        parts.append(f"{sacks} sack{'' if sacks == 1 else 's'}")
    if kilos:
        parts.append(f"{kilos} kg")

    return {"lines": lines, "by_type": group_by_type(lines),
            "sacks": sacks, "kilos": kilos,
            "total": " and ".join(parts) or "0", "empty": not lines}


def group_by_type(lines: list[dict]) -> list[dict]:
    """
    The same figures with one row per waste type instead of one per
    (type, unit).

    `lines` is keyed by unit because the chart needs sacks and kilos as
    separate series. A reader does not: a list that says "Bag -- 357 sacks"
    and then "Bag -- 227 kg" four rows later reads as two different wastes.
    Same numbers, still never added together, just gathered under one name.
    """
    grouped: dict[str, dict] = {}
    for line in lines:
        row = grouped.setdefault(line["label"], {
            "label": line["label"], "sacks": 0, "kilos": 0})
        if line["unit"] == "Kilo":
            row["kilos"] += int(line.get("value") or 0)
        else:
            row["sacks"] += int(line.get("value") or 0)

    for row in grouped.values():
        parts = []
        if row["sacks"]:
            parts.append(f"{row['sacks']} {format_unit('Sack', row['sacks'])}")
        if row["kilos"]:
            parts.append(f"{row['kilos']} kg")
        row["display"] = " · ".join(parts) or "0"

    return [grouped[label] for label in sorted(grouped)]


def format_unit(unit: str, qty: int) -> str:
    if unit == "Kilo":
        return "kg"
    return "sack" if qty == 1 else "sacks"


def barangay_totals(barangay_id: str, date=None) -> dict:
    """
    A barangay's Overall Collected Load for a date.

    This is the figure the truck sees on that barangay's MRF card, so it is
    computed from the collection entries rather than typed in twice.
    """
    return totals(entries_for_date(date, barangay_id=barangay_id))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def can_edit(entry: dict | None, user_id: str, today=None) -> bool:
    """
    A collector may correct their OWN entry, on the CURRENT day only.

    Mistakes happen in the field, so same-day correction is allowed; past days
    are locked, because rewriting them would silently change totals that have
    already been reported and frozen into history.
    """
    if not entry:
        return True
    if entry.get("collector_id") != user_id:
        return False
    return entry.get("date") == timeutil.date_str(today or timeutil.today())


def save_entry(form, files, property_record: dict, collector: dict,
               date=None) -> dict:
    """
    Record a stop. Creates the entry, or replaces today's if the collector is
    correcting their own.

    `collector` is the signed-in user's public view; the barangay, purok, and
    vehicle are taken from it and the property, never from the form.
    """
    day = timeutil.date_str(date or timeutil.today())
    v = Validator(form)

    status = v.choice("status", "Collection result", STATUSES)
    v.text("note", "Note", max_length=500)
    gps = _parse_gps(form.get("gps"))

    existing = entry_for(property_record["id"], day)
    if existing and not can_edit(existing, collector["id"], day):
        if existing.get("collector_id") != collector["id"]:
            raise ValidationError({"form": (
                "Another collector already recorded this property today. "
                "Ask the barangay admin to correct it.")})
        raise ValidationError({"form": (
            "Entries can only be corrected on the day they were made.")})

    waste, proof_path = [], existing.get("image_proof_path") if existing else None

    if status == COLLECTED:
        waste = _read_waste_lines(form, day, v)
        if not waste:
            v.fail("waste", "Record at least one waste quantity greater than zero.")
    else:
        v.choice("reason", "Reason", NOT_COLLECTED_REASONS)
        # Spec 6.4 requires photo evidence for a household refusal: it is what
        # the barangay admin reviews when a resident disputes the entry.
        upload = files.get("proof") if files else None
        if upload and getattr(upload, "filename", ""):
            try:
                proof_path = save_proof(upload, day)
            except ValidationError as exc:
                v.errors.update(exc.errors)
        elif not proof_path:
            v.fail("proof", "An image proof is required for a not-collected entry.")

    v.raise_if_invalid()

    payload = {
        "property_id": property_record["id"],
        "barangay_id": property_record.get("barangay_id"),
        "purok": property_record.get("purok"),
        "date": day,
        "status": status,
        "collector_id": collector["id"],
        "tricycle_code": collector.get("vehicle"),
        "gps": gps,
        "timestamp": timeutil.stamp(),
        "waste": waste,
        "reason": v.data.get("reason", "") if status == NOT_COLLECTED else "",
        "image_proof_path": proof_path if status == NOT_COLLECTED else None,
        "note": v.data["note"],
        "source": SOURCE_COLLECTOR,
        "disputed": False,
        "schedule_day": timeutil.weekday_name(day),
        "waste_type": schedule_service.for_date(day).get("short"),
    }

    from services import realtime

    if existing:
        saved = storage.update("collections", existing["id"], payload,
                               collector["id"])
    else:
        saved = storage.insert("collections", payload, collector["id"])

    realtime.collection_saved(saved)
    return saved


def _read_waste_lines(form, day, v: Validator) -> list[dict]:
    """
    Waste quantities, keyed by index so a type containing a slash or a space
    cannot collide with another field name.

    The offered types come from that day's schedule row, so a Tuesday entry
    offers recyclable categories rather than Kitchen Waste.
    """
    allowed = schedule_service.waste_types_for(day)
    lines = []

    for index, waste_type in enumerate(allowed):
        raw_qty = str(form.get(f"qty_{index}", "") or "0").strip()
        unit = str(form.get(f"unit_{index}", "Sack") or "Sack").strip()

        try:
            qty = int(float(raw_qty))
        except (TypeError, ValueError):
            v.fail(f"qty_{index}", f"{waste_type} quantity must be a whole number.")
            continue

        if qty < 0:
            v.fail(f"qty_{index}", f"{waste_type} quantity cannot be negative.")
            continue
        if qty > 999:
            v.fail(f"qty_{index}", f"{waste_type} quantity looks too large (max 999).")
            continue
        if unit not in UNITS:
            v.fail(f"unit_{index}", "Unit must be Sack or Kilo.")
            continue
        if qty > 0:
            lines.append({"type": waste_type, "unit": unit, "qty": qty})

    return lines


def _parse_gps(raw) -> dict | None:
    """
    Accept "lat,lng" from the browser Geolocation API. GPS is evidence, not
    input: a malformed value is dropped rather than guessed at.
    """
    if not raw:
        return None
    try:
        lat_s, lng_s = str(raw).split(",", 1)
        lat, lng = float(lat_s), float(lng_s)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {"lat": round(lat, 6), "lng": round(lng, 6)}


# ---------------------------------------------------------------------------
# Image proofs
# ---------------------------------------------------------------------------

def save_proof(upload, date=None) -> str:
    """
    Store an uploaded proof and return its relative path, 'YYYY-MM-DD/name.jpg'.

    Filenames are random, never derived from what the phone sent: an attacker
    controls that string, and a name like '../../app.py' would otherwise decide
    where the file lands.
    """
    day = timeutil.date_str(date or timeutil.today())
    filename = getattr(upload, "filename", "") or ""
    extension = Path(filename).suffix.lower()

    if extension == ".jpe":
        extension = ".jpeg"
    if extension not in Config.ALLOWED_PROOF_EXTENSIONS:
        raise ValidationError({"proof": "The image must be a JPG or PNG file."})

    declared = (getattr(upload, "mimetype", "") or "").lower()
    if declared and declared not in Config.ALLOWED_PROOF_TYPES:
        raise ValidationError({"proof": "The image must be a JPG or PNG file."})

    upload.stream.seek(0, 2)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size == 0:
        raise ValidationError({"proof": "That image file is empty."})
    if size > Config.MAX_PROOF_BYTES:
        megabytes = Config.MAX_PROOF_BYTES // (1024 * 1024)
        raise ValidationError(
            {"proof": f"That image is larger than {megabytes} MB. "
                      f"Try again -- the app shrinks photos before upload."})

    folder = Path(Config.UPLOAD_DIR) / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_urlsafe(16)}{extension}"
    upload.save(str(folder / name))

    return f"{day}/{name}"


def proof_path(relative: str) -> Path | None:
    """
    Resolve a stored proof path to a file on disk, refusing anything that
    escapes the upload directory.
    """
    if not relative:
        return None
    base = Path(Config.UPLOAD_DIR).resolve()
    try:
        candidate = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    if base not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def proof_mimetype(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def may_view_proof(entry: dict, user: dict) -> bool:
    """Admins and the collector who filed it. Nobody else (spec section 8)."""
    if not entry or not user:
        return False
    if user.get("role") == "city_admin":
        return True
    if user.get("role") == "barangay_admin":
        return entry.get("barangay_id") == user.get("barangay_id")
    return entry.get("collector_id") == user.get("id")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def history_for_collector(collector_id: str, date=None,
                          search: str = "") -> list[dict]:
    """A collector's own past entries as cards, newest first."""
    names = {p["id"]: p for p in storage.read("properties")}
    needle = (search or "").strip().lower()
    day = timeutil.date_str(date) if date else None

    rows = []
    for entry in storage.find("collections", collector_id=collector_id):
        if day and entry.get("date") != day:
            continue
        prop = names.get(entry.get("property_id")) or {}
        if needle and needle not in str(prop.get("owner_name", "")).lower():
            continue

        stamp = timeutil.parse_stamp(entry.get("timestamp"))
        rows.append({
            **entry,
            "owner_name": prop.get("owner_name") or "Deleted property",
            "property_type": prop.get("type") or "—",
            "purok": entry.get("purok") or prop.get("purok") or "—",
            "date_display": timeutil.display_date(entry.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "batch": f"{entry.get('schedule_day', '')} Batch".strip(),
            "load_display": totals([entry])["lines"],
        })

    rows.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""),
              reverse=True)
    return rows
