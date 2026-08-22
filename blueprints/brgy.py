"""
Barangay Admin portal -- scoped to a single assigned barangay.

Every query here is filtered by the admin's own barangay, and that barangay is
read from the session, never from the request. `_scope()` is the single place
it comes from; if a route needs a barangay, it takes it from there.
"""

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

from blueprints.auth import current_user, role_required
from services import (assignment_service, collection_service, history_service,
                      mrf_service, notification_service, property_service,
                      public_report_service, schedule_service, storage,
                      timeutil)
from services.validation import ValidationError

brgy_bp = Blueprint("brgy", __name__)

NAV = [
    {"group": None, "items": [
        ("brgy.dashboard", "Dashboard", "grid"),
    ]},
    {"group": "Management", "items": [
        ("brgy.properties", "Property List", "home"),
    ]},
    {"group": "Monitoring", "items": [
        ("brgy.schedule", "Waste Schedule", "calendar"),
        ("brgy.tracking", "Live Tracking", "radar"),
        ("brgy.collections", "Collections", "recycle"),
    ]},
    # History sits under Reports, mirroring the City Hall portal, rather than
    # under Settings as the draft had it (OPEN ITEM 6).
    {"group": "Reports", "items": [
        ("brgy.reports", "Resident Reports", "home"),
        ("brgy.history", "History", "file"),
    ]},
    {"group": "Settings", "items": [
        ("brgy.profile", "Profile", "user"),
        ("auth.logout", "Log Out", "logout"),
    ]},
]


def _scope() -> str:
    """
    The signed-in admin's barangay id. The single source of scope for every
    query on this portal.

    An admin with no barangay assigned cannot be scoped to anything, so they
    are stopped here rather than silently shown the whole city.
    """
    user = current_user() or {}
    barangay_id = user.get("barangay_id")
    if not barangay_id:
        abort(403, description="Your account has no barangay assigned. "
                               "Ask the City Hall Admin to set one.")
    return barangay_id


def assigned_barangay() -> str | None:
    user = current_user() or {}
    return user.get("barangay")


@brgy_bp.context_processor
def inject_shell():
    user = current_user() or {}
    return {
        "nav": NAV,
        "user": user,
        "portal": "brgy",
        "barangay": user.get("barangay"),
        "barangay_id": user.get("barangay_id"),
        "home_endpoint": "brgy.dashboard",
        "profile_endpoint": "brgy.profile",
        "tracking_endpoint": "brgy.tracking",
        "portal_label": "Garbage Collection Tracking System",
    }


def _requested_date() -> str:
    """
    The date being viewed. Defaults to today; the Collections and Property
    pages let the admin look back at earlier days.
    """
    raw = request.args.get("date")
    parsed = timeutil.to_date(raw) if raw else None
    return timeutil.date_str(parsed) if parsed else timeutil.today_str()


@brgy_bp.route("/")
@role_required("barangay_admin")
def dashboard():
    barangay_id = _scope()
    user = current_user()
    date = timeutil.today_str()

    properties = property_service.listing(barangay_id=barangay_id)
    rows = collection_service.route_with_status(properties, date)
    recent = sorted([r for r in rows if r["entry"]],
                    key=lambda r: r["entry"].get("timestamp") or "", reverse=True)

    collectors = {u["id"]: u.get("full_name") for u in storage.read("users")}

    return render_template(
        "brgy-admin/dashboard.html",
        page_title="Dashboard",
        counts=collection_service.counts(properties, date),
        totals=collection_service.totals(
            collection_service.entries_for_date(date, barangay_id=barangay_id)),
        recent=recent[:8],
        collectors=collectors,
        alerts=notification_service.for_user(user, limit=6),
        mrf=mrf_service.mrf_card(barangay_id, date),
        today_row=schedule_service.for_date(date),
        map_barangays=[(barangay_id, user.get("barangay"))],
    )


@brgy_bp.route("/properties", methods=["GET", "POST"])
@role_required("barangay_admin")
def properties():
    barangay_id = _scope()
    actor = current_user()["id"]

    if request.method == "POST":
        action = request.form.get("action", "create")
        target = request.form.get("id", "")
        try:
            if action == "create":
                created = property_service.create(request.form, barangay_id, actor)
                flash(f"Added {created['owner_name']} to the property list.", "success")
            elif action == "update":
                # The barangay is passed from the session, so a tampered hidden
                # field cannot move a property into another barangay.
                updated = property_service.update(target, request.form,
                                                  barangay_id, actor)
                flash(f"Updated {updated['owner_name']}.", "success")
            elif action == "delete":
                removed = property_service.delete(target, barangay_id, actor)
                flash(f"Removed {removed['owner_name']} from the property list.",
                      "success")
            else:
                flash("Unknown action.", "danger")
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        return redirect(url_for("brgy.properties"))

    rows = property_service.listing(
        barangay_id=barangay_id,
        property_type=request.args.get("type") or None,
        search=request.args.get("search", ""))

    return render_template(
        "brgy-admin/properties.html",
        page_title="Property List",
        rows=collection_service.route_with_status(rows),
        total=property_service.count_for(barangay_id),
        puroks=property_service.puroks_for(barangay_id),
        types=property_service.PROPERTY_TYPES,
        tags=property_service.COMMON_TAGS,
        search=request.args.get("search", ""),
    )


@brgy_bp.route("/schedule")
@role_required("barangay_admin")
def schedule():
    return render_template(
        "brgy-admin/schedule.html",
        page_title="Waste Schedule",
        schedule=schedule_service.week(),
        editable=False,
    )


@brgy_bp.route("/tracking")
@role_required("barangay_admin")
def tracking():
    barangay_id = _scope()
    user = current_user()
    return render_template(
        "brgy-admin/tracking.html",
        page_title="Live Tracking",
        alerts=notification_service.for_user(user, limit=10),
        mrf=mrf_service.mrf_card(barangay_id),
        map_barangays=[(barangay_id, user.get("barangay"))],
    )


@brgy_bp.route("/collections")
@role_required("barangay_admin")
def collections():
    barangay_id = _scope()
    date = _requested_date()

    properties = property_service.listing(barangay_id=barangay_id)
    entries = collection_service.entries_for_date(date, barangay_id=barangay_id)

    # Per-collector totals, for the collectors table and its View modal.
    users = {u["id"]: u for u in storage.read("users")}
    by_collector: dict[str, list] = {}
    for entry in entries:
        by_collector.setdefault(entry.get("collector_id"), []).append(entry)

    collector_rows = []
    for row in assignment_service.tricycle_listing(barangay=barangay_id):
        own = by_collector.get(row["collector_id"], [])
        collector_rows.append({
            **row,
            "entries": len(own),
            "collected": sum(1 for e in own
                             if e["status"] == collection_service.COLLECTED),
            "not_collected": sum(1 for e in own
                                 if e["status"] == collection_service.NOT_COLLECTED),
            "totals": collection_service.totals(own),
        })

    # A collector who recorded here without an active assignment (a
    # replacement whose assignment has since ended) would otherwise vanish
    # from the table along with their load.
    listed = {r["collector_id"] for r in collector_rows}
    for collector_id, own in by_collector.items():
        if collector_id in listed or not collector_id:
            continue
        user = users.get(collector_id) or {}
        collector_rows.append({
            "collector_id": collector_id,
            "collector": user.get("full_name") or "Deleted account",
            "username": user.get("username") or "—",
            "tricycle": (own[0] or {}).get("tricycle_code") or "—",
            "puroks": [], "availability": "—", "status": "No active assignment",
            "entries": len(own),
            "collected": sum(1 for e in own
                             if e["status"] == collection_service.COLLECTED),
            "not_collected": sum(1 for e in own
                                 if e["status"] == collection_service.NOT_COLLECTED),
            "totals": collection_service.totals(own),
        })

    return render_template(
        "brgy-admin/collections.html",
        page_title="Collections",
        counts=collection_service.counts(properties, date),
        totals=collection_service.totals(entries),
        collectors=collector_rows,
        rows=collection_service.route_with_status(properties, date),
        selected_date=date,
        is_today=date == timeutil.today_str(),
        today=timeutil.today_str(),
    )


@brgy_bp.route("/history")
@role_required("barangay_admin")
def history():
    barangay_id = _scope()
    return render_template(
        "brgy-admin/history.html",
        page_title="History",
        feed=history_service.feed(barangay_id, limit=14),
    )


@brgy_bp.route("/profile")
@role_required("barangay_admin")
def profile():
    # Change Password posts to auth.change_password (shared by all roles).
    return render_template("brgy-admin/profile.html", page_title="Profile")


@brgy_bp.route("/reports", methods=["GET", "POST"])
@role_required("barangay_admin")
def reports():
    """
    Resident reports, and the disputes they raise.

    A dispute is where an anonymous report and a collector's record disagree.
    Neither was overwritten, so the admin decides which stands -- with the
    collector's photo proof to hand.
    """
    barangay_id = _scope()
    actor = current_user()["id"]

    if request.method == "POST":
        try:
            public_report_service.resolve_dispute(
                request.form.get("id", ""), request.form.get("keep", ""), actor)
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        else:
            flash("Dispute resolved.", "success")
        return redirect(url_for("brgy.reports"))

    properties = {p["id"]: p for p in property_service.listing(barangay_id=barangay_id)}
    disputes = []
    for entry in storage.find("collections", barangay_id=barangay_id):
        if not entry.get("disputed"):
            continue
        prop = properties.get(entry.get("property_id")) or {}
        disputes.append({
            **entry,
            "owner_name": prop.get("owner_name") or "Deleted property",
            "date_display": timeutil.display_date(entry.get("date")),
        })
    disputes.sort(key=lambda r: r.get("date") or "", reverse=True)

    return render_template(
        "brgy-admin/reports.html",
        page_title="Resident Reports",
        reports=public_report_service.listing(barangay_id=barangay_id),
        disputes=disputes,
    )


@brgy_bp.route("/notifications/read", methods=["POST"])
@role_required("barangay_admin")
def read_notifications():
    user = current_user()
    target = request.form.get("id")
    if target:
        notification_service.mark_read(target, user["id"])
    else:
        notification_service.mark_all_read(user)
    return redirect(request.form.get("back") or url_for("brgy.dashboard"))
