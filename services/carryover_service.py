"""
Carry-overs -- MRF loads that were not picked up and must be collected later.

The lifecycle from spec section 7, in three stages:

    truck marks Not Collected
      -> Missed Collection   (no truck, no date -- nobody has acted yet)
      -> admin Reassign AND Reschedule
      -> Pending             (a named truck on a named date; it will be got)
      -> that truck collects the MRF
      -> Collected           (closed by the pickup itself, not by a tick-box)

The three stages are the admin's worklist in order: **Missed Collection** is
work waiting on a decision, **Pending** is work already arranged, **Collected**
is the archive. Splitting the first two matters because they need different
things from the admin -- a missed load needs assigning, a pending one only
needs watching -- and a single "open" list hid the difference.

A load is only Pending once it has *both* a truck and a date: a truck with no
date nobody is coming on, and a date with no truck nobody is coming for. Until
both are set the row stays in Missed Collection, where it is still being
chased. Because the stage is derived from those two fields (see `stage_of`),
it can never drift from them, and rows written before this split read
correctly without a migration.

The load itself never moves anywhere: it stays in the barangay MRF. A
carry-over is the *record* that it is overdue, so it cannot be quietly
forgotten. That is why closing one is driven by the next successful pickup of
that barangay rather than by an admin ticking it off.
"""

from services import storage, timeutil

MISSED = "Missed Collection"
PENDING = "Pending"
COLLECTED = "Collected"

# The two stages that still owe the city a pickup.
OPEN_STATUSES = (MISSED, PENDING)
STATUSES = (MISSED, PENDING, COLLECTED)


def stage_of(row: dict) -> str:
    """
    Which of the three stages a record is in.

    Derived rather than trusted: `status` is written alongside every change,
    but the truck and the date are what actually decide whether anyone is
    coming, and deriving from them means a row can never show Pending with
    nothing arranged. It also reads legacy rows -- which only ever stored
    "Pending" -- at the right stage.
    """
    if row.get("status") == COLLECTED:
        return COLLECTED
    return (PENDING if row.get("current_truck") and row.get("reschedule_date")
            else MISSED)


def is_open(row: dict) -> bool:
    return row.get("status") != COLLECTED


def missing_from(row: dict) -> str:
    """
    What a carry-over still needs before anyone is coming for it, in words.

    Empty for a row that is arranged or already collected. This is the whole
    difference between the two open stages, so it is written once here and
    read by the table, the dialog and the page notice alike -- three places
    that would otherwise each have their own idea of what "not ready" means.
    """
    if not is_open(row):
        return ""
    has_truck, has_date = bool(row.get("current_truck")), bool(row.get("reschedule_date"))
    if has_truck and has_date:
        return ""
    if not has_truck and not has_date:
        return "Needs a truck and a collection date"
    return "Needs a collection date" if has_truck else "Needs a truck"


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
        # A miss on the rescheduled day means the arrangement did not hold, so
        # the row goes back to Missed Collection and needs a new date. The
        # truck is kept -- it is still the one that knows this barangay -- but
        # the spent date is cleared rather than left behind looking overdue.
        return storage.update("carry_overs", existing["id"], {
            "waste": pickup.get("load"),
            "missed_count": (existing.get("missed_count") or 1) + 1,
            "last_missed_date": pickup.get("date"),
            "last_pickup_id": pickup.get("id"),
            "misses": (existing.get("misses") or []) + [_miss(pickup)],
            "reschedule_date": None,
            "status": MISSED,
        }, actor)

    return storage.insert("carry_overs", {
        "barangay_id": barangay_id,
        "source_schedule_day": pickup.get("source_schedule_day"),
        "waste_type": pickup.get("waste_type"),
        "waste": pickup.get("load"),
        "original_truck": pickup.get("truck_code"),
        "current_truck": None,
        "status": MISSED,
        "reschedule_date": None,
        "missed_count": 1,
        "first_missed_date": pickup.get("date"),
        "last_missed_date": pickup.get("date"),
        "first_pickup_id": pickup.get("id"),
        "last_pickup_id": pickup.get("id"),
        # Every attempt, in order, so the detail dialog can say "1st miss,
        # 2nd miss" with the date of each rather than only how many there were.
        "misses": [_miss(pickup)],
        "reason": pickup.get("reason"),
    }, actor)


def _miss(pickup: dict) -> dict:
    """One failed attempt, kept as its own small record."""
    return {
        "date": pickup.get("date"),
        "pickup_id": pickup.get("id"),
        "truck_code": pickup.get("truck_code"),
        "operator_id": pickup.get("operator_id"),
        "reason": pickup.get("reason"),
    }


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
            if is_open(r)]
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[0] if rows else None


def pending_for_truck(truck_code: str, date=None) -> list[dict]:
    """
    Carry-overs a given truck should see as an extra stop today: reassigned to
    it, and either due today or already overdue.

    The stage is not part of the test. What puts the stop on a truck's list is
    that the load is still owed and the truck's name is on it -- a row with no
    date yet reads as "as soon as you can", which is what a missed load with an
    assigned truck means.
    """
    if not truck_code:
        return []
    day = timeutil.date_str(date or timeutil.today())

    rows = []
    for row in storage.read("carry_overs"):
        if not is_open(row) or row.get("current_truck") != truck_code:
            continue
        due = row.get("reschedule_date")
        if due and due > day:
            continue        # scheduled for a later date
        rows.append(row)
    return rows


def listing(status: str = "", barangay_id: str = "") -> list[dict]:
    """
    One stage of the worklist: Missed Collection, Pending, or Collected.
    Anything else -- including a blank -- gives Missed Collection, the stage
    that is waiting on the admin.

    The three are separate views rather than one list with a column, because
    they are read for different reasons: the missed ones to act on, the pending
    ones to check nothing has slipped, the collected ones to prove the load was
    eventually taken. Mixed together, the page read as a growing backlog when
    nothing was actually outstanding.

    Collected records are kept, never deleted: they are that evidence, and the
    Collected view is what reaches them.
    """
    wanted = status if status in STATUSES else MISSED
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    today = timeutil.today_str()

    rows = []
    for row in storage.read("carry_overs"):
        stage = stage_of(row)
        if stage != wanted:
            continue
        if barangay_id and row.get("barangay_id") != barangay_id:
            continue

        due = row.get("reschedule_date")
        rows.append({
            **row,
            "status": stage,
            "barangay": names.get(row.get("barangay_id")) or "—",
            "current_truck_display": row.get("current_truck") or "—",
            "reschedule_display": (timeutil.display_date(due) if due
                                   else "Not scheduled"),
            "overdue": bool(stage != COLLECTED and due and due < today),
            "needs_truck": bool(stage == MISSED and not row.get("current_truck")),
            "needs_date": bool(stage == MISSED and not row.get("reschedule_date")),
            "missing": missing_from(row),
            "due_today": bool(due == today),
            "lines": (row.get("waste") or {}).get("lines") or [],
            "total": (row.get("waste") or {}).get("total") or "0",
            "first_missed_display": timeutil.display_date(row.get("first_missed_date")),
            "detail": detail(row),
        })

    # Oldest first inside a stage: the load that has been waiting longest is
    # the one to deal with first.
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows


def misses_of(row: dict) -> list[dict]:
    """
    Every missed attempt, oldest first, each labelled 1st / 2nd / 3rd.

    Rows written before `misses` existed are rebuilt from the first and last
    missed dates they do carry, so an old carry-over still shows a history
    rather than an empty list.
    """
    recorded = row.get("misses")
    if not recorded:
        dates = [d for d in (row.get("first_missed_date"),
                             row.get("last_missed_date")) if d]
        recorded = [{"date": d} for d in sorted(set(dates))]

    out = []
    for index, miss in enumerate(recorded, start=1):
        out.append({
            **miss,
            "label": f"{index}{_ordinal_suffix(index)} miss",
            "date_display": timeutil.display_date(miss.get("date")),
        })
    return out


def _ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def detail(row: dict) -> dict:
    """
    Everything a View Details dialog shows, resolved for the stage the row is
    in. Each stage answers a different question, so each carries a different
    set of fields:

      Missed Collection -- why was it not collected? The failed attempt:
                           when it was tried, by whom, the reason, the place.
      Pending           -- what has been arranged? The new date and the two
                           trucks, plus how many times it has been missed.
      Collected         -- what finally happened? The successful pickup, which
                           is the same set of fields the MRF page shows for it,
                           because it is the same pickup record.

    The pickup-derived fields all come from `mrf_service.pickup_view`, so a
    collected carry-over and its MRF row cannot disagree.
    """
    from services import mrf_service

    stage = stage_of(row)
    names = {b["id"]: b for b in storage.read("barangays")}
    barangay = names.get(row.get("barangay_id")) or {}
    operators = mrf_service.operator_names()

    def pickup(pickup_id):
        return (storage.get("mrf_pickups", pickup_id) if pickup_id else None)

    misses = misses_of(row)
    last_miss = pickup(row.get("last_pickup_id"))
    first_miss = pickup(row.get("first_pickup_id")) or last_miss
    closing = pickup(row.get("collected_by_pickup"))

    # Which pickup this stage is about: the attempt that failed, or the one
    # that finally succeeded.
    source = closing if stage == COLLECTED else last_miss
    view = mrf_service.pickup_view(source, row.get("barangay_id"))

    def with_operator(truck_code, operator_id):
        """"TRK-02 — Juan Dela Cruz", or just the code when nobody is known."""
        name = operators.get(operator_id)
        if not truck_code:
            return "Not assigned"
        return f"{truck_code} — {name}" if name else truck_code

    # From the carry-over's own record of the first attempt rather than the
    # pickup row: when another truck takes over a missed pickup on the same
    # day, that row is rewritten with the collecting truck's operator, and
    # reading it here would rename the truck that actually missed it.
    original_operator = ((misses[0].get("operator_id") if misses else None)
                         or (first_miss or {}).get("operator_id"))
    current_operator = ((closing or {}).get("operator_id")
                        or _assigned_operator(row.get("current_truck")))

    return {
        **view,
        "id": row.get("id"),
        "stage": stage,
        "status": stage,
        "barangay": barangay.get("name") or row.get("barangay_id"),
        "mrf_name": barangay.get("mrf_name") or f"{barangay.get('name', '')} MRF",
        # The carry-over's own record of the waste, which survives even when
        # no pickup is attached to read it from.
        "source_schedule_day": (view.get("source_schedule_day")
                                if view.get("recorded")
                                else row.get("source_schedule_day") or "—"),
        "waste_type": (view.get("waste_type") if view.get("recorded")
                       else row.get("waste_type") or "—"),
        "load": (view.get("load") if stage == COLLECTED
                 else {"total": "0", "lines": [], "empty": True}),
        "original_truck": with_operator(row.get("original_truck"), original_operator),
        "current_truck": with_operator(row.get("current_truck"), current_operator),
        "assigned_truck": with_operator(
            row.get("current_truck") or row.get("original_truck"),
            current_operator or original_operator),
        "reschedule_display": (timeutil.display_date(row["reschedule_date"])
                               if row.get("reschedule_date") else "Not scheduled"),
        "collected_display": (timeutil.display_date(row["collected_date"])
                              if row.get("collected_date") else "—"),
        "missing": missing_from(row),
        "misses": misses,
        "missed_count": row.get("missed_count") or len(misses) or 1,
        "misses_display": " · ".join(f"{m['label']} — {m['date_display']}"
                                     for m in misses) or "—",
        "first_missed_display": timeutil.display_date(row.get("first_missed_date")),
        "reason": view.get("reason") or row.get("reason") or "",
    }


def _assigned_operator(truck_code: str | None) -> str | None:
    """
    Who drives a truck today, for a carry-over that has not been collected.

    `operator_id`, not `collector_id`: that is the truck assignment's own
    field -- `collector_id` belongs to the tricycle assignments and is always
    absent here, which silently dropped the operator's name from every truck
    that had not collected yet.
    """
    if not truck_code:
        return None
    from services import assignment_service
    for assignment in storage.read(assignment_service.TRUCK_COLLECTION):
        if (assignment_service.is_active(assignment)
                and assignment.get("truck_code") == truck_code):
            return assignment.get("operator_id")
    return None


def counts() -> dict:
    rows = storage.read("carry_overs")
    stages = [(r, stage_of(r)) for r in rows]
    today = timeutil.today_str()

    missed = [r for r, stage in stages if stage == MISSED]
    pending = [r for r, stage in stages if stage == PENDING]

    return {
        "total": len(rows),
        "missed": len(missed),
        "pending": len(pending),
        # Still owed either way -- what the barangay is actually waiting for.
        "open": len(missed) + len(pending),
        "unassigned": sum(1 for r in missed if not r.get("current_truck")),
        "undated": sum(1 for r in missed if not r.get("reschedule_date")),
        "overdue": sum(1 for r in pending
                       if r.get("reschedule_date") and r["reschedule_date"] < today),
        "collected": sum(1 for r, stage in stages if stage == COLLECTED),
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
    if not is_open(row):
        raise ValidationError({"form": "That carry-over has already been collected."})
    if truck_code not in vehicle_service.in_service(vehicle_service.TRUCK):
        raise ValidationError({"truck": "Choose a truck that is in service."})

    updated = _restage(row, {"current_truck": truck_code}, actor)

    from services import triggers
    triggers.on_carry_over_reassigned(updated, truck_code)
    return updated


def reschedule(carry_over_id: str, date, actor: str) -> dict:
    from services.validation import ValidationError

    row = storage.get("carry_overs", carry_over_id)
    if not row:
        raise ValidationError({"form": "That carry-over no longer exists."})
    if not is_open(row):
        raise ValidationError({"form": "That carry-over has already been collected."})

    parsed = timeutil.to_date(date)
    if not parsed:
        raise ValidationError({"reschedule_date": "Choose a valid date."})
    if timeutil.date_str(parsed) < timeutil.today_str():
        raise ValidationError({"reschedule_date": "Choose today or a later date."})

    updated = _restage(row, {"reschedule_date": timeutil.date_str(parsed)}, actor)

    # The operator holding this stop has to hear that the day moved. Without
    # this, a reassignment was announced and the date it actually happens on
    # was not, which is the half of the arrangement they act on.
    from services import triggers
    triggers.on_carry_over_rescheduled(updated)
    return updated


def _restage(row: dict, changes: dict, actor: str) -> dict:
    """
    Apply a change and write the stage it puts the row in.

    The stage is derived, so this is bookkeeping rather than the source of
    truth -- but storing it keeps exports, reports and the audit trail able to
    say what the row was without recomputing it.
    """
    changes = {**changes, "status": stage_of({**row, **changes})}
    return storage.update("carry_overs", row["id"], changes, actor)
