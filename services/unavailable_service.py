"""
"Unavailable for Duty" requests.

A collector files one when they cannot report; from that moment their
Availability reads Unavailable on the affected dates, and the request shows up
in the City Hall Admin's "Unavailable Requests" counter so the route can be
covered by a Temporary Replacement.

The request is Pending until an admin resolves it, or until its date range has
passed -- an old request should not keep inflating the counter forever.
"""

from services import storage, timeutil
from services.validation import ValidationError, Validator

PENDING = "Pending"
RESOLVED = "Resolved"

REASONS = (
    "Sick / medical leave",
    "Family emergency",
    "Vehicle breakdown",
    "Personal leave",
    "Other",
)


def create(form, user: dict) -> dict:
    v = Validator(form)

    affected = v.date("affected_date", "Affected date", required=True)
    until = v.date("unavailable_until", "Unavailable until")
    v.choice("reason", "Reason", REASONS)
    v.text("notes", "Notes", max_length=500)

    if affected and until and until < affected:
        v.fail("unavailable_until",
               "The end date cannot be before the affected date.")

    today = timeutil.today_str()
    if affected and affected < today:
        v.fail("affected_date", "Choose today or a future date.")

    if affected and _overlaps(user["id"], affected, until or affected):
        v.fail("affected_date",
               "You already have a pending request covering that date.")

    v.raise_if_invalid()

    record = storage.insert("unavailable_requests", {
        "user_id": user["id"],
        "user_name": user.get("name"),
        "role": user.get("role"),
        "role_label": user.get("role_label"),
        "barangay_id": user.get("barangay_id"),
        "affected_date": v.data["affected_date"],
        "unavailable_until": v.data["unavailable_until"] or None,
        "reason": v.data["reason"],
        "notes": v.data["notes"],
        "status": PENDING,
    }, user["id"])

    from services import triggers
    triggers.on_unavailable_request(record, user)
    return record


def _overlaps(user_id: str, start: str, end: str) -> bool:
    for row in storage.find("unavailable_requests", user_id=user_id, status=PENDING):
        other_start = row.get("affected_date")
        other_end = row.get("unavailable_until") or other_start
        if other_start and other_start <= end and start <= (other_end or other_start):
            return True
    return False


def for_user(user_id: str) -> list[dict]:
    rows = storage.find("unavailable_requests", user_id=user_id)
    rows.sort(key=lambda r: r.get("affected_date") or "", reverse=True)
    return [_decorate(r) for r in rows]


def pending(barangay_id: str | None = None) -> list[dict]:
    rows = [r for r in storage.read("unavailable_requests")
            if r.get("status") == PENDING and not _expired(r)]
    if barangay_id:
        rows = [r for r in rows if r.get("barangay_id") == barangay_id]
    rows.sort(key=lambda r: r.get("affected_date") or "")
    return [_decorate(r) for r in rows]


def resolve(request_id: str, actor: str) -> dict | None:
    return storage.update("unavailable_requests", request_id,
                          {"status": RESOLVED, "resolved_at": timeutil.stamp()},
                          actor)


def _expired(row: dict) -> bool:
    """Past its end date -- effectively resolved by time passing."""
    end = row.get("unavailable_until") or row.get("affected_date")
    return bool(end and end < timeutil.today_str())


def _decorate(row: dict) -> dict:
    end = row.get("unavailable_until") or row.get("affected_date")
    return {
        **row,
        "expired": _expired(row),
        "covers_today": bool(row.get("affected_date")
                             and row["affected_date"] <= timeutil.today_str()
                             <= (end or row["affected_date"])),
        "affected_display": timeutil.display_date(row.get("affected_date")),
        "until_display": (timeutil.display_date(row["unavailable_until"])
                          if row.get("unavailable_until") else "Same day only"),
    }
