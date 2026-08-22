"""
Automatic notification triggers (spec section 5).

Each function is called at the moment the condition becomes true, stores the
notification, and pushes it to the right rooms. Two of them fire on repeating
conditions and so must not spam:

  * **Truck approaching** is evaluated on every GPS ping. A truck idling near
    an MRF would otherwise raise an alert every few seconds, so it fires once
    per truck per MRF per day, keyed on a dedupe string.
  * **Scheduled arrival** (T-2h) is checked lazily on requests rather than by
    a scheduler, matching the end-of-day job. Same once-per-day keying.

Everything here is best-effort: a notification that fails to send must never
break the action that triggered it. The caller has already saved its data.
"""

from datetime import timedelta

from config import Config
from services import (assignment_service, geo_service, notification_service,
                      realtime, storage, timeutil)

N = notification_service


# ---------------------------------------------------------------------------
# Truck approaching an MRF
# ---------------------------------------------------------------------------

def check_truck_approaching(user: dict, lat: float, lng: float) -> list[dict]:
    """
    Haversine distance from an on-duty truck to each MRF it covers. Under
    `Config.TRUCK_APPROACH_METRES`, tell that barangay and the city admin.

    Silently does nothing when the MRF has no coordinates yet -- which is the
    normal state until the real geo data is supplied, and not a reason to
    complain on every ping.
    """
    if user.get("role") != "truck_collector":
        return []

    assignment = _active_truck_assignment(user["id"])
    if not assignment:
        return []

    truck = assignment.get("truck_code") or "A truck"
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    sent = []

    for barangay_id in assignment.get("covered_mrfs") or []:
        point = geo_service.point_for_barangay(barangay_id)
        if not point:
            continue

        distance = geo_service.haversine_metres((lat, lng), point)
        if distance > Config.TRUCK_APPROACH_METRES:
            continue

        key = f"approach:{truck}:{barangay_id}"
        if N.already_sent(key):
            continue

        name = names.get(barangay_id, barangay_id)
        message = f"Truck {truck} approaching {name} MRF"
        sent.append(realtime.notify(
            realtime.barangay_room(barangay_id), N.TRUCK_APPROACHING,
            message, title="Truck approaching", dedupe_key=key,
            barangay_id=barangay_id, truck_code=truck,
            distance_m=round(distance)))
        realtime.notify(realtime.CITY, N.TRUCK_APPROACHING, message,
                        title="Truck approaching", dedupe_key=f"{key}:city",
                        barangay_id=barangay_id, truck_code=truck)

    return sent


def _active_truck_assignment(operator_id: str) -> dict | None:
    for row in storage.find(assignment_service.TRUCK_COLLECTION,
                            operator_id=operator_id):
        if row.get("status") in assignment_service.ACTIVE_STATUSES:
            return row
    return None


# ---------------------------------------------------------------------------
# Scheduled arrival reminder (T minus 2 hours)
# ---------------------------------------------------------------------------

def check_arrival_reminders(now=None) -> list[dict]:
    """
    "2 hours from now the garbage collector TRK-01 from City Hall will arrive."

    Driven by `planned_pickup_times` on each active truck assignment. Fires
    once the current time is within the reminder window of the planned time,
    once per barangay per day.
    """
    now = now or timeutil.now()
    today = timeutil.date_str(now)
    window = timedelta(hours=Config.ARRIVAL_REMINDER_HOURS)
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    sent = []

    for assignment in storage.read(assignment_service.TRUCK_COLLECTION):
        if assignment.get("status") not in assignment_service.ACTIVE_STATUSES:
            continue

        truck = assignment.get("truck_code") or "the truck"
        for barangay_id, planned in (assignment.get("planned_pickup_times") or {}).items():
            target = _time_today(planned, now)
            if not target:
                continue

            # Only inside the window, and not once the time has passed: a
            # reminder for something that already happened is noise.
            if not (target - window <= now < target):
                continue

            key = f"arrival:{truck}:{barangay_id}:{today}"
            if N.already_sent(key):
                continue

            hours = Config.ARRIVAL_REMINDER_HOURS
            message = (f"{hours} hours from now the garbage collector {truck} "
                       f"from City Hall will arrive.")
            sent.append(realtime.notify(
                realtime.barangay_room(barangay_id), N.ARRIVAL_REMINDER,
                message, title=f"{names.get(barangay_id, 'MRF')} pickup",
                dedupe_key=key, barangay_id=barangay_id, truck_code=truck,
                planned_time=planned))

    return sent


def _time_today(hhmm: str, now):
    """Turn 'HH:MM' into a datetime on today's date, or None if malformed."""
    try:
        hour, minute = (int(part) for part in str(hhmm).split(":", 1))
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Event-driven triggers
# ---------------------------------------------------------------------------

def on_unavailable_request(request_row: dict, user: dict) -> None:
    """A collector cannot report -- the city admin must cover the route."""
    when = timeutil.display_date(request_row.get("affected_date"))
    realtime.notify(
        realtime.CITY, N.UNAVAILABLE_REQUEST,
        f"{user.get('name')} ({user.get('vehicle') or 'no vehicle'}) is "
        f"unavailable on {when}: {request_row.get('reason')}.",
        title="Unavailable for duty", actor=user.get("id"),
        request_id=request_row.get("id"), user_id=user.get("id"))


def on_assignment_changed(assignment: dict, collector_id: str,
                          kind: str, actor: str | None = None) -> None:
    """The affected collector is told what they are now working."""
    if not collector_id:
        return

    if kind == "tricycle":
        barangay = storage.find_one("barangays", id=assignment.get("barangay_id")) or {}
        detail = (f"Brgy. {barangay.get('name', '')} · "
                  f"{', '.join(assignment.get('purok_coverage') or []) or 'no puroks'} · "
                  f"{assignment.get('tricycle_code')}")
    else:
        count = len(assignment.get("covered_mrfs") or [])
        detail = f"{assignment.get('truck_code')} · {count} barangay MRF(s)"

    status = assignment.get("status")
    lead = ("You are a temporary replacement on this route"
            if status == "Temporary Replacement" else "Your route has been set")

    realtime.notify(realtime.user_room(collector_id), N.ASSIGNMENT_CHANGED,
                    f"{lead}: {detail}.", title="Assignment updated",
                    actor=actor, assignment_id=assignment.get("id"))


def on_carry_over_created(carry_over: dict, pickup: dict) -> None:
    """A missed MRF pickup needs an admin to reassign or reschedule it."""
    if not carry_over:
        return
    barangay = storage.find_one("barangays", id=carry_over.get("barangay_id")) or {}
    name = barangay.get("name", "A barangay")

    message = (f"{name} MRF was not collected"
               f"{' (' + pickup.get('reason') + ')' if pickup.get('reason') else ''}. "
               f"A carry-over is waiting to be reassigned.")
    realtime.notify(realtime.CITY, N.CARRY_OVER_CREATED, message,
                    title="Carry-over created",
                    barangay_id=carry_over.get("barangay_id"),
                    carry_over_id=carry_over.get("id"))
    realtime.notify(realtime.barangay_room(carry_over.get("barangay_id")),
                    N.CARRY_OVER_CREATED,
                    f"Your MRF was not collected today. City Hall has been "
                    f"notified and will reschedule the pickup.",
                    title="MRF not collected",
                    barangay_id=carry_over.get("barangay_id"))
    realtime.carry_over_created(carry_over)


def on_carry_over_reassigned(carry_over: dict, truck_code: str) -> None:
    """The newly assigned operator gains a stop that is not on their route."""
    operator = _operator_for_truck(truck_code)
    if not operator:
        return
    barangay = storage.find_one("barangays", id=carry_over.get("barangay_id")) or {}
    realtime.notify(
        realtime.user_room(operator["id"]), N.ASSIGNMENT_CHANGED,
        f"A carry-over pickup at {barangay.get('name', 'a barangay')} MRF has "
        f"been assigned to {truck_code}. It will appear on your MRF list.",
        title="Carry-over assigned to you",
        carry_over_id=carry_over.get("id"))


def _operator_for_truck(truck_code: str) -> dict | None:
    for row in storage.read(assignment_service.TRUCK_COLLECTION):
        if (row.get("truck_code") == truck_code
                and row.get("status") in assignment_service.ACTIVE_STATUSES):
            return storage.get("users", row.get("operator_id"))
    return None


def on_public_report(report: dict) -> None:
    """A resident has reported -- their barangay admin and the city hear it."""
    barangay = storage.find_one("barangays", id=report.get("barangay_id")) or {}
    who = report.get("owner_name") or "A resident"

    if report.get("disputed"):
        message = (f"{who} reported {report.get('status_reported')}, which "
                   f"differs from the collector's record. Please review.")
        title = "Disputed collection"
    else:
        message = (f"{who} reported their waste as "
                   f"{report.get('status_reported')}.")
        title = "Resident report"

    realtime.notify(realtime.barangay_room(report.get("barangay_id")),
                    N.PUBLIC_REPORT, message, title=title,
                    barangay_id=report.get("barangay_id"),
                    report_id=report.get("id"))
    realtime.notify(realtime.CITY, N.PUBLIC_REPORT,
                    f"{barangay.get('name', 'A barangay')}: {message}",
                    title=title, barangay_id=report.get("barangay_id"),
                    report_id=report.get("id"))


def on_delivery(delivery: dict, operator: dict) -> None:
    total = (delivery.get("load") or {}).get("total", "0")
    count = len(delivery.get("mrfs_included") or [])
    realtime.notify(
        realtime.CITY, N.DELIVERY_COMPLETED,
        f"{delivery.get('truck_code')} delivered {total} from {count} MRF(s) "
        f"to the landfill.",
        title="Delivered to landfill", actor=operator.get("id"),
        delivery_id=delivery.get("id"))
    realtime.delivery_saved(delivery)


def on_schedule_updated(actor: str | None = None) -> None:
    realtime.notify(realtime.PUBLIC, N.SCHEDULE_UPDATED,
                    "The city waste collection schedule has been updated.",
                    title="Schedule updated", actor=actor)
    realtime.schedule_updated()
