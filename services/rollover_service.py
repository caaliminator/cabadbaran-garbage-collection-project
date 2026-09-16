"""
The daily reset: what a new day clears, and what needs no clearing.

Runs lazily on the first request after midnight, next to the end-of-day freeze
(`history_service.ensure_frozen`). Same reasoning as that job: no scheduler, no
extra dependency, and it still runs correctly if the server was switched off
overnight, because it asks "is this state from an earlier day?" rather than
"did midnight just tick past?".


WHAT NEEDS NO RESET -- and why that is by design
    Collection status is not a flag that gets cleared. Every entry carries the
    date it was recorded, and every counter asks for *today's* entries:

        route_with_status()  ->  storage.find("collections", date=today)
        counts()             ->  the same rows

    A property with no entry for today reads as Pending, so at 00:00 the whole
    round is Pending again without anything being written, and yesterday's
    record stays intact in History. A reset that actually cleared statuses
    would destroy the day it was meant to close.

    The same holds for MRF pickups, deliveries and public reports: all dated,
    all naturally daily.


WHAT DOES NEED RESETTING
    Duty is the exception. `duty_status` is a single current value on the user
    record, not a dated one -- there is one duty state per collector, not one
    per day. A collector who closes the browser without tapping Off Duty stays
    On Duty forever: still counted in "Active Now", still drawn on the public
    map, still holding a position from yesterday.

    So a shift that began on an earlier day is ended here. This is the same
    thing tapping Off Duty does, minus the collector having to remember.

    Carry-overs are deliberately NOT touched. An uncollected property rolling
    into the next day is the point of that record, not stale state.
"""

from services import duty_service, storage, timeutil

ACTOR = "system"

COLLECTOR_ROLES = ("tricycle_collector", "truck_collector")


def stale_duty(today=None) -> list[dict]:
    """
    Collectors still On Duty from a shift that began before today.

    A shift with no `duty_changed_at` at all counts as stale: the flag is set
    with a timestamp every time it is toggled, so a missing one means the value
    predates that and cannot be shown to belong to today.
    """
    day = timeutil.to_date(today) or timeutil.today()

    out = []
    for user in storage.read("users"):
        if user.get("role") not in COLLECTOR_ROLES:
            continue
        if not duty_service.is_on_duty(user):
            continue
        started = timeutil.parse_stamp(user.get("duty_changed_at"))
        if started is None or started.date() < day:
            out.append(user)
    return out


def run(actor: str = ACTOR, today=None) -> dict:
    """
    Close off anything left open by an earlier day. Returns what was cleared.

    Idempotent: a second call on the same day finds nothing stale and writes
    nothing, which is what lets it sit on a per-request hook.
    """
    ended = []
    for user in stale_duty(today):
        # Straight through `set_duty` rather than a bare storage write: it also
        # clears the stale position and tells the live maps to drop the marker,
        # which is most of the reason for doing this at all.
        duty_service.set_duty(user["id"], False)
        ended.append(user["id"])

    return {"duty_ended": ended}
