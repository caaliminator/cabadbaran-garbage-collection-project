"""
Route assignments -- which collector works which barangay, and which truck
operator empties which barangay MRFs.

A tricycle assignment is barangay-wide. It used to also carry a purok
coverage list, which meant the admin picked the puroks on top of the barangay
and every consumer had to reason about a second level of scope. In practice a
tricycle works the barangay it is assigned to, so the barangay is the unit of
coverage and the purok list is gone.

Two rules run through everything here:

  * one collector holds one active assignment, and one vehicle serves one
    active assignment. Both are hard errors -- a duplicate would make the
    route lists ambiguous and double-count the collected load.
  * overlapping coverage is a *warning*, not an error. Two tricycles sharing a
    barangay during a handover is legitimate; so is two trucks sharing a
    barangay MRF when 8 trucks x 4 barangays exceeds the 31 that exist. The
    admin is told and decides.

"Temporary Replacement" is an active status: while one is in force the
replacement sees the route and the original does not (spec section 7).
"""

from config import Config
from services import storage, timeutil
from services.validation import ValidationError, Validator

ACTIVE_STATUSES = ("Active", "Temporary Replacement")
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
            if row.get("status") in ACTIVE_STATUSES and row.get(field)}


def is_unavailable(user_id: str, on_date=None) -> bool:
    """
    Does an approved unavailability request cover this date? Drives the
    Availability column and the "Unavailable Requests" counter.
    """
    day = timeutil.date_str(on_date or timeutil.today())
    for req in storage.find("unavailable_requests", user_id=user_id):
        if req.get("status") == "Resolved":
            continue
        start = req.get("affected_date")
        end = req.get("unavailable_until") or start
        if start and start <= day <= (end or start):
            return True
    return False


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
            "tricycle": row.get("tricycle_code") or "—",
            "effective_date": row.get("effective_date") or "",
            "effective_display": (timeutil.display_date(row["effective_date"])
                                  if row.get("effective_date") else "Not set"),
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
    active = [r for r in storage.read(TRICYCLE_COLLECTION)
              if r.get("status") in ACTIVE_STATUSES]
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

        unit_clash = _active_clash(TRICYCLE_COLLECTION, "tricycle_code", tricycle,
                                   assignment_id)
        if unit_clash:
            holder = users.get(unit_clash.get("collector_id"), {}).get("full_name",
                                                                       "another collector")
            v.fail("tricycle_code", f"{tricycle} is already assigned to {holder}.")

    v.raise_if_invalid()

    # Overlaps are legitimate during a handover, so they inform rather than
    # block. With coverage now barangay-wide, sharing the barangay is the
    # overlap -- there is no narrower unit left to compare.
    if status in ACTIVE_STATUSES:
        for other in storage.read(TRICYCLE_COLLECTION):
            if other.get("id") == assignment_id or other.get("status") not in ACTIVE_STATUSES:
                continue
            if other.get("barangay_id") != barangay_id:
                continue
            name = users.get(other.get("collector_id"), {}).get("full_name",
                                                                "another collector")
            v.warn(f"{barangays.get(barangay_id, {}).get('name', 'That barangay')} is "
                   f"also covered by {name} ({other.get('tricycle_code')}). "
                   f"Both collectors will see the same properties.")

    payload = {
        "collector_id": collector_id,
        "barangay_id": barangay_id,
        "tricycle_code": tricycle,
        "effective_date": effective_date,
        "status": status,
        "note": v.data["note"],
    }

    if assignment_id:
        record = storage.update(TRICYCLE_COLLECTION, assignment_id, payload, actor)
        if not record:
            raise ValidationError({"form": "That assignment no longer exists."})
    else:
        record = storage.insert(TRICYCLE_COLLECTION, payload, actor)

    _sync_collector_fields(collector_id, barangay_id, tricycle, actor)

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
            "effective_display": (timeutil.display_date(row["effective_date"])
                                  if row.get("effective_date") else "Not set"),
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
    active = [r for r in storage.read(TRUCK_COLLECTION)
              if r.get("status") in ACTIVE_STATUSES]
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

        unit_clash = _active_clash(TRUCK_COLLECTION, "truck_code", truck, assignment_id)
        if unit_clash:
            holder = users.get(unit_clash.get("operator_id"), {}).get("full_name",
                                                                      "another operator")
            v.fail("truck_code", f"{truck} is already assigned to {holder}.")

    v.raise_if_invalid()

    if status in ACTIVE_STATUSES:
        for other in storage.read(TRUCK_COLLECTION):
            if other.get("id") == assignment_id or other.get("status") not in ACTIVE_STATUSES:
                continue
            shared = set(other.get("covered_mrfs") or []) & set(covered)
            if shared:
                name = users.get(other.get("operator_id"), {}).get("full_name",
                                                                   "another operator")
                labels = sorted(barangays[b]["name"] for b in shared)
                v.warn(f"{', '.join(labels)} is also covered by {name} "
                       f"({other.get('truck_code')}). Both trucks will see it as "
                       f"a pending pickup.")

    payload = {
        "operator_id": operator_id,
        "truck_code": truck,
        "covered_mrfs": covered,
        "planned_pickup_times": planned,
        "effective_date": effective_date,
        "status": status,
        "note": v.data["note"],
    }

    if assignment_id:
        record = storage.update(TRUCK_COLLECTION, assignment_id, payload, actor)
        if not record:
            raise ValidationError({"form": "That assignment no longer exists."})
    else:
        record = storage.insert(TRUCK_COLLECTION, payload, actor)

    storage.update("users", operator_id, {
        "assigned_vehicle": truck,
        "assigned_barangays": covered,
    }, actor)

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
    return storage.update(collection, assignment_id, {"status": "Ended"}, actor)


def _active_clash(collection: str, field: str, value, exclude_id: str | None):
    if not value:
        return None
    for row in storage.read(collection):
        if row.get("id") == exclude_id:
            continue
        if row.get(field) == value and row.get("status") in ACTIVE_STATUSES:
            return row
    return None


def _sync_collector_fields(collector_id, barangay_id, vehicle, actor):
    """
    Mirror the assignment onto the account, so the collector's own profile and
    sidebar show their current route without every page re-deriving it.

    The purok fields are cleared rather than left alone: an account written
    before coverage went barangay-wide would otherwise keep showing a purok
    list that no longer scopes anything.
    """
    if not collector_id:
        return
    storage.update("users", collector_id, {
        "assigned_barangay": barangay_id,
        "assigned_vehicle": vehicle,
        "assigned_puroks": [],
        "assigned_purok": None,
    }, actor)
