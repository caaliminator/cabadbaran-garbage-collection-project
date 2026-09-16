"""
"Unavailable for Duty" requests.

A collector files one when they cannot report. It lands on the City Hall
Admin's Unavailability worklist, where it is approved or rejected, and an
approved one can have a Temporary Replacement attached to it so there is a
record of who actually covered the route.

The lifecycle:

    collector files          -> Pending
    admin approves           -> Approved   (collector reads Unavailable)
    admin rejects            -> Rejected   (collector stays Available)
    admin assigns a stand-in -> replacement recorded on the request

A Pending request already marks the collector Unavailable. That is deliberate:
between filing and the decision the route is genuinely at risk, and the
Availability column exists to show that risk rather than to record a verdict.
Rejecting is what puts them back on the route.

`Resolved` is the status this used to end in, before approve/reject existed.
Old records keep it and are read as closed -- no migration, and nothing
silently changes meaning underneath a request someone already actioned.
"""

from services import storage, timeutil
from services.validation import ValidationError, Validator

PENDING = "Pending"
APPROVED = "Approved"
REJECTED = "Rejected"
RESOLVED = "Resolved"        # legacy, pre-decision records

STATUS_CHOICES = (PENDING, APPROVED, REJECTED)

# Statuses that keep a collector off the route. A rejected request must not,
# which is the whole point of being able to reject one.
BLOCKING = (PENDING, APPROVED)

# Terminal: the admin has said something, so it leaves the worklist.
DECIDED = (APPROVED, REJECTED, RESOLVED)

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


def get(request_id: str) -> dict | None:
    row = storage.get("unavailable_requests", request_id)
    return _decorate(row) if row else None


def listing(status: str = "", role: str = "", search: str = "") -> list[dict]:
    """
    The admin worklist. Everything by default -- a rejected request is part of
    the record and has to stay readable, so nothing is filtered out unless the
    admin asks for a status.

    Ordered by the date the collector cannot work, soonest first: the request
    for tomorrow needs a decision before the one for next month.
    """
    needle = (search or "").strip().lower()

    rows = []
    for row in storage.read("unavailable_requests"):
        if status and row.get("status") != status:
            continue
        if role and row.get("role") != role:
            continue
        if needle and needle not in str(row.get("user_name", "")).lower():
            continue
        rows.append(_decorate(row))

    rows.sort(key=lambda r: (r["status"] != PENDING,
                             r.get("affected_date") or ""))
    return rows


def counts() -> dict:
    rows = [_decorate(r) for r in storage.read("unavailable_requests")]
    return {
        "pending": sum(1 for r in rows if r["status"] == PENDING and not r["expired"]),
        "approved": sum(1 for r in rows if r["status"] == APPROVED),
        "rejected": sum(1 for r in rows if r["status"] == REJECTED),
        "covering_today": sum(1 for r in rows
                              if r["covers_today"] and r["status"] in BLOCKING),
        "unassigned": sum(1 for r in rows if r["status"] == APPROVED
                          and r["covers_today"] and not r.get("replacement")),
        "total": len(rows),
    }


def decide(request_id: str, decision: str, actor: str,
           note: str = "") -> dict:
    """
    Approve or reject. Recorded with who and when, because a collector being
    off the route on a given day is the kind of thing that gets questioned
    later and the answer should not be "someone changed it".
    """
    if decision not in (APPROVED, REJECTED):
        raise ValidationError({"form": "Choose either approve or reject."})

    row = storage.get("unavailable_requests", request_id)
    if not row:
        raise ValidationError({"form": "That request no longer exists."})
    if row.get("status") in DECIDED:
        raise ValidationError(
            {"form": f"That request was already {str(row['status']).lower()}."})

    updated = storage.update("unavailable_requests", request_id, {
        "status": decision,
        "decided_by": actor,
        "decided_at": timeutil.stamp(),
        "decision_note": (note or "").strip()[:500],
        # Kept for anything still reading the old field name.
        "resolved_at": timeutil.stamp(),
    }, actor)

    from services import triggers
    triggers.on_unavailable_decided(updated)
    return updated


def link_replacement(request_id: str, kind: str, assignment_id: str,
                     actor: str) -> dict | None:
    """
    Record which Temporary Replacement covers this absence.

    Stored on the request rather than derived by matching dates and barangays:
    the admin's intent is the fact worth keeping, and two overlapping
    assignments would make a derived answer a guess.
    """
    row = storage.get("unavailable_requests", request_id)
    if not row:
        return None
    return storage.update("unavailable_requests", request_id, {
        "replacement": {"kind": kind, "assignment_id": assignment_id},
    }, actor)


def resolve(request_id: str, actor: str) -> dict | None:
    """Legacy close, kept for callers that predate approve/reject."""
    return storage.update("unavailable_requests", request_id,
                          {"status": RESOLVED, "resolved_at": timeutil.stamp()},
                          actor)


def _expired(row: dict) -> bool:
    """Past its end date -- effectively resolved by time passing."""
    end = row.get("unavailable_until") or row.get("affected_date")
    return bool(end and end < timeutil.today_str())


def _decorate(row: dict) -> dict:
    end = row.get("unavailable_until") or row.get("affected_date")
    status = row.get("status") or PENDING
    stamp = timeutil.parse_stamp(row.get("decided_at"))

    # Name and role are denormalised onto the request when it is filed, but
    # records written before that (and by the demo tooling) have neither. Fall
    # back to the account so an old request still reads as a person.
    who = (storage.get("users", row.get("user_id")) or {}
           if not (row.get("user_name") and row.get("role_label")) else {})

    return {
        **row,
        "status": status,
        "user_name": row.get("user_name") or who.get("full_name") or "Deleted account",
        "role": row.get("role") or who.get("role") or "",
        "role_label": row.get("role_label") or _role_label(who.get("role")) or "—",
        "expired": _expired(row),
        "covers_today": bool(row.get("affected_date")
                             and row["affected_date"] <= timeutil.today_str()
                             <= (end or row["affected_date"])),
        "decided": status in DECIDED,
        "blocking": status in BLOCKING,
        "affected_display": timeutil.display_date(row.get("affected_date")),
        "until_display": (timeutil.display_date(row["unavailable_until"])
                          if row.get("unavailable_until") else "Same day only"),
        "end_date": end,
        "days": _day_count(row.get("affected_date"), end),
        "decided_by_name": _actor_name(row.get("decided_by")),
        "decided_display": (f"{timeutil.display_date(stamp)} · "
                            f"{timeutil.display_time(stamp)}") if stamp else "—",
        "replacement_detail": _replacement_detail(row.get("replacement")),
    }


def _role_label(role) -> str:
    return {"tricycle_collector": "Tricycle Garbage Collector",
            "truck_collector": "Truck Garbage Collector"}.get(role, "")


def _day_count(start, end) -> int:
    first, last = timeutil.to_date(start), timeutil.to_date(end or start)
    if not first or not last or last < first:
        return 0
    return (last - first).days + 1


def _actor_name(user_id) -> str:
    if not user_id:
        return "—"
    row = storage.get("users", user_id)
    return (row or {}).get("full_name") or "—"


def _replacement_detail(link) -> dict | None:
    """
    Resolve the stored assignment id into something a table can show. Returns
    None when nothing is attached, or when the assignment has since been
    deleted -- an id pointing at nothing should read as "no replacement", not
    as a broken row.
    """
    if not link or not link.get("assignment_id"):
        return None

    from services import assignment_service
    collection = (assignment_service.TRICYCLE_COLLECTION
                  if link.get("kind") == "tricycle"
                  else assignment_service.TRUCK_COLLECTION)
    row = storage.get(collection, link["assignment_id"])
    if not row:
        return None

    who = storage.get("users", row.get("collector_id") or row.get("operator_id"))
    return {
        "kind": link.get("kind"),
        "assignment_id": row["id"],
        "name": (who or {}).get("full_name") or "Deleted account",
        "vehicle": row.get("tricycle_code") or row.get("truck_code") or "—",
        "status": row.get("status"),
        "from_display": timeutil.display_date(row.get("effective_date")),
        "until_display": (timeutil.display_date(row["until_date"])
                          if row.get("until_date") else "No end date"),
    }
