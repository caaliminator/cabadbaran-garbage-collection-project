"""
Households and establishments -- the things a tricycle collector collects from.

Barangay admins own this list for their own barangay (Phase 5); collectors read
the slice of it that matches their assignment; the City Hall Admin sees all of
it. Every read here takes the scope as an argument rather than reaching for the
session, so the caller cannot accidentally widen it.

A property record carries no status. Status belongs to a *date* and lives in
`collections.json` -- see collection_service. That is what makes "Pending"
mean "no record for today" and history come out for free.
"""

from services import storage
from services.validation import ValidationError, Validator

PROPERTY_TYPES = ("House", "Establishment")

# Free-text tags the barangay admin can flag a property with, e.g. so a
# collector knows to expect special waste before they arrive.
COMMON_TAGS = ("None Composting", "Composting", "Has Special Waste",
               "No Special Waste", "Senior Citizen", "Business Permit Holder")


def barangay_name(barangay_id: str) -> str | None:
    row = storage.find_one("barangays", id=barangay_id)
    return row["name"] if row else None


def puroks_for(barangay_id: str) -> list[str]:
    row = storage.find_one("barangays", id=barangay_id)
    return list((row or {}).get("puroks") or [])


def get(property_id: str) -> dict | None:
    return storage.get("properties", property_id)


def listing(barangay_id: str | None = None, puroks: list[str] | None = None,
            property_type: str | None = None, search: str = "",
            newest_first: bool = True) -> list[dict]:
    """
    Properties matching a scope. `barangay_id=None` means citywide, which only
    the City Hall Admin's routes ever pass.
    """
    needle = (search or "").strip().lower()
    wanted_puroks = set(puroks) if puroks else None

    rows = []
    for row in storage.read("properties"):
        if barangay_id and row.get("barangay_id") != barangay_id:
            continue
        if wanted_puroks is not None and row.get("purok") not in wanted_puroks:
            continue
        if property_type and row.get("type") != property_type:
            continue
        if needle and needle not in str(row.get("owner_name", "")).lower():
            continue
        rows.append(row)

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=newest_first)
    return rows


def for_collector(assignment: dict | None) -> list[dict]:
    """
    The route list for one tricycle collector: their barangay, narrowed to the
    puroks they cover. No assignment means no route -- never the whole barangay.
    """
    if not assignment:
        return []
    return listing(barangay_id=assignment.get("barangay_id"),
                   puroks=assignment.get("purok_coverage") or [],
                   newest_first=False)


def count_for(barangay_id: str | None = None) -> int:
    """
    The denominator in the "collected / total" counters -- registered
    properties, per barangay
    or citywide.
    """
    if barangay_id:
        return storage.count("properties", barangay_id=barangay_id)
    return storage.count("properties")


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _validate(form, barangay_id: str) -> Validator:
    v = Validator(form)
    v.text("owner_name", "Owner name", required=True, max_length=120)
    v.choice("type", "Property type", PROPERTY_TYPES)
    v.choice("purok", "Purok", puroks_for(barangay_id))
    v.text("tag", "Tag", max_length=80)
    v.text("note", "Note", max_length=300)

    if not barangay_id:
        v.fail("barangay_id", "A barangay is required.")
    elif not storage.find_one("barangays", id=barangay_id):
        v.fail("barangay_id", "That barangay does not exist.")

    return v


def create(form, barangay_id: str, actor: str | None = None) -> dict:
    """
    `barangay_id` comes from the caller's scope, never from the form -- a
    barangay admin must not be able to file a property under someone else's
    barangay by editing a hidden input.
    """
    v = _validate(form, barangay_id)
    v.raise_if_invalid()

    return storage.insert("properties", {
        "owner_name": v.data["owner_name"],
        "type": v.data["type"],
        "barangay_id": barangay_id,
        "purok": v.data["purok"],
        "tag": v.data["tag"],
        "note": v.data["note"],
    }, actor)


def update(property_id: str, form, barangay_id: str,
           actor: str | None = None) -> dict:
    existing = get(property_id)
    if not existing:
        raise ValidationError({"form": "That property no longer exists."})
    if existing.get("barangay_id") != barangay_id:
        raise ValidationError({"form": "That property belongs to another barangay."})

    v = _validate(form, barangay_id)
    v.raise_if_invalid()

    return storage.update("properties", property_id, {
        "owner_name": v.data["owner_name"],
        "type": v.data["type"],
        "purok": v.data["purok"],
        "tag": v.data["tag"],
        "note": v.data["note"],
    }, actor)


def delete(property_id: str, barangay_id: str, actor: str | None = None) -> dict:
    existing = get(property_id)
    if not existing:
        raise ValidationError({"form": "That property no longer exists."})
    if existing.get("barangay_id") != barangay_id:
        raise ValidationError({"form": "That property belongs to another barangay."})

    # Collection records are deliberately left alone: they are the historical
    # account of what was collected on a date, and deleting a property today
    # must not rewrite last month's totals.
    return storage.delete("properties", property_id)
