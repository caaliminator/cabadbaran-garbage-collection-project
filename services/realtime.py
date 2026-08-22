"""
The real-time layer -- Socket.IO rooms and events.

Rooms (spec section 4):

    public              everyone, logged in or not
    barangay:<id>       that barangay's admin, and collectors working it
    role:city_admin     every City Hall Admin
    user:<id>           one person

**Room membership is decided by the server from the session, never requested
by the client.** A browser that asks to join `barangay:07` gets whatever its
own account entitles it to and nothing more; otherwise anyone could subscribe
to every barangay's live vehicle positions and notifications by editing one
line of JavaScript.

Everything here degrades to a no-op when Socket.IO is not running -- during
tests, or under a WSGI server without websocket support. `emit_to()` checks
and returns quietly, so no service needs to care whether the socket layer is
up. The REST endpoints remain the 30-second polling fallback either way.
"""

import threading

from services import notification_service, storage

_socketio = None
_lock = threading.Lock()


def init(socketio) -> None:
    """Attach the SocketIO instance created in app.py."""
    global _socketio
    with _lock:
        _socketio = socketio


def is_live() -> bool:
    return _socketio is not None


def emit_to(room: str, event: str, payload: dict | None = None) -> bool:
    """
    Push an event to one room. Returns False when the socket layer is down,
    which is a normal state, not an error -- clients poll instead.
    """
    if not _socketio or not room:
        return False
    try:
        _socketio.emit(event, payload or {}, to=room)
        return True
    except Exception:                       # pragma: no cover - transport-level
        # A failed push must never break the request that triggered it: the
        # data is already saved, and the client will pick it up on its next poll.
        return False


def emit_many(rooms, event: str, payload: dict | None = None) -> int:
    seen, sent = set(), 0
    for room in rooms:
        if room and room not in seen:
            seen.add(room)
            sent += 1 if emit_to(room, event, payload) else 0
    return sent


# ---------------------------------------------------------------------------
# Who should hear about what
# ---------------------------------------------------------------------------

PUBLIC = "public"
CITY = "role:city_admin"


def barangay_room(barangay_id: str) -> str:
    return f"barangay:{barangay_id}" if barangay_id else ""


def user_room(user_id: str) -> str:
    return f"user:{user_id}" if user_id else ""


def rooms_for(user: dict | None) -> list[str]:
    """
    Every room a given session is entitled to. This is the only place that
    decides, and it reads the account, not the request.
    """
    if not user:
        return [PUBLIC]

    rooms = [PUBLIC, user_room(user["id"])]

    role = user.get("role")
    if role == "city_admin":
        rooms.append(CITY)
        # A city admin oversees every barangay.
        rooms += [barangay_room(b["id"]) for b in storage.read("barangays")]
    elif role == "barangay_admin" and user.get("barangay_id"):
        rooms.append(barangay_room(user["barangay_id"]))
    else:
        for barangay_id in user.get("barangay_ids") or []:
            rooms.append(barangay_room(barangay_id))
        if user.get("barangay_id"):
            rooms.append(barangay_room(user["barangay_id"]))

    return [r for r in dict.fromkeys(rooms) if r]


def notify(audience: str, kind: str, message: str, title: str = "",
           actor: str | None = None, **extra) -> dict:
    """
    Store a notification and push it to its audience in one step.

    Storing first is deliberate: the record is the source of truth, and the
    socket push is best-effort on top. Someone whose browser was closed still
    sees it in the bell when they return.
    """
    record = notification_service.create(audience, kind, message, title,
                                         actor, **extra)
    emit_to(audience, "notification_new", {
        "id": record["id"],
        "type": record["type"],
        "title": record["title"],
        "message": record["message"],
        "tone": record["tone"],
        "icon": record["icon"],
        "created_at": record["created_at"],
    })
    return record


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------

def collection_saved(entry: dict) -> None:
    """A household entry: the barangay's counters and the city's both move."""
    payload = {
        "property_id": entry.get("property_id"),
        "barangay_id": entry.get("barangay_id"),
        "status": entry.get("status"),
        "date": entry.get("date"),
        "tricycle_code": entry.get("tricycle_code"),
    }
    emit_many([barangay_room(entry.get("barangay_id")), CITY],
              "collection_saved", payload)


def mrf_pickup_saved(pickup: dict) -> None:
    emit_many([barangay_room(pickup.get("barangay_id")), CITY],
              "mrf_pickup_saved", {
                  "barangay_id": pickup.get("barangay_id"),
                  "status": pickup.get("status"),
                  "truck_code": pickup.get("truck_code"),
                  "date": pickup.get("date"),
                  "total": (pickup.get("load") or {}).get("total"),
              })


def delivery_saved(delivery: dict) -> None:
    emit_to(CITY, "delivery_saved", {
        "truck_code": delivery.get("truck_code"),
        "mrfs": len(delivery.get("mrfs_included") or []),
        "total": (delivery.get("load") or {}).get("total"),
        "date": delivery.get("date"),
    })


def carry_over_created(carry_over: dict) -> None:
    emit_many([barangay_room(carry_over.get("barangay_id")), CITY],
              "carry_over_created", {
                  "barangay_id": carry_over.get("barangay_id"),
                  "original_truck": carry_over.get("original_truck"),
                  "status": carry_over.get("status"),
              })


def schedule_updated() -> None:
    """Everyone reads the schedule, so everyone hears about it changing."""
    emit_to(PUBLIC, "schedule_updated", {})


def collector_status(user: dict, on_duty: bool) -> None:
    """Drives the Active Now counters and shows/hides the map marker."""
    payload = {
        "vehicle": user.get("vehicle"),
        "kind": "truck" if user.get("role") == "truck_collector" else "tricycle",
        "on_duty": on_duty,
    }
    rooms = [PUBLIC, CITY]
    rooms += [barangay_room(b) for b in (user.get("barangay_ids") or [])]
    if user.get("barangay_id"):
        rooms.append(barangay_room(user["barangay_id"]))
    emit_many(rooms, "collector_status", payload)


def location_update(user: dict, lat: float, lng: float) -> None:
    """
    Broadcast a collector's position.

    The public payload carries the vehicle code and nothing that identifies a
    person -- residents need to see where the truck is, not who is driving it.
    Admin rooms get the name as well.
    """
    public_payload = {
        "vehicle": user.get("vehicle"),
        "kind": "truck" if user.get("role") == "truck_collector" else "tricycle",
        "lat": lat,
        "lng": lng,
    }
    emit_to(PUBLIC, "location_update", public_payload)

    named = {**public_payload, "id": user.get("id"), "name": user.get("name")}
    rooms = [CITY] + [barangay_room(b) for b in (user.get("barangay_ids") or [])]
    if user.get("barangay_id"):
        rooms.append(barangay_room(user["barangay_id"]))
    emit_many(rooms, "location_update", named)
