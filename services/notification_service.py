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
# The barangay's own MRF, as its tricycle collectors and admin need to hear
# about it: the truck came and took the load, or a missed load now has a
# truck and a day.
MRF_COLLECTED = "mrf_collected"
CARRY_OVER_SCHEDULED = "carry_over_scheduled"

TONES = {
    TRUCK_APPROACHING: "info",
    ARRIVAL_REMINDER: "info",
    UNAVAILABLE_REQUEST: "warning",
    ASSIGNMENT_CHANGED: "info",
    CARRY_OVER_CREATED: "danger",
    PUBLIC_REPORT: "warning",
    DELIVERY_COMPLETED: "success",
    SCHEDULE_UPDATED: "info",
    MRF_COLLECTED: "success",
    CARRY_OVER_SCHEDULED: "info",
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
    MRF_COLLECTED: "recycle",
    CARRY_OVER_SCHEDULED: "calendar",
}


# ---------------------------------------------------------------------------
# Where a notification takes you
#
# Tapping an alert should land on the page that answers it: "carry-over
# created" on the worklist where an admin reassigns it, "resident report" on
# the page that shows the report. The destination therefore depends on who is
# looking -- the same carry-over alert is a job for City Hall and an
# explanation for the barangay whose MRF was missed.
#
# Resolved at read time from the notification's type rather than stored on the
# row. An alert raised before this existed still links, and a renamed route is
# one edit here instead of a data migration.
#
# A (type, role) pair with no entry is informational and renders as plain text
# rather than as a link to somewhere unhelpful -- a collector has no schedule
# page, so "schedule updated" stays text for them.
# ---------------------------------------------------------------------------

LINKS: dict[str, dict[str, str]] = {
    TRUCK_APPROACHING:   {"city_admin": "city.tracking",
                          "barangay_admin": "brgy.tracking",
                          "truck_collector": "collector.truck_route"},
    ARRIVAL_REMINDER:    {"city_admin": "city.mrf",
                          "barangay_admin": "brgy.tracking",
                          "truck_collector": "collector.truck_route",
                          "tricycle_collector": "collector.tricycle_route"},
    UNAVAILABLE_REQUEST: {"city_admin": "city.unavailability",
                          "tricycle_collector": "collector.tricycle_unavailable",
                          "truck_collector": "collector.truck_unavailable"},
    ASSIGNMENT_CHANGED:  {"tricycle_collector": "collector.tricycle_route",
                          "truck_collector": "collector.truck_route"},
    # The tricycle route page carries a "Your MRF today" card, which is what
    # answers every alert about the barangay's MRF; the barangay dashboard has
    # the same card for the admin.
    CARRY_OVER_CREATED:  {"city_admin": "city.carry_over",
                          "barangay_admin": "brgy.collections",
                          "tricycle_collector": "collector.tricycle_route"},
    MRF_COLLECTED:       {"barangay_admin": "brgy.dashboard",
                          "tricycle_collector": "collector.tricycle_route"},
    CARRY_OVER_SCHEDULED: {"barangay_admin": "brgy.dashboard",
                           "tricycle_collector": "collector.tricycle_route"},
    PUBLIC_REPORT:       {"city_admin": "city.resident_reports",
                          "barangay_admin": "brgy.reports",
                          # The report names a household on this round, so the
                          # collector's own property list is what answers it.
                          "tricycle_collector": "collector.tricycle_route"},
    DELIVERY_COMPLETED:  {"city_admin": "city.mrf",
                          "truck_collector": "collector.truck_route"},
    SCHEDULE_UPDATED:    {"city_admin": "city.schedule",
                          "barangay_admin": "brgy.schedule",
                          # Both route pages head with today's waste type,
                          # which is exactly what a schedule change alters.
                          "tricycle_collector": "collector.tricycle_route",
                          "truck_collector": "collector.truck_route"},
}

# Pages that show one day at a time. Carrying the notification's own date means
# an alert read the next morning opens the day it happened rather than today's
# empty page.
DATED_ENDPOINTS = frozenset({"city.mrf", "brgy.collections"})


def link_for(row: dict, role: str | None) -> tuple[str, dict] | None:
    """
    Where tapping this notification should go, as (endpoint, url_for kwargs),
    or None when there is nowhere useful to send this viewer.

    An endpoint name rather than a URL, so this module stays free of Flask --
    the template and the open route call `url_for` themselves.
    """
    if not role:
        return None

    endpoint = (LINKS.get(row.get("type")) or {}).get(role)
    if not endpoint:
        return None

    kwargs = {}
    if endpoint in DATED_ENDPOINTS and row.get("date"):
        kwargs["date"] = row["date"]
    return endpoint, kwargs


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
    role = (user or {}).get("role")

    rows = []
    for row in storage.read("notifications"):
        if row.get("audience") not in tags:
            continue
        is_read = viewer in (row.get("read_by") or [])
        if unread_only and is_read:
            continue
        stamp = timeutil.parse_stamp(row.get("created_at"))
        link = link_for(row, role)
        rows.append({
            **row,
            "unread": not is_read,
            # Rendered as a link only when both are set. Templates read these
            # rather than a URL because building one needs Flask.
            "link_endpoint": link[0] if link else None,
            "link_args": link[1] if link else {},
            "time_display": timeutil.display_time(stamp) if stamp else "",
            "date_display": timeutil.display_date(row.get("date")),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def unread_count(user: dict | None) -> int:
    if not user:
        return 0
    return len(for_user(user, limit=999, unread_only=True))


def open_for(notification_id: str, user: dict) -> tuple[str, dict] | None:
    """
    Mark one notification read for this viewer and say where it leads.

    The audience check is the access control. A viewer can only touch an alert
    that was addressed to them, so an id lifted from someone else's bell marks
    nothing read and goes nowhere.
    """
    row = storage.get("notifications", notification_id)
    if not row or row.get("audience") not in set(audiences_for(user)):
        return None
    mark_read(notification_id, user["id"])
    return link_for(row, user.get("role"))


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
