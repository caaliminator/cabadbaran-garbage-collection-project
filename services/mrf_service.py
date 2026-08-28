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

**What is sitting in an MRF** is everything the tricycles brought in since the
last successful pickup. That definition is what makes carry-overs work without
special-casing: a missed pickup leaves the load in place, and it is still there
(now larger) on the next visit.
"""

from services import (assignment_service, collection_service, schedule_service,
                      storage, timeutil)
from services.validation import ValidationError, Validator

COLLECTED = "Collected from MRF"
NOT_COLLECTED = "Not Collected"
PENDING = "Pending"

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

def last_pickup(barangay_id: str, before_date=None) -> dict | None:
    """The most recent successful pickup from this MRF."""
    day = timeutil.date_str(before_date or timeutil.today())
    rows = [r for r in storage.find("mrf_pickups", barangay_id=barangay_id)
            if r.get("status") == COLLECTED and (r.get("date") or "") <= day]
    rows.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""))
    return rows[-1] if rows else None


def uncollected_entries(barangay_id: str, up_to=None) -> list[dict]:
    """
    Collection entries whose load is still waiting in the MRF: everything
    recorded after the last successful pickup, up to and including today.

    The cut-off compares (date, timestamp), not date alone. A tricycle
    collector who records a household *after* the truck has already emptied
    the MRF that morning has genuinely added new waste to it -- comparing
    dates only would swallow that load and never show it again.
    """
    day = timeutil.date_str(up_to or timeutil.today())
    previous = last_pickup(barangay_id, day)
    since = ((previous.get("date") or "", previous.get("timestamp") or "")
             if previous else None)

    rows = []
    for entry in storage.find("collections", barangay_id=barangay_id,
                              status=collection_service.COLLECTED):
        date = entry.get("date") or ""
        if date > day:
            continue
        if since and (date, entry.get("timestamp") or "") <= since:
            continue
        rows.append(entry)

    rows.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""))
    return rows


def mrf_card(barangay_id: str, date=None) -> dict:
    """
    One barangay MRF as the truck operator sees it: status for the day, the
    schedule day the waste came from, and the load breakdown.
    """
    day = timeutil.date_str(date or timeutil.today())
    names = {b["id"]: b for b in storage.read("barangays")}
    barangay = names.get(barangay_id) or {}

    todays = storage.find_one("mrf_pickups", barangay_id=barangay_id, date=day)
    waiting = uncollected_entries(barangay_id, day)

    if todays and todays.get("status") == COLLECTED:
        # A completed pickup keeps the load it was recorded with. Later
        # collections must not rewrite what a past pickup carried away.
        load = todays.get("load") or {"lines": [], "sacks": 0, "kilos": 0,
                                      "total": "0", "empty": True}
        source_day = todays.get("source_schedule_day")
        status = COLLECTED
    else:
        # Pending, or recorded as missed. Either way the waste is still
        # sitting in the MRF, so the load is the live figure -- an operator
        # who returns after marking it missed must see what is actually
        # there, not the empty snapshot the missed record carries.
        load = collection_service.totals(waiting)
        source_day = (timeutil.weekday_name(waiting[0]["date"]) if waiting
                      else timeutil.weekday_name(day))
        status = todays.get("status") if todays else PENDING

    schedule_row = schedule_service.for_day(source_day)

    return {
        "barangay_id": barangay_id,
        "barangay": barangay.get("name") or barangay_id,
        "mrf_name": barangay.get("mrf_name") or f"{barangay.get('name', '')} MRF",
        "date": day,
        "status": status,
        "source_schedule_day": source_day,
        "waste_type": schedule_row.get("short") or schedule_row.get("waste_type"),
        "load": load,
        "entry": todays,
        "entry_id": todays["id"] if todays else None,
        "reason": (todays or {}).get("reason") or "",
        "note": (todays or {}).get("note") or "",
        "property_count": len(waiting),
        "covered_dates": sorted({e["date"] for e in waiting}),
        "delivered": bool((todays or {}).get("delivery_id")),
    }


def assignment_for(operator_id: str) -> dict | None:
    for row in storage.find(assignment_service.TRUCK_COLLECTION,
                            operator_id=operator_id):
        if row.get("status") in assignment_service.ACTIVE_STATUSES:
            return row
    return None


def cards_for_operator(operator_id: str, date=None) -> list[dict]:
    """
    The truck's MRF list for a date: its assigned barangays, plus any
    carry-over rescheduled onto this truck for today.
    """
    assignment = assignment_for(operator_id)
    if not assignment:
        return []

    day = timeutil.date_str(date or timeutil.today())
    barangay_ids = list(assignment.get("covered_mrfs") or [])

    # A carry-over reassigned to this truck adds a stop that is not on its
    # normal route (spec section 7).
    from services import carryover_service
    for row in carryover_service.pending_for_truck(assignment.get("truck_code"), day):
        if row["barangay_id"] not in barangay_ids:
            barangay_ids.append(row["barangay_id"])

    cards = [mrf_card(b, day) for b in barangay_ids]
    carried = {c["barangay_id"] for c in
               carryover_service.pending_for_truck(assignment.get("truck_code"), day)}
    for card in cards:
        card["is_carry_over"] = card["barangay_id"] in carried

    cards.sort(key=lambda c: (c["status"] != PENDING, c["barangay"]))
    return cards


# ---------------------------------------------------------------------------
# Recording a pickup
# ---------------------------------------------------------------------------

def save_pickup(form, barangay_id: str, operator: dict, date=None) -> dict:
    """
    Record a pickup. Collected moves the barangay's load onto the truck;
    Not Collected marks a missed pickup and opens a carry-over.
    """
    from services import carryover_service

    day = timeutil.date_str(date or timeutil.today())
    v = Validator(form)
    status = v.choice("status", "Pickup result", (COLLECTED, NOT_COLLECTED))
    v.text("note", "Note", max_length=500)

    if status == NOT_COLLECTED:
        v.choice("reason", "Reason", NOT_COLLECTED_REASONS)

    assignment = assignment_for(operator["id"])
    if not assignment:
        v.fail("form", "You do not have an active truck assignment.")

    existing = storage.find_one("mrf_pickups", barangay_id=barangay_id, date=day)
    if existing and existing.get("delivery_id"):
        v.fail("form", "That pickup has already been delivered to the landfill "
                       "and can no longer be changed.")
    elif existing and existing.get("operator_id") != operator["id"]:
        v.fail("form", "Another truck already recorded this MRF today.")

    v.raise_if_invalid()

    card = mrf_card(barangay_id, day)
    load = card["load"] if status == COLLECTED else {
        "lines": [], "sacks": 0, "kilos": 0, "total": "0", "empty": True}

    payload = {
        "barangay_id": barangay_id,
        "date": day,
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

    if status == NOT_COLLECTED:
        # A missed pickup must not simply vanish: it becomes a carry-over the
        # City Hall Admin can reassign or reschedule.
        carry_over = carryover_service.open_for(record, actor=operator["id"])
        triggers.on_carry_over_created(carry_over, record)
    else:
        carryover_service.close_for(barangay_id, record, actor=operator["id"])

    realtime.mrf_pickup_saved(record)
    return record


def auto_mark_missed(date, actor: str = "system") -> list[dict]:
    """
    Close off a past day: any MRF with waste waiting and no status recorded is
    marked Missed so it enters carry-over.

    Without this an MRF that nobody touched stays "Pending" forever and its
    load is quietly forgotten -- inaction would lose the carry-over.
    """
    from services import carryover_service

    day = timeutil.date_str(date)
    if not day or day >= timeutil.today_str():
        return []

    covered = {b for row in storage.read(assignment_service.TRUCK_COLLECTION)
               if row.get("status") in assignment_service.ACTIVE_STATUSES
               for b in (row.get("covered_mrfs") or [])}

    created = []
    for barangay_id in sorted(covered):
        if storage.find_one("mrf_pickups", barangay_id=barangay_id, date=day):
            continue
        waiting = uncollected_entries(barangay_id, day)
        if not waiting:
            continue    # nothing was in the MRF, so nothing was missed

        card = mrf_card(barangay_id, day)
        record = storage.insert("mrf_pickups", {
            "barangay_id": barangay_id, "date": day,
            "source_schedule_day": card["source_schedule_day"],
            "waste_type": card["waste_type"], "load": card["load"],
            "covered_dates": card["covered_dates"],
            "truck_code": None, "operator_id": None,
            "status": NOT_COLLECTED, "gps": None,
            "timestamp": timeutil.stamp(),
            "reason": "No pickup recorded for the day",
            "note": "Marked automatically when the day closed.",
            "delivery_id": None,
            "auto_missed": True,
        }, actor)
        from services import triggers
        triggers.on_carry_over_created(
            carryover_service.open_for(record, actor=actor), record)
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

    collected = sum(1 for r in rows if r["status"] == COLLECTED)
    missed = sum(1 for r in rows if r["status"] == NOT_COLLECTED)

    return {
        "total": total,
        "collected": collected,
        "missed": missed,
        "pending": total - collected - missed,
        "delivered": storage.count("deliveries", date=day),
        "percent": round(collected / total * 100) if total else 0,
    }


def city_listing(date=None, barangay_id=None, truck=None, status=None) -> list[dict]:
    """
    Every barangay MRF for a date, whether or not a pickup was recorded --
    a barangay with no record is Pending, and needs to appear as such.
    """
    day = timeutil.date_str(date or timeutil.today())
    recorded = {r["barangay_id"]: r for r in storage.find("mrf_pickups", date=day)}
    operators = {u["id"]: u.get("full_name") for u in storage.read("users")}

    from services import geo_service
    mrf_points = {m["barangay_id"]: m
                  for m in geo_service.mrf_locations().get("mrfs", [])}

    rows = []
    for barangay in sorted(storage.read("barangays"), key=lambda b: b.get("number", 0)):
        card = mrf_card(barangay["id"], day)
        entry = recorded.get(barangay["id"])

        if barangay_id and barangay["id"] != barangay_id:
            continue
        if status and card["status"] != status:
            continue
        if truck and (entry or {}).get("truck_code") != truck:
            continue

        stamp = (entry or {}).get("timestamp")
        rows.append({
            **card,
            "truck": (entry or {}).get("truck_code") or _expected_truck(barangay["id"]),
            "operator": operators.get((entry or {}).get("operator_id")) or "—",
            "auto_missed": bool((entry or {}).get("auto_missed")),
            "timestamp": stamp,
            "date_display": timeutil.display_date(day),
            # Blank until a truck records something -- a Pending MRF has no
            # time and no place, and "—" is the honest value for both.
            "time_display": timeutil.display_time(timeutil.parse_stamp(stamp)) if stamp else "",
            "location": _pickup_location(barangay["id"], entry, mrf_points),
        })
    return rows


def _pickup_location(barangay_id: str, entry: dict | None,
                     mrf_points: dict) -> dict:
    """
    Where a pickup happened, from two independent sources.

    The MRF's own registered coordinates say where the facility is; the GPS
    the truck captured says where the operator actually stood when they
    recorded it. They are usually the same place and occasionally are not,
    which is exactly why both are kept rather than one overwriting the other.
    """
    point = mrf_points.get(barangay_id) or {}
    captured = (entry or {}).get("gps") or None

    return {
        "name": point.get("name") or "",
        "lat": point.get("lat"),
        "lng": point.get("lng"),
        "captured": captured,
        "coords": (f"{captured['lat']}, {captured['lng']}" if captured
                   else (f"{point['lat']}, {point['lng']}"
                         if point.get("lat") is not None else "")),
        "source": ("Captured at the stop" if captured
                   else "Registered MRF location" if point.get("lat") is not None
                   else ""),
    }


def _expected_truck(barangay_id: str) -> str:
    """Which truck is supposed to collect here, for a barangay with no record."""
    for row in storage.read(assignment_service.TRUCK_COLLECTION):
        if (row.get("status") in assignment_service.ACTIVE_STATUSES
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
            "barangays": [names.get(b) for b in row.get("mrfs_included") or []
                          if names.get(b)],
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

    pickups = []
    for row in storage.find("mrf_pickups", operator_id=operator_id):
        if day and row.get("date") != day:
            continue
        name = names.get(row.get("barangay_id")) or "Unknown barangay"
        if needle and needle not in name.lower():
            continue
        stamp = timeutil.parse_stamp(row.get("timestamp"))
        pickups.append({
            **row,
            "barangay": name,
            "mrf_name": f"{name} MRF",
            "date_display": timeutil.display_date(row.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "lines": (row.get("load") or {}).get("lines") or [],
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
        included = [names.get(b) for b in row.get("mrfs_included") or [] if names.get(b)]
        if needle and not any(needle in (n or "").lower() for n in included):
            continue
        deliveries.append({
            **row,
            "barangays": included,
            "batch": f"{row.get('schedule_day', '')} Batch".strip(),
            "date_display": timeutil.display_date(row.get("date")),
            "time_display": timeutil.display_time(stamp) if stamp else "—",
            "lines": (row.get("load") or {}).get("lines") or [],
            "total": (row.get("load") or {}).get("total") or "0",
        })
    deliveries.sort(key=lambda r: (r.get("date") or "", r.get("timestamp") or ""),
                    reverse=True)

    return {"pickups": pickups, "deliveries": deliveries}
