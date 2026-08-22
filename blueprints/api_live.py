"""
Live vehicle positions for the maps.

    GET /api/live/vehicles?type=&barangay=

Scope is decided here, from the session, never from the query string:

    public / no login   on-duty vehicles, position and vehicle code only
    barangay admin      their own barangay's vehicles, with collector names
    city admin          everything
    collector           their own marker

The public map needs to show residents where the truck is, so positions are
public by design -- that is the point of the system. What is *not* public is
who is driving: an anonymous viewer gets the vehicle code and nothing that
identifies a person.

Phase 7 pushes these over a socket; this endpoint stays as the 30-second
polling fallback for when the socket is down.
"""

from flask import Blueprint, jsonify, request

from blueprints.auth import current_user
from services import duty_service, storage

api_live_bp = Blueprint("api_live", __name__)

ROLE_BY_TYPE = {
    "tricycles": "tricycle_collector",
    "trucks": "truck_collector",
}


@api_live_bp.get("/vehicles")
def vehicles():
    user = current_user()
    wanted_type = request.args.get("type", "all")
    role = ROLE_BY_TYPE.get(wanted_type)

    barangay = request.args.get("barangay") or None

    # A barangay admin is pinned to their own barangay regardless of what the
    # query string asks for; a collector only ever sees themselves.
    if user and user["role"] == "barangay_admin":
        barangay = user.get("barangay_id")
    elif user and user["role"] in ("tricycle_collector", "truck_collector"):
        rows = [c for c in duty_service.active_collectors()
                if c["id"] == user["id"]]
        return jsonify(_payload(rows, identify=True))

    rows = duty_service.active_collectors(role=role, barangay_id=barangay)
    identify = bool(user and user["role"] in ("city_admin", "barangay_admin"))
    return jsonify(_payload(rows, identify=identify))


def _payload(rows, identify: bool) -> dict:
    """
    Shape the response. Without `identify`, no name and no user id leaves the
    server -- an anonymous viewer sees "TRI-04 is here", not who is driving it.
    """
    vehicles = []
    for row in rows:
        item = {
            "vehicle": row["vehicle"],
            "kind": "truck" if row["role"] == "truck_collector" else "tricycle",
            "lat": row["lat"],
            "lng": row["lng"],
            "barangays": row["barangays"],
            "barangay_ids": row["barangay_ids"],
            "has_position": row["has_position"],
        }
        if identify:
            item["id"] = row["id"]
            item["name"] = row["name"]
            item["last_seen"] = row["last_seen"]
        vehicles.append(item)

    placed = [v for v in vehicles if v["has_position"]]
    return {
        "vehicles": placed,
        "counts": {
            "tricycles": sum(1 for v in vehicles if v["kind"] == "tricycle"),
            "trucks": sum(1 for v in vehicles if v["kind"] == "truck"),
            "total": len(vehicles),
            "without_position": len(vehicles) - len(placed),
        },
        "barangay_count": storage.count("barangays"),
    }
