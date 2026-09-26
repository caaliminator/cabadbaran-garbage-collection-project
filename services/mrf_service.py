"""
Barangay MRF pickups and landfill deliveries -- the truck operator's work.

The aggregation chain from spec section 7 runs through here, and the whole
point of it is that nothing is entered twice:

    tricycle entries -> barangay Overall Collected Load
                     -> that barangay's MRF card, shown to the truck
                     -> the truck's running Overall Collected Load
                     -> the delivery totals
                     -> the city MRF page

So the truck operator records no quantities. The load on an MRF card is
computed from that barangay's collection entries, and confirming "Collect"
simply moves it along the chain.

**Every day is its own list.** An MRF card for a date carries only that date's
batch -- what the tricycles brought in *that day*, under *that day's* waste
type -- and it is Pending until the truck records Collected or Not Collected.
Nothing from an earlier day is mixed into it. A Monday of biodegradable waste
shows biodegradable waste, even if Saturday's special waste is still sitting
in the MRF.

What happens to a batch the truck did not take is the Carry-Over page's job,
not the next day's card's. A miss -- recorded by the operator, or marked
automatically when the day closes with nothing recorded -- opens a carry-over
for that one batch. It reappears on a truck's page only once the City Hall
Admin has given it a truck and a date, and then as a separate, labelled
carry-over stop with its own day and waste type: a pickup of `kind`
"carry_over", recorded against that carry-over, beside the day's regular list
rather than folded into it.

Pickups written before `kind` existed read as regular ones.
"""

from services import (assignment_service, collection_service, schedule_service,
                      storage, timeutil)
from services.validation import ValidationError, Validator

COLLECTED = "Collected from MRF"
NOT_COLLECTED = "Not Collected"
PENDING = "Pending"

# A pickup is either the day's regular stop at an MRF, or a carry-over stop:
# an earlier day's missed batch, collected on the date the admin arranged.
REGULAR = "regular"
CARRY_OVER = "carry_over"

EMPTY_LOAD = {"lines": [], "sacks": 0, "kilos": 0, "total": "0", "empty": True}

NOT_COLLECTED_REASONS = (
    "MRF was locked / no attendant",
    "Road inaccessible",
    "Truck breakdown",
    "Load exceeds truck capacity",
    "Waste not segregated at the MRF",
    "Ran out of time on the route",
    "Other",
)


# ---------------------------------------------------------------------------
# What is in the MRF
# ---------------------------------------------------------------------------

def is_regular(pickup: dict) -> bool:
    return (pickup.get("kind") or REGULAR) == REGULAR


def regular_pickup(barangay_id: str, date=None) -> dict | None:
    """The day's regular pickup at an MRF, if one has been recorded."""
    day = timeutil.date_str(date or timeutil.today())
    return next((p for p in storage.find("mrf_pickups", barangay_id=barangay_id,
                                         date=day) if is_regular(p)), None)


def carry_over_pickup(carry_over_id: str, date=None) -> dict | None:
    """A carry-over stop's pickup on a date, if one has been recorded."""
    day = timeutil.date_str(date or timeutil.today())
    return storage.find_one("mrf_pickups", carry_over_id=carry_over_id, date=day)


def batch_entries(barangay_id: str, date=None) -> list[dict]:
    """
    One day's batch at an MRF: the households the tricycles collected in that
    barangay on that date. This is everything a day's regular card holds --
    an earlier day's waste is never counted into a later day's card.
    """
    day = timeutil.date_str(date or timeutil.today())
    rows = storage.find("collections", barangay_id=barangay_id, date=day,
                        status=collection_service.COLLECTED)
    rows.sort(key=lambda r: r.get("timestamp") or "")
    return rows


def _waste_type_for(day_name: str) -> str:
    row = schedule_service.for_day(day_name)
    return row.get("short") or row.get("waste_type")


def mrf_card(barangay_id: str, date=None) -> dict:
    """
    One barangay MRF on a date, as the truck operator sees it: that day's
    status, that day's waste type, and that day's load -- nothing else.
    """
    day = timeutil.date_str(date or timeutil.today())
    names = {b["id"]: b for b in storage.read("barangays")}
    barangay = names.get(barangay_id) or {}

    todays = regular_pickup(barangay_id, day)
    batch = batch_entries(barangay_id, day)

    if todays and todays.get("status") == COLLECTED:
        # A completed pickup keeps the load it was recorded with. Later
        # entries must not rewrite what a past pickup carried away.
        load = todays.get("load") or EMPTY_LOAD
        status = COLLECTED
    else:
        # Pending, or recorded as missed: the day's batch as it stands, so an
        # operator who returns after marking it missed sees what is there.
        load = collection_service.totals(batch)
        status = todays.get("status") if todays else PENDING

    source_day = timeutil.weekday_name(day)

    return {
        "barangay_id": barangay_id,
        "barangay": barangay.get("name") or barangay_id,
        "mrf_name": barangay.get("mrf_name") or f"{barangay.get('name', '')} MRF",
        "date": day,
        "kind": REGULAR,
        "status": status,
        "source_schedule_day": source_day,
        "waste_type": _waste_type_for(source_day),
        "load": load,
        "entry": todays,
        "entry_id": todays["id"] if todays else None,
        "reason": (todays or {}).get("reason") or "",
        "note": (todays or {}).get("note") or "",
        "property_count": len(batch),
        "covered_dates": [day] if batch else [],
        "delivered": bool((todays or {}).get("delivery_id")),
        "is_carry_over": False,
        "carry_over_id": None,
    }


def carry_over_card(carry_over: dict, date=None) -> dict:
    """
    A carry-over stop on the date it was arranged for. It carries the missed
    day's own waste type and load, so it can sit beside the day's regular
    list without anything from it being mixed in.
    """
    day = timeutil.date_str(date or timeutil.today())
    names = {b["id"]: b for b in storage.read("barangays")}
    barangay_id = carry_over.get("barangay_id")
    barangay = names.get(barangay_id) or {}
    todays = carry_over_pickup(carry_over["id"], day)

    if todays and todays.get("status") == COLLECTED:
        load = todays.get("load") or EMPTY_LOAD
    else:
        load = carry_over.get("waste") or EMPTY_LOAD

    batch_date = carry_over.get("batch_date") or carry_over.get("first_missed_date")
    return {
        "barangay_id": barangay_id,
        "barangay": barangay.get("name") or barangay_id,
        "mrf_name": barangay.get("mrf_name") or f"{barangay.get('name', '')} MRF",
        "date": day,
        "kind": CARRY_OVER,
        "status": todays.get("status") if todays else PENDING,
        "source_schedule_day": carry_over.get("source_schedule_day") or "—",
        "waste_type": carry_over.get("waste_type") or "—",
        "load": load,
        "entry": todays,
        "entry_id": todays["id"] if todays else None,
        "reason": (todays or {}).get("reason") or "",
        "note": (todays or {}).get("note") or "",
        "property_count": None,
        "covered_dates": [batch_date] if batch_date else [],
        "delivered": bool((todays or {}).get("delivery_id")),
        "is_carry_over": True,
        "carry_over_id": carry_over["id"],
        "missed_count": carry_over.get("missed_count") or 1,
        "original_truck": carry_over.get("original_truck"),
    }


def _may_take_over(existing: dict, status: str) -> bool:
    """
    Whether a second truck may record over the day's existing pickup.

    Normally no: two trucks recording the same MRF on the same day means one
    of them is wrong, and the load would be counted twice.

    The exception is the one the carry-over flow depends on. If the record
    standing is a *miss*, the waste is still in the MRF -- and City Hall
    reassigning the stop to another truck for the same day is exactly the
    rescue the Carry-Over page exists to arrange. Refusing it left the admin
    able to make an arrangement the assigned operator could not carry out, and
    the load sitting there until tomorrow for no reason.

    Only a collection may take over a miss. A second truck recording another
    *miss* would overwrite the first truck's reason and photo with its own,
    and two trucks failing at the same MRF on the same day is a conversation
    for the admin, not a record to silently replace.

    The miss itself is not lost: the carry-over keeps its own copy of every
    attempt -- the date, the truck, the operator and the reason -- which is
    what the Missed Collection history is built from.
    """
    return existing.get("status") == NOT_COLLECTED and status == COLLECTED


def assignment_for(operator_id: str) -> dict | None:
    for row in storage.find(assignment_service.TRUCK_COLLECTION,
                            operator_id=operator_id):
        if assignment_service.is_active(row):
            return row
    return None


def cards_for_operator(operator_id: str, date=None) -> list[dict]:
    """
    The truck's regular list for a date: one card per MRF on its route, each
    holding that day's batch only. Carry-over stops are a separate list --
    see carry_over_cards_for_operator.
    """
    assignment = assignment_for(operator_id)
    if not assignment:
        return []

    day = timeutil.date_str(date or timeutil.today())
    cards = [mrf_card(b, day) for b in assignment.get("covered_mrfs") or []]
    cards.sort(key=lambda c: (c["status"] != PENDING, c["barangay"]))
    return cards


def carry_over_cards_for_operator(operator_id: str, date=None) -> list[dict]:
    """
    Carry-over stops the City Hall Admin arranged for this truck on this date:
    reassigned to it and rescheduled for today. Also keeps any this truck
    already recorded today, so a collected stop reads Collected instead of
    vanishing from the page the moment it is done.
    """
    from services import carryover_service

    assignment = assignment_for(operator_id)
    if not assignment:
        return []
    day = timeutil.date_str(date or timeutil.today())

    rows = {r["id"]: r for r in
            carryover_service.pending_for_truck(assignment.get("truck_code"), day)}
    for pickup in storage.find("mrf_pickups", operator_id=operator_id, date=day,
                               kind=CARRY_OVER):
        row = storage.get("carry_overs", pickup.get("carry_over_id"))
        if row:
            rows.setdefault(row["id"], row)

    cards = [carry_over_card(row, day) for row in rows.values()]
    cards.sort(key=lambda c: (c["status"] != PENDING, c["barangay"]))
    return cards


# ---------------------------------------------------------------------------
# Recording a pickup
# ---------------------------------------------------------------------------

def save_pickup(form, barangay_id: str, operator: dict, date=None,
                carry_over_id: str | None = None) -> dict:
    """
    Record a pickup. Collected moves the load onto the truck; Not Collected
    marks it missed.

    Without `carry_over_id` this is the day's regular stop, and a miss opens a
    carry-over for that day's batch. With it, this is a carry-over stop: the
    load is the carry-over's own, Collected closes that carry-over, and a miss
    sends it back to Missed Collection for the admin to arrange again.
    """
    from services import carryover_service

    day = timeutil.date_str(date or timeutil.today())
    # A stop opened before midnight and saved after it belongs to the day it
    # was opened for, which has closed; saving it would record the new day's
    # stop -- a different day's waste -- without the operator knowing.
    opened = str(form.get("form_date") or "").strip()
    if opened and opened != day:
        raise ValidationError({"form": (
            f"This stop was opened for {timeutil.display_date(opened)}, and that "
            f"day's list has closed. It was not saved — reopen today's MRF list.")})
    v = Validator(form)
    status = v.choice("status", "Pickup result", (COLLECTED, NOT_COLLECTED))
    v.text("note", "Note", max_length=500)

    if status == NOT_COLLECTED:
        v.choice("reason", "Reason", NOT_COLLECTED_REASONS)

    assignment = assignment_for(operator["id"])
    if not assignment:
        v.fail("form", "You do not have an active truck assignment.")

    carry_over = storage.get("carry_overs", carry_over_id) if carry_over_id else None
    if carry_over_id and (not carry_over or carry_over.get("barangay_id") != barangay_id):
        v.fail("form", "That carry-over no longer exists.")

    existing = (carry_over_pickup(carry_over_id, day) if carry_over_id
                else regular_pickup(barangay_id, day))
    if existing and existing.get("delivery_id"):
        v.fail("form", "That pickup has already been delivered to the landfill "
                       "and can no longer be changed.")
    elif (existing and existing.get("operator_id") != operator["id"]
            and not _may_take_over(existing, status)):
        v.fail("form", "Another truck already recorded this MRF today.")

    v.raise_if_invalid()

    card = (carry_over_card(carry_over, day) if carry_over
            else mrf_card(barangay_id, day))
    # A missed load never reaches the truck, so the pickup carries none. What
    # was left behind travels with the carry-over instead (see open_for).
    load = card["load"] if status == COLLECTED else EMPTY_LOAD

    payload = {
        "barangay_id": barangay_id,
        "date": day,
        "kind": CARRY_OVER if carry_over else REGULAR,
        "carry_over_id": carry_over["id"] if carry_over else None,
        "source_schedule_day": card["source_schedule_day"],
        "waste_type": card["waste_type"],
        "load": load,
        "covered_dates": card["covered_dates"],
        "truck_code": assignment.get("truck_code"),
        "operator_id": operator["id"],
        "status": status,
        "gps": collection_service._parse_gps(form.get("gps")),
        "timestamp": timeutil.stamp(),
        "reason": v.data.get("reason", "") if status == NOT_COLLECTED else "",
        "note": v.data["note"],
        "delivery_id": None,
        "auto_missed": False,
    }

    if existing:
        record = storage.update("mrf_pickups", existing["id"], payload, operator["id"])
    else:
        record = storage.insert("mrf_pickups", payload, operator["id"])

    from services import realtime, triggers

    if carry_over:
        if status == COLLECTED:
            carryover_service.close(carry_over, record, actor=operator["id"])
            triggers.on_mrf_collected(record, carry_over)
        else:
            again = carryover_service.missed_again(carry_over, record,
                                                   actor=operator["id"])
            triggers.on_carry_over_missed_again(again, record)
    elif status == NOT_COLLECTED:
        # A missed pickup must not simply vanish: that day's batch becomes a
        # carry-over the City Hall Admin can reassign and reschedule.
        opened = carryover_service.open_for(record, card["load"], actor=operator["id"])
        triggers.on_carry_over_created(opened, record)
    else:
        # Collected after being marked missed the same day: the batch was
        # taken after all, so its carry-over closes with it.
        carryover_service.close_for(barangay_id, record, actor=operator["id"])
        triggers.on_mrf_collected(record)

    realtime.mrf_pickup_saved(record)
    return record


def auto_mark_missed(date, actor: str = "system") -> list[dict]:
    """
    Close off a past day, so that day's list is finished and the next day's
    starts clean:

    * any MRF whose batch was never given a status is marked missed, and that
      batch enters carry-over;
    * any carry-over arranged for that day that its truck never recorded goes
      back to Missed Collection, for the admin to arrange again.

    Without this an MRF that nobody touched stays "Pending" forever and its
    load is quietly forgotten -- inaction would lose the carry-over.
    """
    from services import carryover_service, triggers

    day = timeutil.date_str(date)
    if not day or day >= timeutil.today_str():
        return []

    covered = {b for row in storage.read(assignment_service.TRUCK_COLLECTION)
               if assignment_service.is_active(row)
               for b in (row.get("covered_mrfs") or [])}

    created = []
    for barangay_id in sorted(covered):
        if regular_pickup(barangay_id, day):
            continue
        batch = batch_entries(barangay_id, day)
        if not batch:
            continue    # nothing came in that day, so nothing was missed

        card = mrf_card(barangay_id, day)
        record = storage.insert("mrf_pickups", {
            "barangay_id": barangay_id, "date": day,
            "kind": REGULAR, "carry_over_id": None,
            "source_schedule_day": card["source_schedule_day"],
            "waste_type": card["waste_type"], "load": EMPTY_LOAD,
            "covered_dates": card["covered_dates"],
            "truck_code": None, "operator_id": None,
            "status": NOT_COLLECTED, "gps": None,
            "timestamp": timeutil.stamp(),
            "reason": "No pickup recorded for the day",
            "note": "Marked automatically when the day closed.",
            "delivery_id": None,
            "auto_missed": True,
        }, actor)
        triggers.on_carry_over_created(
            carryover_service.open_for(record, card["load"], actor=actor), record)
        created.append(record)

    # Carry-overs whose arranged day has now passed with no pickup recorded.
    for row in storage.read("carry_overs"):
        if (carryover_service.stage_of(row) != carryover_service.PENDING
                or row.get("reschedule_date") != day
                or carry_over_pickup(row["id"], day)):
            continue
        record = storage.insert("mrf_pickups", {
            "barangay_id": row.get("barangay_id"), "date": day,
            "kind": CARRY_OVER, "carry_over_id": row["id"],
            "source_schedule_day": row.get("source_schedule_day"),
            "waste_type": row.get("waste_type"), "load": EMPTY_LOAD,
            "covered_dates": [row.get("batch_date") or row.get("first_missed_date")],
            "truck_code": row.get("current_truck"), "operator_id": None,
            "status": NOT_COLLECTED, "gps": None,
            "timestamp": timeutil.stamp(),
            "reason": "Not collected on the rescheduled date",
            "note": "Marked automatically when the day closed.",
            "delivery_id": None,
            "auto_missed": True,
        }, actor)
        triggers.on_carry_over_missed_again(
            carryover_service.missed_again(row, record, actor=actor), record)
        created.append(record)

    return created


# ---------------------------------------------------------------------------
# The truck's running load, and delivery to landfill
# ---------------------------------------------------------------------------

def running_load(operator_id: str, date=None) -> dict:
    """
    What is on the truck right now: today's collected pickups that have not
    been delivered yet.

    Delivering resets this to zero, so a second trip the same day starts clean
    rather than double-counting the first load.
    """
    day = timeutil.date_str(date or timeutil.today())
    pickups = [p for p in storage.find("mrf_pickups", operator_id=operator_id, date=day)
               if p.get("status") == COLLECTED and not p.get("delivery_id")]

    buckets: dict[tuple[str, str], int] = {}
    for pickup in pickups:
        for line in (pickup.get("load") or {}).get("lines") or []:
            key = (line["label"], line["unit"])
            buckets[key] = buckets.get(key, 0) + int(line.get("value") or 0)

    lines = [{"label": label, "unit": unit, "value": qty,
              "display": collection_service.format_unit(unit, qty)}
             for (label, unit), qty in sorted(buckets.items())]
    sacks = sum(q for (_, u), q in buckets.items() if u == "Sack")
    kilos = sum(q for (_, u), q in buckets.items() if u == "Kilo")

    parts = []
    if sacks:
        parts.append(f"{sacks} sack{'' if sacks == 1 else 's'}")
    if kilos:
        parts.append(f"{kilos} kg")

    return {
        "lines": lines, "by_type": collection_service.group_by_type(lines),
        "sacks": sacks, "kilos": kilos,
        "total": " and ".join(parts) or "0",
        "empty": not lines,
        "mrf_count": len(pickups),
        "pickup_ids": [p["id"] for p in pickups],
        "barangays": [p["barangay_id"] for p in pickups],
    }


def deliver(operator: dict, gps=None, date=None) -> dict:
    """
    Record a landfill delivery for everything currently on the truck, then
    clear the running load.
    """
    day = timeutil.date_str(date or timeutil.today())
    load = running_load(operator["id"], day)

    if load["empty"] and not load["pickup_ids"]:
        raise ValidationError({"form": "There is no collected load to deliver yet."})

    assignment = assignment_for(operator["id"])
    names = {b["id"]: b["name"] for b in storage.read("barangays")}

    delivery = storage.insert("deliveries", {
        "truck_code": (assignment or {}).get("truck_code"),
        "operator_id": operator["id"],
        "date": day,
        "timestamp": timeutil.stamp(),
        "mrfs_included": load["barangays"],
        "mrf_names": [names.get(b) for b in load["barangays"] if names.get(b)],
        "pickup_ids": load["pickup_ids"],
        "load": {k: load[k] for k in ("lines", "sacks", "kilos", "total")},
        "schedule_day": timeutil.weekday_name(day),
        "gps": collection_service._parse_gps(gps),
    }, operator["id"])

    # Stamping the pickups is what resets the running load -- they stop
    # counting as "on the truck" the moment they are delivered.
    for pickup_id in load["pickup_ids"]:
        storage.update("mrf_pickups", pickup_id,
                       {"delivery_id": delivery["id"]}, operator["id"])

    from services import triggers
    triggers.on_delivery(delivery, operator)
    return delivery


# ---------------------------------------------------------------------------
# City-wide views
# ---------------------------------------------------------------------------

def city_counts(date=None) -> dict:
    """The four MRF cards on the City Hall dashboard and MRF page."""
    day = timeutil.date_str(date or timeutil.today())
    total = storage.count("barangays")
    rows = storage.find("mrf_pickups", date=day)
    # The day's list is one regular stop per MRF. A carry-over stop is an
    # earlier day's batch, so it is counted on its own rather than as a second
    # "collected" for the same barangay.
    regular = [r for r in rows if is_regular(r)]

    collected = sum(1 for r in regular if r["status"] == COLLECTED)
    missed = sum(1 for r in regular if r["status"] == NOT_COLLECTED)

    return {
        "total": total,
        "collected": collected,
        "missed": missed,
        "pending": total - collected - missed,
        "carry_overs_collected": sum(1 for r in rows if not is_regular(r)
                                     and r["status"] == COLLECTED),
        "delivered": storage.count("deliveries", date=day),
        "percent": round(collected / total * 100) if total else 0,
    }


def city_listing(date=None, barangay_id=None, truck=None, status=None) -> list[dict]:
    """
    Every barangay MRF for a date, whether or not a pickup was recorded --
    a barangay with no record is Pending, and needs to appear as such.
    """
    day = timeutil.date_str(date or timeutil.today())
    pickups = storage.find("mrf_pickups", date=day)
    recorded = {r["barangay_id"]: r for r in pickups if is_regular(r)}
    operators = operator_names()

    # A collected carry-over is shown on the date it was actually picked up
    # -- the "collected carry-overs reflect in the MRF on their corresponding
    # dates" rule -- as its own row, labelled with the earlier day it came
    # from, beside that barangay's regular row for the day.
    from services import carryover_service
    closed = {c["collected_by_pickup"]: c for c in storage.read("carry_overs")
              if c.get("collected_by_pickup")}

    from services import geo_service
    mrf_points = {m["barangay_id"]: m
                  for m in geo_service.mrf_locations().get("mrfs", [])}

    def row_for(card, entry, barangay_ref):
        stamp = (entry or {}).get("timestamp")
        carry_over = closed.get((entry or {}).get("id"))
        return {
            **card,
            "truck": (entry or {}).get("truck_code") or _expected_truck(barangay_ref),
            "operator": operators.get((entry or {}).get("operator_id")) or "—",
            # Present only on a row that collected a carry-over; the template
            # uses it to switch from "Assigned Truck" to original/current.
            "carry_over": (carryover_service.detail(carry_over)
                           if carry_over else None),
            "auto_missed": bool((entry or {}).get("auto_missed")),
            "timestamp": stamp,
            "date_display": timeutil.display_date(day),
            # Blank until a truck records something -- a Pending MRF has no
            # time and no place, and "—" is the honest value for both.
            "time_display": timeutil.display_time(timeutil.parse_stamp(stamp)) if stamp else "",
            "location": _pickup_location(barangay_ref, entry, mrf_points),
        }

    def wanted(card, entry, barangay_ref):
        if barangay_id and barangay_ref != barangay_id:
            return False
        if status and card["status"] != status:
            return False
        if truck and (entry or {}).get("truck_code") != truck:
            return False
        return True

    carried = {}
    for pickup in pickups:
        if is_regular(pickup):
            continue
        row = storage.get("carry_overs", pickup.get("carry_over_id"))
        if row:
            carried.setdefault(pickup["barangay_id"], []).append(
                (carry_over_card(row, day), pickup))

    rows = []
    for barangay in sorted(storage.read("barangays"), key=lambda b: b.get("number", 0)):
        card = mrf_card(barangay["id"], day)
        entry = recorded.get(barangay["id"])
        if wanted(card, entry, barangay["id"]):
            rows.append(row_for(card, entry, barangay["id"]))
        for co_card, co_entry in carried.get(barangay["id"], []):
            if wanted(co_card, co_entry, barangay["id"]):
                rows.append(row_for(co_card, co_entry, barangay["id"]))
    return rows


def operator_names() -> dict:
    """Operator id -> full name, for the detail dialogs."""
    return {u["id"]: u.get("full_name") for u in storage.read("users")}


def pickup_view(pickup: dict | None, barangay_id: str = "") -> dict:
    """
    One recorded pickup, resolved for a detail dialog: display date and time,
    the truck and the operator by name, the load, and where it happened.

    Shared by the MRF page and the Carry-Over page on purpose. The spec asks
    for a collected carry-over to show the same details as the MRF pickup that
    collected it -- the only way to guarantee that is for both to be the same
    function rather than two templates that happen to agree today.
    """
    from services import geo_service

    if not pickup:
        return {"recorded": False, "date_display": "—", "time_display": "—",
                "truck": "—", "operator": "—", "load": {"total": "0", "lines": []},
                "status": PENDING, "reason": "", "note": "",
                "location": {"name": "", "coords": "", "source": ""}}

    stamp = timeutil.parse_stamp(pickup.get("timestamp"))
    points = {m["barangay_id"]: m for m in geo_service.mrf_locations().get("mrfs", [])}
    bid = barangay_id or pickup.get("barangay_id")

    return {
        "recorded": True,
        "id": pickup.get("id"),
        "date": pickup.get("date"),
        "date_display": timeutil.display_date(pickup.get("date")),
        "time_display": timeutil.display_time(stamp) if stamp else "—",
        "source_schedule_day": pickup.get("source_schedule_day") or "—",
        "waste_type": pickup.get("waste_type") or "—",
        "truck": pickup.get("truck_code") or "—",
        "operator": operator_names().get(pickup.get("operator_id")) or "—",
        "load": pickup.get("load") or {"total": "0", "lines": []},
        "status": pickup.get("status"),
        "reason": pickup.get("reason") or "",
        "note": pickup.get("note") or "",
        "auto_missed": bool(pickup.get("auto_missed")),
        "location": _pickup_location(bid, pickup, points),
    }


def _pickup_location(barangay_id: str, entry: dict | None,
                     mrf_points: dict) -> dict:
    """
    Where a pickup happened, from two independent sources.

    The MRF's own coordinates say where the facility is; the GPS the truck
    captured says where the operator actually stood when they recorded it. They
    are usually the same place and occasionally are not, which is exactly why
    both are kept rather than one overwriting the other.

    The facility's own coordinate comes in two grades, and the label has to say
    which. Ten of the 31 have been measured at the gate; the rest are placed
    near their barangay's published centre until somebody surveys them. Calling
    an approximated point a "registered location" would invite exactly the
    mistake the flag exists to prevent -- reading a figure that is right to
    within a barangay as one that is right to within a building.
    """
    point = mrf_points.get(barangay_id) or {}
    captured = (entry or {}).get("gps") or None
    has_point = point.get("lat") is not None

    if captured:
        source = "Captured at the stop"
    elif has_point and point.get("surveyed"):
        source = "Surveyed MRF location"
    elif has_point:
        source = "Approximate — MRF not yet surveyed"
    else:
        source = ""

    return {
        "name": point.get("name") or "",
        "lat": point.get("lat"),
        "lng": point.get("lng"),
        "surveyed": bool(point.get("surveyed")),
        "captured": captured,
        "coords": (f"{captured['lat']}, {captured['lng']}" if captured
                   else (f"{point['lat']}, {point['lng']}" if has_point else "")),
        "source": source,
    }


def _expected_truck(barangay_id: str) -> str:
    """Which truck is supposed to collect here, for a barangay with no record."""
    for row in storage.read(assignment_service.TRUCK_COLLECTION):
        if (assignment_service.is_active(row)
                and barangay_id in (row.get("covered_mrfs") or [])):
            return row.get("truck_code") or "—"
    return "Unassigned"


def city_totals(date=None) -> dict:
    """Overall Collected Load across every MRF picked up on a date."""
    day = timeutil.date_str(date or timeutil.today())
    buckets: dict[tuple[str, str], int] = {}
    for pickup in storage.find("mrf_pickups", date=day, status=COLLECTED):
        for line in (pickup.get("load") or {}).get("lines") or []:
            key = (line["label"], line["unit"])
            buckets[key] = buckets.get(key, 0) + int(line.get("value") or 0)

    lines = [{"label": label, "unit": unit, "value": qty,
              "display": collection_service.format_unit(unit, qty)}
             for (label, unit), qty in sorted(buckets.items())]
    sacks = sum(q for (_, u), q in buckets.items() if u == "Sack")
    kilos = sum(q for (_, u), q in buckets.items() if u == "Kilo")
    parts = []
    if sacks:
        parts.append(f"{sacks} sack{'' if sacks == 1 else 's'}")
    if kilos:
        parts.append(f"{kilos} kg")

    return {"lines": lines, "by_type": collection_service.group_by_type(lines),
            "sacks": sacks, "kilos": kilos,
            "total": " and ".join(parts) or "0", "empty": not lines}


def _mrf_names(delivery: dict, names: dict) -> list[str]:
    """
    The MRFs a delivery came from, each named once. A truck that collected a
    barangay's regular stop and its carry-over stop delivers both, which is
    two pickups but one MRF -- listing it twice read as two facilities.
    """
    return list(dict.fromkeys(names.get(b) for b in delivery.get("mrfs_included") or []
                              if names.get(b)))


def deliveries_listing(date=None) -> list[dict]:
    day = timeutil.date_str(date) if date else None
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    operators = {u["id"]: u.get("full_name") for u in storage.read("users")}

    rows = []
    for row in storage.read("deliveries"):
        if day and row.get("date") != day:
            continue
        stamp = timeutil.parse_stamp(row.get("timestamp"))
        rows.append({
            **row,
            "operator": operators.get(row.get("operator_id")) or "—",
            "barangays": _mrf_names(row, names),
            "date_display": timeutil.display_date(row.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
        })
    rows.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""), reverse=True)
    return rows


def history_for_operator(operator_id: str, date=None, search: str = "") -> dict:
    """MRF Pickup History and Final Disposal Delivery History, both filtered."""
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    day = timeutil.date_str(date) if date else None
    needle = (search or "").strip().lower()

    from services import carryover_service

    pickups = []
    for row in storage.find("mrf_pickups", operator_id=operator_id):
        if day and row.get("date") != day:
            continue
        name = names.get(row.get("barangay_id")) or "Unknown barangay"
        if needle and needle not in name.lower():
            continue
        stamp = timeutil.parse_stamp(row.get("timestamp"))
        # The same fields the MRF page's View Details reads, from the same
        # function, so a pickup opened from History and from the MRF page
        # cannot disagree.
        view = pickup_view(row, row.get("barangay_id"))
        carried = (storage.get("carry_overs", row.get("carry_over_id"))
                   if row.get("carry_over_id") else None)
        pickups.append({
            **row,
            **view,
            "barangay": name,
            "mrf_name": f"{name} MRF",
            "date_display": timeutil.display_date(row.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "lines": (row.get("load") or {}).get("lines") or [],
            "is_carry_over": not is_regular(row),
            "carry_over": carryover_service.detail(carried) if carried else None,
        })
    pickups.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""),
                 reverse=True)

    deliveries = []
    for row in storage.read("deliveries"):
        if row.get("operator_id") != operator_id:
            continue
        if day and row.get("date") != day:
            continue
        stamp = timeutil.parse_stamp(row.get("timestamp"))
        included = _mrf_names(row, names)
        if needle and not any(needle in (n or "").lower() for n in included):
            continue
        deliveries.append({
            **row,
            "barangays": included,
            "operator": operator_names().get(row.get("operator_id")) or "—",
            "batch": f"{row.get('schedule_day', '')} Batch".strip(),
            "date_display": timeutil.display_date(row.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "lines": (row.get("load") or {}).get("lines") or [],
            "total": (row.get("load") or {}).get("total") or "0",
        })
    deliveries.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""),
                    reverse=True)

    return {"pickups": pickups, "deliveries": deliveries}
