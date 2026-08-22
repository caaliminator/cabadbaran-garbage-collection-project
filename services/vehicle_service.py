"""
Vehicle registry -- the TRI and TRK units the assign dropdowns draw from.

Spec section 6.3 flags this as missing logic: the assignment forms need
registered units to come from somewhere. A unit is one of three states:

    available   registered, not currently on an active assignment
    assigned    held by an active assignment
    inactive    withdrawn from service (breakdown, sold, retired)

`status` is derived from the assignment records rather than stored as the
truth, so a unit can never get stuck showing "assigned" after its assignment
is deleted. The stored field only distinguishes inactive from in-service.
"""

from config import Config
from services import storage
from services.validation import VEHICLE_RE, ValidationError

TRICYCLE = "tricycle"
TRUCK = "truck"
TYPES = (TRICYCLE, TRUCK)

PREFIXES = {TRICYCLE: "TRI", TRUCK: "TRK"}


def all_vehicles(vehicle_type: str | None = None) -> list[dict]:
    rows = storage.find("vehicles", type=vehicle_type)
    return sorted(rows, key=lambda v: v.get("code", ""))


def by_code(code: str) -> dict | None:
    return storage.find_one("vehicles", code=(code or "").strip().upper())


def assigned_codes(exclude_assignment: str | None = None) -> set[str]:
    """Vehicle codes held by an active assignment, either kind."""
    held = set()
    for collection, field in (("assignments_tricycle", "tricycle_code"),
                              ("assignments_truck", "truck_code")):
        for row in storage.read(collection):
            if row.get("id") == exclude_assignment:
                continue
            if row.get("status") in ("Active", "Temporary Replacement") and row.get(field):
                held.add(row[field])
    return held


def with_status(vehicle_type: str | None = None) -> list[dict]:
    """Every unit, each annotated with its live availability."""
    held = assigned_codes()
    out = []
    for vehicle in all_vehicles(vehicle_type):
        stored = vehicle.get("status")
        if stored == "inactive":
            state = "inactive"
        elif vehicle["code"] in held:
            state = "assigned"
        else:
            state = "available"
        out.append({**vehicle, "status": state})
    return out


def available(vehicle_type: str, including: str | None = None) -> list[str]:
    """
    Codes selectable in an assign form: free units, plus the one this
    assignment already holds (otherwise editing a row would blank its own
    vehicle).
    """
    held = assigned_codes()
    codes = [v["code"] for v in all_vehicles(vehicle_type)
             if v.get("status") != "inactive"
             and (v["code"] not in held or v["code"] == including)]
    return sorted(codes)


def in_service(vehicle_type: str) -> list[str]:
    """
    Every registered unit that has not been withdrawn, free or not.

    The assign forms only *offer* free units, but validation accepts any unit
    in service so that picking an already-taken one (from a stale page, say)
    reports "TRI-02 is already assigned to <that collector>" rather than the
    useless "not a valid choice".
    """
    return sorted(v["code"] for v in all_vehicles(vehicle_type)
                  if v.get("status") != "inactive")


def counts(vehicle_type: str) -> dict:
    rows = with_status(vehicle_type)
    return {
        "total": len(rows),
        "available": sum(1 for r in rows if r["status"] == "available"),
        "assigned": sum(1 for r in rows if r["status"] == "assigned"),
        "inactive": sum(1 for r in rows if r["status"] == "inactive"),
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def register(code: str, vehicle_type: str, note: str = "",
             actor: str | None = None) -> dict:
    code = (code or "").strip().upper()

    if vehicle_type not in TYPES:
        raise ValidationError({"type": "Choose either a tricycle or a truck."})

    if not VEHICLE_RE.match(code):
        raise ValidationError({"code": "Use the format TRI-01 or TRK-01."})

    expected = PREFIXES[vehicle_type]
    if not code.startswith(expected + "-"):
        raise ValidationError(
            {"code": f"A {vehicle_type} unit's code must start with {expected}-."})

    if by_code(code):
        raise ValidationError({"code": f"{code} is already registered."})

    if vehicle_type == TRUCK:
        # The city operates a fixed maximum fleet of trucks.
        in_service = sum(1 for v in all_vehicles(TRUCK) if v.get("status") != "inactive")
        if in_service >= Config.MAX_TRUCKS:
            raise ValidationError({"code": (
                f"The city is limited to {Config.MAX_TRUCKS} trucks and all "
                f"{in_service} are in service. Deactivate one first.")})

    return storage.insert("vehicles", {
        "code": code, "type": vehicle_type, "status": "available",
        "note": (note or "").strip(),
    }, actor)


def set_active(vehicle_id: str, active: bool, actor: str | None = None) -> dict:
    """Withdraw a unit from service, or return it."""
    vehicle = storage.get("vehicles", vehicle_id)
    if not vehicle:
        raise ValidationError({"form": "That vehicle is not registered."})

    if not active and vehicle["code"] in assigned_codes():
        raise ValidationError({"form": (
            f"{vehicle['code']} is on an active assignment. Reassign or end "
            f"that assignment before deactivating the unit.")})

    return storage.update("vehicles", vehicle_id,
                          {"status": "inactive" if not active else "available"},
                          actor)


def delete(vehicle_id: str, actor: str | None = None) -> dict:
    vehicle = storage.get("vehicles", vehicle_id)
    if not vehicle:
        raise ValidationError({"form": "That vehicle is not registered."})
    if vehicle["code"] in assigned_codes():
        raise ValidationError({"form": (
            f"{vehicle['code']} is on an active assignment and cannot be removed.")})
    return storage.delete("vehicles", vehicle_id)
