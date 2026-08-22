"""
On Duty / Off Duty, and the last known position of each collector.

Spec 6.4 flags the duty toggle as missing logic. It matters for more than a
badge: going On Duty starts the browser sharing GPS, puts the collector on
every live map, and is what the "27 active now: 23 Tricycles and 4 Trucks"
counters actually count. Off Duty stops the sharing and removes the marker --
a collector who has gone home should not still appear to be working a route.

Duty state and last position live on the user record. There is no separate
collection because there is exactly one current state per collector; history
of movement is not something the system is asked to keep, and storing a
position stream would be a meaningful privacy decision nobody has made.
"""

from services import storage, timeutil

ON_DUTY = "On Duty"
OFF_DUTY = "Off Duty"

# A position older than this is treated as stale: the phone lost signal, the
# browser was closed, or the battery died. Better to drop the marker than to
# show a truck parked somewhere it left an hour ago.
STALE_AFTER_MINUTES = 10


def is_on_duty(user: dict) -> bool:
    return (user or {}).get("duty_status") == ON_DUTY


def set_duty(user_id: str, on_duty: bool) -> dict:
    """Toggle duty. Going off duty clears the position along with the flag."""
    changes = {
        "duty_status": ON_DUTY if on_duty else OFF_DUTY,
        "duty_changed_at": timeutil.stamp(),
    }
    if not on_duty:
        changes["last_location"] = None
    updated = storage.update("users", user_id, changes, user_id)

    if updated:
        from services import auth_service, realtime
        realtime.collector_status(auth_service.public_view(updated), on_duty)
    return updated


def record_location(user_id: str, lat, lng, accuracy=None) -> dict | None:
    """
    Store a collector's latest position.

    Only accepted while they are on duty: a phone that keeps reporting after
    the shift ended should not keep a marker alive on the public map.
    """
    user = storage.get("users", user_id)
    if not user or not is_on_duty(user):
        return None

    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return None

    return storage.update("users", user_id, {
        "last_location": {
            "lat": round(lat_f, 6),
            "lng": round(lng_f, 6),
            "accuracy": round(float(accuracy), 1) if accuracy not in (None, "") else None,
            "at": timeutil.stamp(),
        },
    }, user_id)


def active_collectors(role: str | None = None,
                      barangay_id: str | None = None) -> list[dict]:
    """
    On-duty collectors with a fresh position -- what the live maps draw and the
    "Active Now" counters count.
    """
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    now = timeutil.now()
    out = []

    for user in storage.read("users"):
        if role and user.get("role") != role:
            continue
        if user.get("status") != "Active" or not is_on_duty(user):
            continue

        covered = user.get("assigned_barangays") or []
        if user.get("assigned_barangay"):
            covered = [user["assigned_barangay"]] + [
                b for b in covered if b != user["assigned_barangay"]]
        if barangay_id and barangay_id not in covered:
            continue

        location = user.get("last_location") or {}
        seen = timeutil.parse_stamp(location.get("at"))
        fresh = bool(seen and (now - seen).total_seconds() <= STALE_AFTER_MINUTES * 60)

        out.append({
            "id": user["id"],
            "name": user.get("full_name"),
            "role": user.get("role"),
            "vehicle": user.get("assigned_vehicle"),
            "barangay_ids": covered,
            "barangays": [names.get(b) for b in covered if names.get(b)],
            "lat": location.get("lat") if fresh else None,
            "lng": location.get("lng") if fresh else None,
            "last_seen": location.get("at"),
            "has_position": fresh,
        })

    return out


def active_counts(barangay_id: str | None = None) -> dict:
    """The dashboard's "23 Tricycles / 4 Trucks active now" card."""
    tricycles = active_collectors("tricycle_collector", barangay_id)
    trucks = active_collectors("truck_collector", barangay_id)
    return {
        "tricycles": len(tricycles),
        "trucks": len(trucks),
        "total": len(tricycles) + len(trucks),
        "with_position": sum(1 for c in tricycles + trucks if c["has_position"]),
    }
