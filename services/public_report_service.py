"""
Anonymous resident reports.

A resident says whether their waste was collected. No account, no name -- so
two things need care:

**Rate limiting.** Anything anonymous invites abuse, so a device may file at
most `Config.PUBLIC_REPORT_DAILY_LIMIT` reports per day. The limit is keyed on
a *salted hash* of the IP, never the IP itself: a raw address stored next to a
household's name and purok is identifying information the system has no need
to keep, and the hash enforces the limit just as well.

**Conflict handling.** A resident's "Not Collected" must never silently
overwrite a collector's "Collected" (spec section 7). When the two disagree,
both records are kept and the collection entry is flagged **Disputed** for the
barangay admin to resolve -- with the collector's photo proof already attached
for them to look at.
"""

import hashlib

from config import Config
from services import collection_service, storage, timeutil
from services.validation import ValidationError, Validator

COLLECTED = "Collected"
NOT_COLLECTED = "Not Collected"
STATUS_CHOICES = (COLLECTED, NOT_COLLECTED)


def _device_key(ip: str, fingerprint: str = "") -> str:
    """
    A stable, non-reversible identifier for the reporting device.

    Salted with SECRET_KEY so the hashes cannot be matched against a
    precomputed table of the whole IPv4 space -- an unsalted hash of an IP
    address is trivially reversible.
    """
    material = f"{Config.SECRET_KEY}|{ip or ''}|{fingerprint or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def reports_today(device_key: str, date=None) -> int:
    day = timeutil.date_str(date or timeutil.today())
    return storage.count("public_reports", device_key=device_key, date=day)


def remaining_today(ip: str, fingerprint: str = "", date=None) -> int:
    used = reports_today(_device_key(ip, fingerprint), date)
    return max(0, Config.PUBLIC_REPORT_DAILY_LIMIT - used)


# ---------------------------------------------------------------------------
# Options for the report form
# ---------------------------------------------------------------------------

def barangay_options() -> list[tuple[str, str]]:
    rows = sorted(storage.read("barangays"), key=lambda b: b.get("number", 0))
    return [(b["id"], b["name"]) for b in rows]


def purok_options(barangay_id: str) -> list[str]:
    """Only puroks that actually have a registered property."""
    if not barangay_id:
        return []
    found = {p.get("purok") for p in storage.find("properties", barangay_id=barangay_id)
             if p.get("purok")}
    order = (storage.find_one("barangays", id=barangay_id) or {}).get("puroks") or []
    known = [p for p in order if p in found]
    return known + sorted(found - set(known))


def property_options(barangay_id: str, purok: str = "",
                     search: str = "") -> list[dict]:
    """
    Properties a resident can pick from.

    Deliberately narrow: owner name, type, and purok only. The tag and note
    fields are operational notes for collectors, and this list is served to
    anyone.
    """
    if not barangay_id:
        return []
    needle = (search or "").strip().lower()

    rows = []
    for prop in storage.find("properties", barangay_id=barangay_id):
        if purok and prop.get("purok") != purok:
            continue
        if needle and needle not in str(prop.get("owner_name", "")).lower():
            continue
        rows.append({"id": prop["id"], "owner_name": prop.get("owner_name"),
                     "type": prop.get("type"), "purok": prop.get("purok")})

    rows.sort(key=lambda r: (r["purok"] or "", r["owner_name"] or ""))
    return rows


# ---------------------------------------------------------------------------
# Filing a report
# ---------------------------------------------------------------------------

def submit(form, ip: str, fingerprint: str = "", date=None) -> dict:
    """
    File a report. Returns the stored record, with `disputed` set when the
    resident disagrees with a collector's entry.
    """
    day = timeutil.date_str(date or timeutil.today())
    device_key = _device_key(ip, fingerprint)

    if reports_today(device_key, day) >= Config.PUBLIC_REPORT_DAILY_LIMIT:
        raise ValidationError({"form": (
            f"You have already sent {Config.PUBLIC_REPORT_DAILY_LIMIT} reports "
            f"today. Please try again tomorrow, or contact your barangay office "
            f"if something is urgent.")})

    v = Validator(form)
    barangay_id = v.choice("barangay_id", "Barangay",
                           [b["id"] for b in storage.read("barangays")])
    property_id = v.text("property_id", "Property", required=True)
    status = v.choice("status_reported", "Collection status", STATUS_CHOICES)
    v.text("comment", "Comment", max_length=500)

    prop = storage.get("properties", property_id) if property_id else None
    if property_id and not prop:
        v.fail("property_id", "Choose a property from the list.")
    elif prop and prop.get("barangay_id") != barangay_id:
        v.fail("property_id", "That property is not in the barangay you chose.")

    v.raise_if_invalid()

    entry = collection_service.entry_for(property_id, day)
    disputed = bool(entry and entry.get("status") != status
                    and entry.get("source") == collection_service.SOURCE_COLLECTOR)

    report = storage.insert("public_reports", {
        "barangay_id": barangay_id,
        "property_id": property_id,
        "purok": prop.get("purok"),
        "owner_name": prop.get("owner_name"),
        "date": day,
        "status_reported": status,
        "comment": v.data["comment"],
        "device_key": device_key,
        "disputed": disputed,
        "matched_entry_id": entry["id"] if entry else None,
    }, actor="public")

    _apply_to_collection(report, entry, prop, day)

    from services import triggers
    triggers.on_public_report(report)
    return report


def _apply_to_collection(report: dict, entry: dict | None, prop: dict, day: str) -> None:
    """
    Reflect the report onto the day's collection record.

    Three cases, and only the first one writes a status:

      no entry yet     -> create one, marked `source: public_report`, so the
                          admin can see it came from a resident
      entry agrees     -> leave it; just note that a resident confirmed it
      entry disagrees  -> **keep the collector's entry untouched** and flag it
                          Disputed. Overwriting would destroy the collector's
                          record, including their photo proof, on the word of
                          an anonymous form.
    """
    status = report["status_reported"]

    if not entry:
        storage.insert("collections", {
            "property_id": prop["id"],
            "barangay_id": prop.get("barangay_id"),
            "purok": prop.get("purok"),
            "date": day,
            "status": status,
            "collector_id": None,
            "tricycle_code": None,
            "gps": None,
            "timestamp": timeutil.stamp(),
            "waste": [],
            "reason": "Reported by resident" if status == NOT_COLLECTED else "",
            "image_proof_path": None,
            "note": report.get("comment", ""),
            "source": collection_service.SOURCE_PUBLIC,
            "disputed": False,
            "reported_by_resident": True,
            "schedule_day": timeutil.weekday_name(day),
        }, actor="public")
        return

    changes = {"reported_by_resident": True}
    if report["disputed"]:
        changes["disputed"] = True
        changes["dispute_note"] = (
            f"Resident reported {status} on {timeutil.display_date(day)}; "
            f"the collector recorded {entry.get('status')}.")
    storage.update("collections", entry["id"], changes, actor="public")


# ---------------------------------------------------------------------------
# Admin views
# ---------------------------------------------------------------------------

def listing(barangay_id: str | None = None, date=None,
            disputed_only: bool = False) -> list[dict]:
    day = timeutil.date_str(date) if date else None

    rows = []
    for row in storage.read("public_reports"):
        if barangay_id and row.get("barangay_id") != barangay_id:
            continue
        if day and row.get("date") != day:
            continue
        if disputed_only and not row.get("disputed"):
            continue
        stamp = timeutil.parse_stamp(row.get("created_at"))
        rows.append({
            **row,
            # The device key never leaves the service layer: it is a rate-limit
            # mechanism, not something to show an admin.
            "device_key": None,
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "date_display": timeutil.display_date(row.get("date")),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def dispute_count(barangay_id: str | None = None, date=None) -> int:
    day = timeutil.date_str(date or timeutil.today())
    return sum(1 for r in storage.find("collections", date=day,
                                       barangay_id=barangay_id)
               if r.get("disputed"))


def resolve_dispute(entry_id: str, keep: str, actor: str) -> dict:
    """
    A barangay admin settles a dispute by choosing which account stands.

    `keep` is 'collector' (clear the flag, leave the entry) or 'resident'
    (switch the status, recording who changed it and why).
    """
    entry = storage.get("collections", entry_id)
    if not entry:
        raise ValidationError({"form": "That collection record no longer exists."})
    if not entry.get("disputed"):
        raise ValidationError({"form": "That record is not disputed."})

    if keep == "collector":
        return storage.update("collections", entry_id, {
            "disputed": False,
            "dispute_resolution": "Collector's record upheld.",
        }, actor)

    if keep == "resident":
        flipped = (NOT_COLLECTED if entry.get("status") == COLLECTED else COLLECTED)
        return storage.update("collections", entry_id, {
            "status": flipped,
            "disputed": False,
            "waste": [] if flipped == NOT_COLLECTED else entry.get("waste"),
            "reason": ("Resident report upheld by barangay admin"
                       if flipped == NOT_COLLECTED else ""),
            "dispute_resolution": "Resident's report upheld.",
        }, actor)

    raise ValidationError({"form": "Choose whose record stands."})
