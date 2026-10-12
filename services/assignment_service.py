"""
Route assignments -- which collector works which puroks of a barangay, and
which truck operator empties which barangay MRFs.

A tricycle assignment covers at least one purok of its barangay; a truck
assignment covers at least one barangay MRF. The rules, all hard errors:

  * one collector holds one active assignment, and one vehicle serves one
    active assignment -- a duplicate would make the route lists ambiguous and
    double-count the collected load.
  * one purok belongs to one tricycle route, and one MRF to one truck route,
    so every household and every MRF has exactly one collector answerable
    for it. The assign forms only offer what is still free.

"Temporary Replacement" is an active status: while one is in force the
replacement sees the route and the original does not (spec section 7).

A stand-in drives the absent collector's own vehicle and works their puroks or
MRFs, so a replacement shares all of that with the regular route it covers.
Only two assignments of the same kind clash -- two regular routes always, two
replacements when their dates overlap. While the replacement is in force the stand-in's account
carries the route's barangay and vehicle; when it ends the account goes back
to what it was (sync_replacement_accounts).
"""

from config import Config
from services import storage, timeutil
from services.validation import ValidationError, Validator

REPLACEMENT = "Temporary Replacement"
ACTIVE_STATUSES = ("Active", REPLACEMENT)
STATUS_CHOICES = ("Active", "Temporary Replacement", "Ended")

TRICYCLE_COLLECTION = "assignments_tricycle"
TRUCK_COLLECTION = "assignments_truck"


def _barangay_names() -> dict[str, str]:
    return {b["id"]: b["name"] for b in storage.read("barangays")}


def _user_names() -> dict[str, dict]:
    return {u["id"]: u for u in storage.read("users")}


def assigned_collector_ids(role: str) -> set[str]:
    """Collectors already holding an active assignment of the matching kind."""
    collection, field = ((TRICYCLE_COLLECTION, "collector_id")
                         if role == "tricycle_collector"
                         else (TRUCK_COLLECTION, "operator_id"))
    return {row[field] for row in storage.read(collection)
            if is_active(row) and row.get(field)}


def is_unavailable(user_id: str, on_date=None) -> bool:
    """
    Is this collector off the route on this date? Drives the Availability
    column and the "Unavailable Requests" counter.

    Pending and Approved both count; Rejected does not. A request the admin
    turned down must put the collector back on the route, which is the point
    of being able to reject one.
    """
    from services import unavailable_service

    day = timeutil.date_str(on_date or timeutil.today())
    for req in storage.find("unavailable_requests", user_id=user_id):
        if req.get("status") not in unavailable_service.BLOCKING:
            continue
        start = req.get("affected_date")
        end = req.get("unavailable_until") or start
        if start and start <= day <= (end or start):
            return True
    return False


def absence_window(user_id: str, on_date=None) -> tuple[str, str] | None:
    """
    (first day, last day) of this collector's nearest absence that is running
    or still to come, counting requests the same way is_unavailable() does --
    Pending and Approved. None if they have nothing ahead.
    """
    from services import unavailable_service

    if not user_id:
        return None
    day = timeutil.date_str(on_date or timeutil.today())
    windows = []
    for req in storage.find("unavailable_requests", user_id=user_id):
        if req.get("status") not in unavailable_service.BLOCKING:
            continue
        start = req.get("affected_date")
        end = req.get("unavailable_until") or start
        if start and end >= day:
            windows.append((start, end))
    return min(windows) if windows else None


def is_active(row: dict, on_date=None) -> bool:
    """
    Is this assignment in force?

    Status alone is not enough once a Temporary Replacement carries an end
    date: a replacement whose last day has passed is history, and treating it
    as active would keep the stand-in on the route and the original collector
    off it indefinitely. Everything that asks "who works this barangay" goes
    through here so they all answer the same way.
    """
    if row.get("status") not in ACTIVE_STATUSES:
        return False
    until = row.get("until_date")
    if until and until < timeutil.date_str(on_date or timeutil.today()):
        return False
    return True


def _effective_display(row: dict) -> str:
    """
    The Effective From cell. A date only where it tells the admin something:
    a Temporary Replacement (when the stand-in took over), or a handover set
    for a future day (when it will happen -- the Scheduled badge beside it is
    meaningless without one). A collector's own standing route shows a dash;
    its start date is just the day the account was set up.
    """
    date = row.get("effective_date")
    scheduled = bool(date and date > timeutil.today_str())
    if row.get("status") != "Temporary Replacement" and not scheduled:
        return "—"
    return timeutil.display_date(date) if date else "Not set"


def _until_display(row: dict) -> str:
    """Replacement Until: only a replacement has one to show."""
    if row.get("until_date"):
        return timeutil.display_date(row["until_date"])
    return "No end date" if row.get("status") == "Temporary Replacement" else "—"


def in_force(row: dict, on_date=None) -> bool:
    """Active and already started -- is_active() plus the start date."""
    if not is_active(row, on_date):
        return False
    start = row.get("effective_date")
    return not start or start <= timeutil.date_str(on_date or timeutil.today())


def _fields(collection: str) -> tuple[str, str]:
    """(who, vehicle) field names for an assignment collection."""
    if collection == TRICYCLE_COLLECTION:
        return "collector_id", "tricycle_code"
    return "operator_id", "truck_code"


def covering_replacement(collection: str, row: dict, on_date=None) -> dict | None:
    """
    The Temporary Replacement in force on this regular route's vehicle, if
    any. While there is one, the stand-in works the route and the regular
    holder does not.
    """
    if not row or row.get("status") == REPLACEMENT:
        return None
    _, vehicle = _fields(collection)
    if not row.get(vehicle):
        return None
    for other in storage.read(collection):
        if (other.get("id") != row.get("id") and other.get("status") == REPLACEMENT
                and other.get(vehicle) == row[vehicle] and in_force(other, on_date)):
            return other
    return None


def working_assignment(collection: str, user_id: str, on_date=None) -> dict | None:
    """
    The route this collector actually works on a date: in force, and not
    handed to a stand-in for the day. Both collector portals read their route
    from here, so an assignment set for next week does not show up early and
    a collector on covered leave does not see the route the stand-in has.
    """
    if not user_id:
        return None
    who, _ = _fields(collection)
    for row in storage.find(collection, **{who: user_id}):
        if in_force(row, on_date) and not covering_replacement(collection, row, on_date):
            return row
    return None


def vehicle_worker(collection: str, code: str, on_date=None) -> dict | None:
    """
    The assignment whose collector is driving this vehicle on a date: an
    in-force replacement over the regular route it covers.
    """
    _, vehicle = _fields(collection)
    regular = None
    for row in storage.read(collection):
        if row.get(vehicle) != code or not in_force(row, on_date):
            continue
        if row.get("status") == REPLACEMENT:
            return row
        regular = regular or row
    return regular


def regular_route(collection: str, user_id: str) -> dict | None:
    """
    A collector's own standing route -- what a replacement covering their
    absence prefills from.
    """
    who, _ = _fields(collection)
    for row in storage.find(collection, **{who: user_id}):
        if row.get("status") == "Active" and is_active(row):
            return row
    return None


# ---------------------------------------------------------------------------
# Tricycle assignments
# ---------------------------------------------------------------------------

def tricycle_listing(search: str = "", barangay: str = "", status: str = "",
                     availability: str = "") -> list[dict]:
    names, users = _barangay_names(), _user_names()
    needle = (search or "").strip().lower()

    rows = []
    for row in storage.read(TRICYCLE_COLLECTION):
        user = users.get(row.get("collector_id")) or {}
        available = "Unavailable" if is_unavailable(row.get("collector_id")) else "Available"

        if barangay and row.get("barangay_id") != barangay:
            continue
        if status and row.get("status") != status:
            continue
        if availability and available != availability:
            continue
        if needle and needle not in (f"{user.get('full_name', '')} "
                                     f"{user.get('username', '')}").lower():
            continue

        rows.append({
            "id": row["id"],
            "collector_id": row.get("collector_id"),
            "collector": user.get("full_name") or "Deleted account",
            "username": user.get("username") or "—",
            "barangay_id": row.get("barangay_id"),
            "barangay": names.get(row.get("barangay_id")) or "—",
            "puroks": row.get("purok_coverage") or [],
            "tricycle": row.get("tricycle_code") or "—",
            "effective_date": row.get("effective_date") or "",
            "effective_display": _effective_display(row),
            "until_date": row.get("until_date") or "",
            "until_display": _until_display(row),
            "expired": bool(row.get("until_date")
                            and row["until_date"] < timeutil.today_str()
                            and row.get("status") in ACTIVE_STATUSES),
            "pending_start": bool(row.get("effective_date")
                                  and row["effective_date"] > timeutil.today_str()),
            "availability": available,
            "status": row.get("status", "Active"),
            "note": row.get("note") or "",
            "created_at": row.get("created_at"),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def tricycle_counts() -> dict:
    """The four KPI cards on the Tricycle page."""
    collectors = [u for u in storage.read("users")
                  if u.get("role") == "tricycle_collector" and u.get("status") == "Active"]
    active = [r for r in storage.read(TRICYCLE_COLLECTION) if is_active(r)]
    covered = {r.get("barangay_id") for r in active if r.get("barangay_id")}
    unavailable = sum(1 for c in collectors if is_unavailable(c["id"]))

    return {
        "total_collectors": len(collectors),
        "active_assignments": len(active),
        "unavailable_requests": unavailable,
        "barangays_covered": len(covered),
        "total_barangays": Config.TOTAL_BARANGAYS,
    }


def save_tricycle_assignment(form, assignment_id: str | None = None,
                             actor: str | None = None) -> tuple[dict, list[str]]:
    """Create or update. Returns (record, warnings)."""
    v = Validator(form)
    barangays = {b["id"]: b for b in storage.read("barangays")}
    users = _user_names()

    collector_id = v.choice("collector_id", "Collector",
                            [u["id"] for u in storage.read("users")
                             if u.get("role") == "tricycle_collector"])
    barangay_id = v.choice("barangay_id", "Assigned Barangay", list(barangays))
    # At least one purok, and only ones that exist in that barangay.
    puroks = v.multi("purok_coverage", "Purok",
                     (barangays.get(barangay_id) or {}).get("puroks") or [])

    from services import vehicle_service
    # Accept any in-service unit here; the clash check below produces the
    # message that actually names who is holding it.
    tricycle = v.choice("tricycle_code", "Tricycle",
                        vehicle_service.in_service(vehicle_service.TRICYCLE))
    status = v.choice("status", "Status", STATUS_CHOICES)
    # When the route actually changes hands. Required, because "who is working
    # this barangay" is only answerable with a date attached -- a replacement
    # that starts on Monday is not the same fact as one starting today.
    effective_date = v.date("effective_date", "Date of Effectivity", required=True)
    until_date = _read_until_date(v, status, effective_date)
    v.text("note", "Note", max_length=500)

    collector = users.get(collector_id) or {}
    if collector_id and collector.get("status") != "Active":
        v.fail("collector_id", "That collector's account is inactive.")

    if status in ACTIVE_STATUSES:
        clash = _active_clash(TRICYCLE_COLLECTION, "collector_id", collector_id,
                              assignment_id)
        if clash:
            v.fail("collector_id", (
                f"{collector.get('full_name', 'That collector')} already holds an "
                f"active assignment in "
                f"{barangays.get(clash.get('barangay_id'), {}).get('name', 'another barangay')}. "
                f"End or edit that assignment first."))

        unit_clash = _unit_clash(TRICYCLE_COLLECTION, tricycle, assignment_id,
                                 status, effective_date, until_date)
        if unit_clash:
            holder = users.get(unit_clash.get("collector_id"), {}).get("full_name",
                                                                       "another collector")
            v.fail("tricycle_code", _unit_clash_message(tricycle, holder, unit_clash))

        for other in _clashing_rows(TRICYCLE_COLLECTION, assignment_id, status,
                                    effective_date, until_date):
            if other.get("barangay_id") != barangay_id:
                continue
            shared = [p for p in puroks if p in (other.get("purok_coverage") or [])]
            if shared:
                holder = users.get(other.get("collector_id"), {}).get(
                    "full_name", "another collector")
                v.fail("purok_coverage", _coverage_message(shared, holder, other))
                break

    v.raise_if_invalid()

    payload = {
        "collector_id": collector_id,
        "barangay_id": barangay_id,
        "purok_coverage": puroks,
        "tricycle_code": tricycle,
        "effective_date": effective_date,
        "until_date": until_date,
        "status": status,
        "note": v.data["note"],
    }

    if assignment_id:
        record = storage.update(TRICYCLE_COLLECTION, assignment_id, payload, actor)
        if not record:
            raise ValidationError({"form": "That assignment no longer exists."})
    else:
        record = storage.insert(TRICYCLE_COLLECTION, payload, actor)

    if status == REPLACEMENT:
        # The account follows the replacement while it is in force, and goes
        # back afterwards -- not overwritten now for a cover that starts later.
        _remember_account(TRICYCLE_COLLECTION, record, actor)
        sync_replacement_accounts(actor)
    else:
        _sync_collector_fields(collector_id, barangay_id, tricycle, puroks, actor)
        _settle_former_replacement(TRICYCLE_COLLECTION, record, actor)

    from services import triggers
    triggers.on_assignment_changed(record, collector_id, "tricycle", actor)
    return record, v.warnings


# ---------------------------------------------------------------------------
# Truck assignments
# ---------------------------------------------------------------------------

def truck_listing(search: str = "", status: str = "",
                  availability: str = "") -> list[dict]:
    names, users = _barangay_names(), _user_names()
    needle = (search or "").strip().lower()

    rows = []
    for row in storage.read(TRUCK_COLLECTION):
        user = users.get(row.get("operator_id")) or {}
        available = "Unavailable" if is_unavailable(row.get("operator_id")) else "Available"

        if status and row.get("status") != status:
            continue
        if availability and available != availability:
            continue
        if needle and needle not in (f"{user.get('full_name', '')} "
                                     f"{user.get('username', '')}").lower():
            continue

        covered = row.get("covered_mrfs") or []
        rows.append({
            "id": row["id"],
            "operator_id": row.get("operator_id"),
            "operator": user.get("full_name") or "Deleted account",
            "username": user.get("username") or "—",
            "mrf_ids": covered,
            "mrfs": [names.get(b) for b in covered if names.get(b)],
            "truck": row.get("truck_code") or "—",
            "effective_date": row.get("effective_date") or "",
            "effective_display": _effective_display(row),
            "until_date": row.get("until_date") or "",
            "until_display": _until_display(row),
            "expired": bool(row.get("until_date")
                            and row["until_date"] < timeutil.today_str()
                            and row.get("status") in ACTIVE_STATUSES),
            "pending_start": bool(row.get("effective_date")
                                  and row["effective_date"] > timeutil.today_str()),
            "planned_pickup_times": row.get("planned_pickup_times") or {},
            "availability": available,
            "status": row.get("status", "Active"),
            "note": row.get("note") or "",
            "created_at": row.get("created_at"),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def truck_counts() -> dict:
    """
    The four KPI cards on the Truck page, plus the coverage gap.

    8 trucks x 4 barangays is 32 against 31 barangays, so exact coverage needs
    one truck on 3. `uncovered` is what makes that visible instead of letting
    a barangay quietly go unserviced.
    """
    from services import vehicle_service

    operators = [u for u in storage.read("users")
                 if u.get("role") == "truck_collector" and u.get("status") == "Active"]
    active = [r for r in storage.read(TRUCK_COLLECTION) if is_active(r)]
    covered = {b for r in active for b in (r.get("covered_mrfs") or [])}
    all_barangays = {b["id"] for b in storage.read("barangays")}
    unavailable = sum(1 for o in operators if is_unavailable(o["id"]))

    return {
        "total_trucks": vehicle_service.counts(vehicle_service.TRUCK)["total"],
        "max_trucks": Config.MAX_TRUCKS,
        "total_operators": len(operators),
        "active_assignments": len(active),
        "unavailable_requests": unavailable,
        "mrfs_covered": len(covered),
        "total_barangays": Config.TOTAL_BARANGAYS,
        "uncovered": sorted(all_barangays - covered),
        "fully_covered": not (all_barangays - covered),
    }


def uncovered_barangays() -> list[dict]:
    """Barangays no active truck assignment currently picks up from."""
    names = _barangay_names()
    return [{"id": b, "name": names.get(b, b)} for b in truck_counts()["uncovered"]]


def save_truck_assignment(form, assignment_id: str | None = None,
                          actor: str | None = None) -> tuple[dict, list[str]]:
    v = Validator(form)
    barangays = {b["id"]: b for b in storage.read("barangays")}
    users = _user_names()

    operator_id = v.choice("operator_id", "Truck Operator",
                           [u["id"] for u in storage.read("users")
                            if u.get("role") == "truck_collector"])

    from services import vehicle_service
    truck = v.choice("truck_code", "Truck",
                     vehicle_service.in_service(vehicle_service.TRUCK))
    covered = v.multi("covered_mrfs", "Barangay MRF", list(barangays))
    status = v.choice("status", "Status", STATUS_CHOICES)
    effective_date = v.date("effective_date", "Date of Effectivity", required=True)
    until_date = _read_until_date(v, status, effective_date)
    v.text("note", "Note", max_length=500)

    # Optional planned pickup time per covered MRF, for the T-2h reminders.
    planned = {}
    for barangay_id in covered:
        raw = str(form.get(f"planned_time__{barangay_id}", "") or "").strip()
        if not raw:
            continue
        from services.validation import TIME_RE
        if not TIME_RE.match(raw):
            v.fail(f"planned_time__{barangay_id}",
                   f"Pickup time for {barangays[barangay_id]['name']} must be a "
                   f"24-hour time, e.g. 08:30.")
        else:
            planned[barangay_id] = raw

    operator = users.get(operator_id) or {}
    if operator_id and operator.get("status") != "Active":
        v.fail("operator_id", "That operator's account is inactive.")

    if status in ACTIVE_STATUSES:
        clash = _active_clash(TRUCK_COLLECTION, "operator_id", operator_id, assignment_id)
        if clash:
            v.fail("operator_id",
                   f"{operator.get('full_name', 'That operator')} already holds an "
                   f"active assignment on {clash.get('truck_code')}. "
                   f"End or edit that assignment first.")

        unit_clash = _unit_clash(TRUCK_COLLECTION, truck, assignment_id,
                                 status, effective_date, until_date)
        if unit_clash:
            holder = users.get(unit_clash.get("operator_id"), {}).get("full_name",
                                                                      "another operator")
            v.fail("truck_code", _unit_clash_message(truck, holder, unit_clash))

        for other in _clashing_rows(TRUCK_COLLECTION, assignment_id, status,
                                    effective_date, until_date):
            shared = [b for b in covered if b in (other.get("covered_mrfs") or [])]
            if shared:
                holder = users.get(other.get("operator_id"), {}).get(
                    "full_name", "another operator")
                labels = [f"{barangays[b]['name']} MRF" for b in shared]
                v.fail("covered_mrfs", _coverage_message(labels, holder, other))
                break

    v.raise_if_invalid()

    payload = {
        "operator_id": operator_id,
        "truck_code": truck,
        "covered_mrfs": covered,
        "planned_pickup_times": planned,
        "effective_date": effective_date,
        "until_date": until_date,
        "status": status,
        "note": v.data["note"],
    }

    if assignment_id:
        record = storage.update(TRUCK_COLLECTION, assignment_id, payload, actor)
        if not record:
            raise ValidationError({"form": "That assignment no longer exists."})
    else:
        record = storage.insert(TRUCK_COLLECTION, payload, actor)

    if status == REPLACEMENT:
        _remember_account(TRUCK_COLLECTION, record, actor)
        sync_replacement_accounts(actor)
    else:
        storage.update("users", operator_id, {
            "assigned_vehicle": truck,
            "assigned_barangays": covered,
        }, actor)
        _settle_former_replacement(TRUCK_COLLECTION, record, actor)

    from services import triggers
    triggers.on_assignment_changed(record, operator_id, "truck", actor)

    # Say what is still unserviced right after a save, while the admin is
    # looking at the page and can act on it.
    remaining = uncovered_barangays()
    if remaining and status in ACTIVE_STATUSES:
        listed = ", ".join(b["name"] for b in remaining[:4])
        more = f" and {len(remaining) - 4} more" if len(remaining) > 4 else ""
        v.warn(f"{len(remaining)} barangay MRF(s) still have no assigned truck: "
               f"{listed}{more}.")

    return record, v.warnings


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

def get(collection: str, assignment_id: str) -> dict | None:
    return storage.get(collection, assignment_id)


def end_assignment(collection: str, assignment_id: str,
                   actor: str | None = None) -> dict | None:
    """Ending frees the collector and the vehicle without losing the history."""
    record = storage.update(collection, assignment_id, {"status": "Ended"}, actor)
    # A stand-in's account goes back to what it was the moment cover ends.
    sync_replacement_accounts(actor)
    return record


def _read_until_date(v, status: str, effective_date):
    """
    "Until when" for a Temporary Replacement.

    Required for that status and only that status: a stand-in covers a known
    absence, and one with no end date is how a temporary arrangement quietly
    becomes permanent. A permanent Active assignment has no end, so asking for
    one there would be noise.
    """
    until = v.date("until_date", "Replacement until")
    if status != "Temporary Replacement":
        return None
    if not until:
        v.fail("until_date",
               "A temporary replacement needs an end date -- the last day the "
               "stand-in works this route.")
        return None
    if effective_date and until < effective_date:
        v.fail("until_date", "The end date cannot be before the start date.")
        return None
    return until


def _active_clash(collection: str, field: str, value, exclude_id: str | None):
    if not value:
        return None
    for row in storage.read(collection):
        if row.get("id") == exclude_id:
            continue
        if row.get(field) == value and is_active(row):
            return row
    return None


def _unit_clash(collection: str, code, exclude_id: str | None, status: str,
                start: str | None, until: str | None) -> dict | None:
    """
    Another assignment that cannot share this vehicle.

    A replacement and the regular route it covers share one by design -- the
    stand-in drives the absent collector's unit. Two regular routes clash
    outright; two replacements clash only if their dates overlap, so cover
    for next week can be lined up while this week's is still running.
    """
    if not code:
        return None
    _, vehicle = _fields(collection)
    return next((row for row in _clashing_rows(collection, exclude_id, status, start, until)
                 if row.get(vehicle) == code), None)


def _clashing_rows(collection: str, exclude_id: str | None, status: str,
                   start: str | None, until: str | None):
    """
    Active assignments that may not share a vehicle, a purok or an MRF with
    one being saved: the same kind -- regular with regular, replacement with
    replacement -- and, for two replacements, overlapping dates. A replacement
    and the regular route it covers share everything by design.
    """
    for row in storage.read(collection):
        if row.get("id") == exclude_id or not is_active(row):
            continue
        if (row.get("status") == REPLACEMENT) != (status == REPLACEMENT):
            continue
        if status == REPLACEMENT:
            other_start = row.get("effective_date") or ""
            other_until = row.get("until_date") or "9999-12-31"
            if not ((start or "") <= other_until
                    and other_start <= (until or "9999-12-31")):
                continue
        yield row


def _coverage_message(names: list[str], holder: str, row: dict) -> str:
    listed = ", ".join(names)
    verb = "is" if len(names) == 1 else "are"
    if row.get("status") == REPLACEMENT:
        return (f"{listed} {verb} already covered by {holder} as a temporary "
                f"replacement on overlapping dates.")
    return f"{listed} {verb} already assigned to {holder}."


def _route_absence(collection: str, row: dict) -> str:
    """
    For a regular route whose collector is unavailable and not yet covered,
    a short note on when -- the routes a Temporary Replacement can take over.
    Blank for every other route.
    """
    if row.get("status") != "Active" or not is_active(row):
        return ""
    who, vehicle = _fields(collection)
    covered = any(r.get("status") == REPLACEMENT and is_active(r)
                  and r.get(vehicle) == row.get(vehicle)
                  for r in storage.read(collection))
    window = None if covered else absence_window(row.get(who))
    if not window:
        return ""
    start, end = (timeutil.display_date(d) for d in window)
    return f"unavailable {start}" if start == end else f"unavailable {start} to {end}"


def purok_options() -> dict[str, list[dict]]:
    """
    For the assign form: barangay id -> its puroks, each saying who holds it.
    A purok on a regular route is not offered at all -- unless that route's
    collector is unavailable, when it is offered for a Temporary Replacement
    only. Replacement routes are left out: they borrow the puroks they cover.
    """
    users = {u["id"]: u.get("full_name") for u in storage.read("users")}
    held = {}
    for row in storage.read(TRICYCLE_COLLECTION):
        if row.get("status") != "Active" or not is_active(row):
            continue
        absence = _route_absence(TRICYCLE_COLLECTION, row)
        for purok in row.get("purok_coverage") or []:
            held[(row.get("barangay_id"), purok)] = {
                "holder": users.get(row.get("collector_id")) or "another collector",
                "holder_id": row["id"], "absence": absence}

    out = {}
    for brgy in sorted(storage.read("barangays"), key=lambda b: b.get("number", 0)):
        out[brgy["id"]] = [
            {"name": purok, **held.get((brgy["id"], purok),
                                       {"holder": "", "holder_id": "", "absence": ""})}
            for purok in brgy.get("puroks") or []]
    return out


def mrf_options() -> dict[str, dict]:
    """
    For the truck forms: barangay id -> who holds its MRF on a regular route,
    and, if that operator is unavailable and uncovered, when. Free MRFs are
    absent from the map.
    """
    users = {u["id"]: u.get("full_name") for u in storage.read("users")}
    out = {}
    for row in storage.read(TRUCK_COLLECTION):
        if row.get("status") != "Active" or not is_active(row):
            continue
        absence = _route_absence(TRUCK_COLLECTION, row)
        for barangay_id in row.get("covered_mrfs") or []:
            out[barangay_id] = {
                "holder": users.get(row.get("operator_id")) or "another operator",
                "holder_id": row["id"], "truck": row.get("truck_code"),
                "absence": absence}
    return out


def _unit_clash_message(code: str, holder: str, row: dict) -> str:
    if row.get("status") == REPLACEMENT:
        return (f"{code} is already covered by {holder} as a temporary "
                f"replacement on overlapping dates.")
    return f"{code} is already assigned to {holder}."


# What a replacement changes on the stand-in's account, and restores after.
ACCOUNT_FIELDS = ("assigned_barangay", "assigned_barangays", "assigned_vehicle",
                  "assigned_puroks", "assigned_purok")


def _remember_account(collection: str, record: dict, actor) -> None:
    """
    Keep the stand-in's account as it was before this replacement, once, so
    it can be put back when the cover ends. An edit that keeps the same
    stand-in keeps the original snapshot; a new stand-in gets a fresh one.
    """
    who, _ = _fields(collection)
    before = record.get("account_before") or {}
    if before and before.get("user_id") == record.get(who):
        return
    user = storage.get("users", record.get(who)) or {}
    storage.update(collection, record["id"], {
        "account_before": {"user_id": record.get(who),
                           **{f: user.get(f) for f in ACCOUNT_FIELDS}},
        "account_applied": False,
        "account_restored": False,
    }, actor)


def sync_replacement_accounts(actor=None, on_date=None) -> int:
    """
    Point each stand-in's account at the route they are covering while the
    replacement is in force, and back to what it was once it is over.

    The account's vehicle and barangay are what the collector's profile, the
    live map and every recorded entry read, so they have to follow the cover.
    Idempotent and cheap -- it rides the daily rollover hook, so a cover that
    starts tomorrow is applied on the first request of tomorrow.
    """
    changed = 0
    for collection in (TRICYCLE_COLLECTION, TRUCK_COLLECTION):
        who, vehicle = _fields(collection)
        for row in storage.read(collection):
            before = row.get("account_before")
            if not before:
                continue
            user_id = row.get(who)
            if row.get("status") == REPLACEMENT and in_force(row, on_date):
                if row.get("account_applied"):
                    continue
                if collection == TRICYCLE_COLLECTION:
                    puroks = row.get("purok_coverage") or []
                    fields = {"assigned_barangay": row.get("barangay_id"),
                              "assigned_vehicle": row.get(vehicle),
                              "assigned_puroks": puroks,
                              "assigned_purok": puroks[0] if puroks else None}
                else:
                    fields = {"assigned_vehicle": row.get(vehicle),
                              "assigned_barangays": row.get("covered_mrfs") or []}
                storage.update("users", user_id, fields, actor)
                storage.update(collection, row["id"], {"account_applied": True}, actor)
                changed += 1
            elif (not is_active(row, on_date) and row.get("account_applied")
                  and not row.get("account_restored")):
                if before.get("user_id") == user_id:
                    storage.update("users", user_id,
                                   {f: before.get(f) for f in ACCOUNT_FIELDS}, actor)
                storage.update(collection, row["id"], {"account_restored": True}, actor)
                changed += 1
    return changed


def _settle_former_replacement(collection: str, record: dict, actor) -> None:
    """
    A replacement edited into another status. Ended puts the stand-in's
    account back; made permanent (Active), the route is theirs now and the
    old snapshot is dropped instead.
    """
    if not record.get("account_before") or record.get("account_restored"):
        return
    if record.get("status") == "Ended":
        sync_replacement_accounts(actor)
    else:
        storage.update(collection, record["id"], {"account_restored": True}, actor)


def replacement_account_ids() -> set[str]:
    """Accounts currently carrying a vehicle borrowed through a replacement."""
    out = set()
    for collection in (TRICYCLE_COLLECTION, TRUCK_COLLECTION):
        who, _ = _fields(collection)
        for row in storage.read(collection):
            if row.get("account_applied") and not row.get("account_restored"):
                out.add(row.get(who))
    return out


def _sync_collector_fields(collector_id, barangay_id, vehicle, puroks, actor):
    """
    Mirror the assignment onto the account, so the collector's own profile and
    sidebar show their current route without every page re-deriving it.
    """
    if not collector_id:
        return
    storage.update("users", collector_id, {
        "assigned_barangay": barangay_id,
        "assigned_vehicle": vehicle,
        "assigned_puroks": list(puroks or []),
        "assigned_purok": puroks[0] if puroks else None,
    }, actor)
