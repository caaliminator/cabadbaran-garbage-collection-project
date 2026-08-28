"""
Carry-overs -- MRF loads that were not picked up and must be collected later.

The lifecycle from spec section 7:

    truck marks Not Collected
      -> carry_over(Pending, current_truck = null)
      -> admin Reassign and/or Reschedule
      -> the assigned truck sees it as a pending pickup on the new date
      -> marking it Collected closes the carry-over, and its load joins that
         day's totals

The load itself never moves anywhere: it stays in the barangay MRF. A
carry-over is the *record* that it is overdue, so it cannot be quietly
forgotten. That is why closing one is driven by the next successful pickup of
that barangay rather than by an admin ticking it off.
"""

from services import storage, timeutil

PENDING = "Pending"
COLLECTED = "Collected"


def open_for(pickup: dict, actor: str | None = None) -> dict | None:
    """
    Open a carry-over for a missed pickup, unless one is already outstanding
    for that barangay.

    One open carry-over per barangay is deliberate: three consecutive misses
    are one overdue load that has grown, not three separate loads, and three
    rows would make the admin chase the same waste three times.
    """
    barangay_id = pickup.get("barangay_id")
    existing = outstanding_for(barangay_id)

    if existing:
        return storage.update("carry_overs", existing["id"], {
            "waste": pickup.get("load"),
            "missed_count": (existing.get("missed_count") or 1) + 1,
            "last_missed_date": pickup.get("date"),
            "last_pickup_id": pickup.get("id"),
        }, actor)

    return storage.insert("carry_overs", {
        "barangay_id": barangay_id,
        "source_schedule_day": pickup.get("source_schedule_day"),
        "waste_type": pickup.get("waste_type"),
        "waste": pickup.get("load"),
        "original_truck": pickup.get("truck_code"),
        "current_truck": None,
        "status": PENDING,
        "reschedule_date": None,
        "missed_count": 1,
        "first_missed_date": pickup.get("date"),
        "last_missed_date": pickup.get("date"),
        "last_pickup_id": pickup.get("id"),
        "reason": pickup.get("reason"),
    }, actor)


def close_for(barangay_id: str, pickup: dict, actor: str | None = None) -> dict | None:
    """A successful pickup closes whatever was outstanding for that barangay."""
    existing = outstanding_for(barangay_id)
    if not existing:
        return None
    return storage.update("carry_overs", existing["id"], {
        "status": COLLECTED,
        "collected_date": pickup.get("date"),
        "collected_by_pickup": pickup.get("id"),
        "current_truck": pickup.get("truck_code") or existing.get("current_truck"),
    }, actor)


def outstanding_for(barangay_id: str) -> dict | None:
    rows = [r for r in storage.find("carry_overs", barangay_id=barangay_id)
            if r.get("status") == PENDING]
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[0] if rows else None


def pending_for_truck(truck_code: str, date=None) -> list[dict]:
    """
    Carry-overs a given truck should see as an extra stop today: reassigned to
    it, and either due today or already overdue.
    """
    if not truck_code:
        return []
    day = timeutil.date_str(date or timeutil.today())

    rows = []
    for row in storage.read("carry_overs"):
        if row.get("status") != PENDING or row.get("current_truck") != truck_code:
            continue
        due = row.get("reschedule_date")
        if due and due > day:
            continue        # scheduled for a later date
        rows.append(row)
    return rows


def listing(status: str = "", barangay_id: str = "") -> list[dict]:
    """
    The carry-over worklist. Open records only, unless a status is asked for.

    A carry-over exists to stop an overdue load being forgotten. Once it is
    collected it has served that purpose, and leaving it in the list makes the
    page read as a growing backlog when nothing is actually outstanding -- the
    admin ends up scanning past closed rows to find the ones that need a truck.

    Closed records are kept, not deleted: they are the evidence that the load
    was eventually taken, and the "Closed" counter and the Collected filter
    both still reach them.
    """
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    today = timeutil.today_str()

    rows = []
    for row in storage.read("carry_overs"):
        if status:
            if row.get("status") != status:
                continue
        elif row.get("status") != PENDING:
            continue
        if barangay_id and row.get("barangay_id") != barangay_id:
            continue

        due = row.get("reschedule_date")
        rows.append({
            **row,
            "barangay": names.get(row.get("barangay_id")) or "—",
            "current_truck_display": row.get("current_truck") or "—",
            "reschedule_display": (timeutil.display_date(due) if due
                                   else "Not scheduled"),
            "overdue": bool(row.get("status") == PENDING and due and due < today),
            "due_today": bool(due == today),
            "lines": (row.get("waste") or {}).get("lines") or [],
            "total": (row.get("waste") or {}).get("total") or "0",
            "first_missed_display": timeutil.display_date(row.get("first_missed_date")),
        })

    rows.sort(key=lambda r: (r.get("status") != PENDING,
                             r.get("created_at") or ""), reverse=False)
    return rows


def counts() -> dict:
    rows = storage.read("carry_overs")
    pending = [r for r in rows if r.get("status") == PENDING]
    today = timeutil.today_str()
    return {
        "total": len(rows),
        "pending": len(pending),
        "unassigned": sum(1 for r in pending if not r.get("current_truck")),
        "overdue": sum(1 for r in pending
                       if r.get("reschedule_date") and r["reschedule_date"] < today),
        "collected": sum(1 for r in rows if r.get("status") == COLLECTED),
    }


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------

def reassign(carry_over_id: str, truck_code: str, actor: str) -> dict:
    from services import vehicle_service
    from services.validation import ValidationError

    row = storage.get("carry_overs", carry_over_id)
    if not row:
        raise ValidationError({"form": "That carry-over no longer exists."})
    if row.get("status") != PENDING:
        raise ValidationError({"form": "That carry-over has already been collected."})
    if truck_code not in vehicle_service.in_service(vehicle_service.TRUCK):
        raise ValidationError({"truck": "Choose a truck that is in service."})

    updated = storage.update("carry_overs", carry_over_id,
                             {"current_truck": truck_code}, actor)

    from services import triggers
    triggers.on_carry_over_reassigned(updated, truck_code)
    return updated


def reschedule(carry_over_id: str, date, actor: str) -> dict:
    from services.validation import ValidationError

    row = storage.get("carry_overs", carry_over_id)
    if not row:
        raise ValidationError({"form": "That carry-over no longer exists."})
    if row.get("status") != PENDING:
        raise ValidationError({"form": "That carry-over has already been collected."})

    parsed = timeutil.to_date(date)
    if not parsed:
        raise ValidationError({"reschedule_date": "Choose a valid date."})
    if timeutil.date_str(parsed) < timeutil.today_str():
        raise ValidationError({"reschedule_date": "Choose today or a later date."})

    return storage.update("carry_overs", carry_over_id,
                          {"reschedule_date": timeutil.date_str(parsed)}, actor)
