"""
Notifications -- the bell, and the barangay dashboard's alerts card.

Every notification is addressed to an **audience**, not to a list of people:

    public              everyone, including the public viewer
    role:city_admin     every City Hall Admin
    barangay:<id>       that barangay's admin
    user:<id>           one person

Addressing by audience rather than by recipient is what lets a notification
survive staff changes: "the Brgy 14 admin" is a role someone holds, and a new
admin should see the alerts for their barangay without anything being rewritten.

Read state is per person (`read_by`), so one admin marking an alert read does
not hide it from their colleague.

Phase 7 adds the automatic triggers (truck approaching, scheduled arrival,
assignment changed, and so on) on top of `create()`.
"""

from services import storage, timeutil

PUBLIC = "public"

# Notification types, used for the icon and tone in the UI.
TRUCK_APPROACHING = "truck_approaching"
ARRIVAL_REMINDER = "arrival_reminder"
UNAVAILABLE_REQUEST = "unavailable_request"
ASSIGNMENT_CHANGED = "assignment_changed"
CARRY_OVER_CREATED = "carry_over_created"
PUBLIC_REPORT = "public_report"
DELIVERY_COMPLETED = "delivery_completed"
SCHEDULE_UPDATED = "schedule_updated"

TONES = {
    TRUCK_APPROACHING: "info",
    ARRIVAL_REMINDER: "info",
    UNAVAILABLE_REQUEST: "warning",
    ASSIGNMENT_CHANGED: "info",
    CARRY_OVER_CREATED: "danger",
    PUBLIC_REPORT: "warning",
    DELIVERY_COMPLETED: "success",
    SCHEDULE_UPDATED: "info",
}

ICONS = {
    TRUCK_APPROACHING: "truck",
    ARRIVAL_REMINDER: "clock",
    UNAVAILABLE_REQUEST: "alert",
    ASSIGNMENT_CHANGED: "users",
    CARRY_OVER_CREATED: "repeat",
    PUBLIC_REPORT: "home",
    DELIVERY_COMPLETED: "check",
    SCHEDULE_UPDATED: "calendar",
}


def audiences_for(user: dict | None) -> list[str]:
    """Which audience tags a given viewer should receive."""
    if not user:
        return [PUBLIC]

    tags = [PUBLIC, f"user:{user['id']}", f"role:{user['role']}"]
    if user.get("barangay_id"):
        tags.append(f"barangay:{user['barangay_id']}")
    for barangay_id in user.get("barangay_ids") or []:
        tag = f"barangay:{barangay_id}"
        if tag not in tags:
            tags.append(tag)
    return tags


def create(audience: str, kind: str, message: str, title: str = "",
           actor: str | None = None, **extra) -> dict:
    """
    Raise a notification. `audience` is one of the tags above.

    Callers that fire on a repeating condition should pass `dedupe_key` and use
    `already_sent()` first -- the truck-approaching alert would otherwise fire
    on every GPS ping within 500 m of an MRF.
    """
    return storage.insert("notifications", {
        "audience": audience,
        "type": kind,
        "title": title or kind.replace("_", " ").title(),
        "message": message,
        "tone": TONES.get(kind, "info"),
        "icon": ICONS.get(kind, "bell"),
        "date": timeutil.today_str(),
        "read_by": [],
        **extra,
    }, actor)


def already_sent(dedupe_key: str, date=None) -> bool:
    """Has this exact alert already gone out today? Stops repeat spam."""
    day = timeutil.date_str(date or timeutil.today())
    return storage.exists("notifications", dedupe_key=dedupe_key, date=day)


def for_user(user: dict | None, limit: int = 20,
             unread_only: bool = False) -> list[dict]:
    tags = set(audiences_for(user))
    viewer = (user or {}).get("id")

    rows = []
    for row in storage.read("notifications"):
        if row.get("audience") not in tags:
            continue
        is_read = viewer in (row.get("read_by") or [])
        if unread_only and is_read:
            continue
        stamp = timeutil.parse_stamp(row.get("created_at"))
        rows.append({
            **row,
            "unread": not is_read,
            "time_display": timeutil.display_time(stamp) if stamp else "",
            "date_display": timeutil.display_date(row.get("date")),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def unread_count(user: dict | None) -> int:
    if not user:
        return 0
    return len(for_user(user, limit=999, unread_only=True))


def mark_read(notification_id: str, user_id: str) -> dict | None:
    """Per-person read state: one admin reading it must not hide it from another."""
    with storage.transaction("notifications") as rows:
        for row in rows:
            if row.get("id") != notification_id:
                continue
            readers = row.setdefault("read_by", [])
            if user_id not in readers:
                readers.append(user_id)
            return dict(row)
    return None


def mark_all_read(user: dict) -> int:
    tags = set(audiences_for(user))
    changed = 0
    with storage.transaction("notifications") as rows:
        for row in rows:
            if row.get("audience") not in tags:
                continue
            readers = row.setdefault("read_by", [])
            if user["id"] not in readers:
                readers.append(user["id"])
                changed += 1
    return changed
