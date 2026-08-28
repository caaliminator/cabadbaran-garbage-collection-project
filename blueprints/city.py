"""
City Hall Admin portal -- city-wide oversight of the collection network.

Every page reads the JSON store. Counters are computed from the records on
each request rather than stored, so a figure on this portal can never drift
from what actually happened -- and the socket layer only tells the page to
re-read, never what the number is.
"""

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

from blueprints.auth import current_user, role_required
from services import (assignment_service, carryover_service, collection_service,
                      duty_service, history_service, mrf_service,
                      property_service, public_report_service, report_service,
                      schedule_service, storage, timeutil, user_service,
                      vehicle_service)
from services.validation import ValidationError

city_bp = Blueprint("city", __name__)

# Sidebar model: grouped exactly like the design reference.
NAV = [
    {"group": None, "items": [
        ("city.dashboard", "Dashboard", "grid"),
    ]},
    {"group": "Management", "items": [
        ("city.users", "User Management", "users"),
        ("city.schedule", "Waste Schedule", "calendar"),
        ("city.tricycle", "Tricycle", "tricycle"),
        ("city.truck", "Truck", "truck"),
    ]},
    {"group": "Monitoring", "items": [
        ("city.tracking", "Live Tracking", "radar"),
        ("city.property", "Property", "home"),
        ("city.mrf", "MRF", "recycle"),
        ("city.carry_over", "Carry-Over", "repeat"),
    ]},
    {"group": "Reports", "items": [
        ("city.reports", "History & Reports", "file"),
    ]},
    {"group": "Settings", "items": [
        ("city.profile", "Profile", "user"),
        ("auth.logout", "Log Out", "logout"),
    ]},
]


@city_bp.context_processor
def inject_shell():
    return {
        "nav": NAV,
        "user": current_user(),
        "portal": "city",
        "home_endpoint": "city.dashboard",
        "profile_endpoint": "city.profile",
        "tracking_endpoint": "city.tracking",
        "portal_label": "City Hall Administration",
    }


def _filters(*keys) -> dict:
    """Non-empty query-string filters, for round-tripping across a redirect."""
    out = {}
    for key in keys:
        value = request.args.get(key, "").strip()
        if value:
            out[key] = value
    return out


@city_bp.route("/")
@role_required("city_admin")
def dashboard():
    date = timeutil.today_str()
    properties = property_service.listing()
    rows = collection_service.route_with_status(properties, date)
    entries = collection_service.entries_for_date(date)

    property_counts = collection_service.counts(properties, date)
    mrf_counts = mrf_service.city_counts(date)
    active = duty_service.active_counts()

    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    users = {u["id"]: u.get("full_name") for u in storage.read("users")}

    # Recent activity: household entries and MRF pickups in one feed, newest
    # first -- an admin watching the day wants both, in the order they happened.
    activity = []
    for row in rows:
        if not row["entry"]:
            continue
        stamp = timeutil.parse_stamp(row["entry"].get("timestamp"))
        activity.append({
            "timestamp": row["entry"].get("timestamp"),
            "time": timeutil.display_time(stamp) if stamp else "—",
            "barangay": names.get(row.get("barangay_id"), "—"),
            "collector": users.get(row["entry"].get("collector_id")) or "Resident report",
            "vehicle": row["entry"].get("tricycle_code") or "—",
            "type": "Household",
            "status": row["status"],
            "reason": row["reason"] or "—",
            "owner_name": row["owner_name"],
            "purok": row["purok"],
            "note": row["note"],
            "load": row["load"],
            "proof": row["entry"].get("image_proof_path"),
            "location": row["entry"].get("location") or "",
            "gps": row["entry"].get("gps"),
        })
    for pickup in storage.find("mrf_pickups", date=date):
        stamp = timeutil.parse_stamp(pickup.get("timestamp"))
        activity.append({
            "timestamp": pickup.get("timestamp"),
            "time": timeutil.display_time(stamp) if stamp else "—",
            "barangay": names.get(pickup.get("barangay_id"), "—"),
            "collector": users.get(pickup.get("operator_id")) or "Automatic",
            "vehicle": pickup.get("truck_code") or "—",
            "type": "MRF Pickup",
            "status": pickup.get("status"),
            "reason": pickup.get("reason") or "—",
            "owner_name": f"{names.get(pickup.get('barangay_id'), '')} MRF",
            "purok": "—",
            "note": pickup.get("note") or "",
            "load": [],
            "proof": None,
            # An MRF pickup's place is the facility itself, and the MRF page
            # is where that detail lives; the feed just names it.
            "location": f"{names.get(pickup.get('barangay_id'), '')} MRF",
            "gps": pickup.get("gps"),
        })
    activity.sort(key=lambda a: a["timestamp"] or "", reverse=True)

    return render_template(
        "city-hall-admin/dashboard.html",
        page_title="Dashboard",
        property_counts=property_counts,
        mrf_counts=mrf_counts,
        active=active,
        activity=activity[:20],
        totals=collection_service.totals(entries),
        schedule=schedule_service.upcoming(),
        today_row=schedule_service.for_date(),
        disputes=public_report_service.dispute_count(),
        map_barangays=user_service.barangay_options(),
        statuses=["Collected", "Pending", "Not Collected", "Collected from MRF"],
        activity_types=["Household", "MRF Pickup"],
    )


@city_bp.route("/users", methods=["GET", "POST"])
@role_required("city_admin")
def users():
    actor = current_user()["id"]

    if request.method == "POST":
        action = request.form.get("action", "create")
        target = request.form.get("id", "")
        try:
            if action == "create":
                created = user_service.create(request.form, actor)
                flash(f"Account created for {created['full_name']}.", "success")
            elif action == "edit":
                updated = user_service.edit(target, request.form, actor)
                flash(f"{updated['full_name']}'s account was updated.", "success")
            elif action == "reset_password":
                user_service.reset_password(target, request.form, actor)
                flash("Password reset. Share it with the user securely -- they "
                      "will be asked to change it.", "success")
            elif action == "toggle":
                user = storage.get("users", target) or {}
                new_status = "Inactive" if user.get("status") == "Active" else "Active"
                row = user_service.set_status(target, new_status, actor)
                flash(f"{row['full_name']} is now {row['status'].lower()}.", "success")
            elif action == "delete":
                removed = user_service.delete(target, actor)
                flash(f"Deleted the account for {removed['full_name']}.", "success")
            else:
                flash("Unknown action.", "danger")
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        return redirect(url_for("city.users", **_filters("search", "role",
                                                         "barangay", "status")))

    return render_template(
        "city-hall-admin/users.html",
        page_title="User Management",
        users=user_service.listing(
            search=request.args.get("search", ""),
            role=request.args.get("role", ""),
            barangay=request.args.get("barangay", ""),
            status=request.args.get("status", "")),
        counts=user_service.counts(),
        roles=user_service.role_options(),
        barangays=user_service.barangay_options(),
        tricycles=vehicle_service.available(vehicle_service.TRICYCLE),
        trucks=vehicle_service.available(vehicle_service.TRUCK),
        filters=_filters("search", "role", "barangay", "status"),
    )


@city_bp.route("/vehicles", methods=["POST"])
@role_required("city_admin")
def vehicles():
    """
    The vehicle registry, posted from a section on the Tricycle and Truck
    pages. The assign dropdowns need registered units to come from somewhere.
    """
    actor = current_user()["id"]
    action = request.form.get("action", "register")
    back = request.form.get("back") or url_for("city.tricycle")

    try:
        if action == "register":
            created = vehicle_service.register(request.form.get("code", ""),
                                               request.form.get("type", ""),
                                               request.form.get("note", ""), actor)
            flash(f"{created['code']} added to the registry.", "success")
        elif action == "deactivate":
            row = vehicle_service.set_active(request.form.get("id", ""), False, actor)
            flash(f"{row['code']} withdrawn from service.", "success")
        elif action == "reactivate":
            row = vehicle_service.set_active(request.form.get("id", ""), True, actor)
            flash(f"{row['code']} returned to service.", "success")
        elif action == "delete":
            row = vehicle_service.delete(request.form.get("id", ""), actor)
            flash(f"{row['code']} removed from the registry.", "success")
    except ValidationError as exc:
        for message in exc.errors.values():
            flash(message, "danger")

    return redirect(back)


@city_bp.route("/schedule", methods=["GET", "POST"])
@role_required("city_admin")
def schedule():
    if request.method == "POST":
        try:
            schedule_service.save_week(request.form, current_user()["id"])
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        else:
            # Phase 7 broadcasts schedule_updated here so every open client
            # refreshes without a reload.
            flash("Waste schedule updated. Every barangay, collector, and the "
                  "public viewer now sees the new schedule.", "success")
            return redirect(url_for("city.schedule"))

    return render_template(
        "city-hall-admin/schedule.html",
        page_title="Waste Schedule",
        schedule=schedule_service.week(),
        tones=schedule_service.TONES,
        common_types=schedule_service.COMMON_TYPES,
        editing=request.args.get("edit") == "1" or request.method == "POST",
        editable=True,
    )


@city_bp.route("/tricycle", methods=["GET", "POST"])
@role_required("city_admin")
def tricycle():
    actor = current_user()["id"]

    if request.method == "POST":
        assignment_id = request.form.get("id") or None
        try:
            if request.form.get("action") == "end":
                assignment_service.end_assignment(
                    assignment_service.TRICYCLE_COLLECTION, assignment_id, actor)
                flash("Assignment ended. The collector and unit are free again.",
                      "success")
            else:
                _, warnings = assignment_service.save_tricycle_assignment(
                    request.form, assignment_id, actor)
                flash("Tricycle assignment updated." if assignment_id
                      else "Tricycle assignment saved.", "success")
                for note in warnings:
                    flash(note, "warning")
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        return redirect(url_for("city.tricycle"))

    counts = assignment_service.tricycle_counts()
    return render_template(
        "city-hall-admin/tricycle.html",
        page_title="Tricycle",
        rows=assignment_service.tricycle_listing(
            search=request.args.get("search", ""),
            barangay=request.args.get("barangay", ""),
            status=request.args.get("status", ""),
            availability=request.args.get("availability", "")),
        stats=[
            ("Total Tricycle Collectors", counts["total_collectors"],
             "Registered accounts", "tricycle"),
            ("Active Assignments", counts["active_assignments"],
             "Currently assigned", "check"),
            ("Unavailable Requests", counts["unavailable_requests"],
             "Need reassignment", "alert"),
            ("Barangays Covered",
             f"{counts['barangays_covered']}/{counts['total_barangays']}",
             "With tricycle assignments", "map"),
        ],
        collectors=user_service.collectors("tricycle_collector"),
        barangays=user_service.barangay_options(),
        tricycles=vehicle_service.available(vehicle_service.TRICYCLE),
        registry=vehicle_service.with_status(vehicle_service.TRICYCLE),
        statuses=assignment_service.STATUS_CHOICES,
    )


@city_bp.route("/truck", methods=["GET", "POST"])
@role_required("city_admin")
def truck():
    actor = current_user()["id"]

    if request.method == "POST":
        assignment_id = request.form.get("id") or None
        try:
            if request.form.get("action") == "end":
                assignment_service.end_assignment(
                    assignment_service.TRUCK_COLLECTION, assignment_id, actor)
                flash("Assignment ended. The operator and truck are free again.",
                      "success")
            else:
                _, warnings = assignment_service.save_truck_assignment(
                    request.form, assignment_id, actor)
                flash("Truck assignment updated." if assignment_id
                      else "Truck assignment saved.", "success")
                for note in warnings:
                    flash(note, "warning")
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        return redirect(url_for("city.truck"))

    counts = assignment_service.truck_counts()
    return render_template(
        "city-hall-admin/truck.html",
        page_title="Truck",
        rows=assignment_service.truck_listing(
            search=request.args.get("search", ""),
            status=request.args.get("status", ""),
            availability=request.args.get("availability", "")),
        stats=[
            ("Total Trucks", f"{counts['total_trucks']}/{counts['max_trucks']}",
             "Registered units", "truck"),
            ("Active Assignments", counts["active_assignments"],
             "Currently assigned", "check"),
            ("Unavailable Requests", counts["unavailable_requests"],
             "Need reassignment", "alert"),
            ("MRFs Covered",
             f"{counts['mrfs_covered']}/{counts['total_barangays']}",
             "Fully covered" if counts["fully_covered"]
             else f"{len(counts['uncovered'])} still unassigned", "recycle"),
        ],
        counts=counts,
        uncovered=assignment_service.uncovered_barangays(),
        operators=user_service.collectors("truck_collector"),
        barangays=user_service.barangay_options(),
        trucks=vehicle_service.available(vehicle_service.TRUCK),
        registry=vehicle_service.with_status(vehicle_service.TRUCK),
        statuses=assignment_service.STATUS_CHOICES,
    )


@city_bp.route("/tracking")
@role_required("city_admin")
def tracking():
    return render_template(
        "city-hall-admin/tracking.html",
        page_title="Live Tracking",
        map_barangays=user_service.barangay_options(),
        counts=duty_service.active_counts(),
        collectors=duty_service.active_collectors(),
    )


@city_bp.route("/property")
@role_required("city_admin")
def property():
    """Household Monitoring: every property in the city, for any date."""
    date = request.args.get("date") or timeutil.today_str()
    barangay_id = request.args.get("barangay") or None

    properties = property_service.listing(
        barangay_id=barangay_id,
        property_type=request.args.get("type") or None,
        search=request.args.get("search", ""))

    purok = request.args.get("purok") or None
    if purok:
        properties = [p for p in properties if p.get("purok") == purok]

    rows = collection_service.route_with_status(properties, date)

    status = request.args.get("status") or None
    if status:
        rows = [r for r in rows if r["status"] == status]

    collector = request.args.get("collector") or None
    if collector:
        rows = [r for r in rows if (r["entry"] or {}).get("collector_id") == collector]

    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    users = {u["id"]: u.get("full_name") for u in storage.read("users")}
    coverage = _tricycle_coverage()

    for row in rows:
        row["barangay"] = names.get(row.get("barangay_id"), "—")
        row["collector"] = users.get((row["entry"] or {}).get("collector_id")) or "—"
        row["assigned_tricycle"] = coverage.get(row.get("barangay_id"), "Unassigned")

    counts = collection_service.counts(property_service.listing(), date)

    return render_template(
        "city-hall-admin/property.html",
        page_title="Property",
        rows=rows,
        counts=counts,
        stats=[
            ("Pending Property", counts["pending"],
             "No status recorded yet", "clock"),
            ("Collected", counts["collected"],
             f"{counts['percent']}% of {counts['total']} registered", "check"),
            ("Not Collected", counts["not_collected"],
             "With a recorded reason", "alert"),
        ],
        barangays=user_service.barangay_options(),
        puroks=sorted({p.get("purok") for p in property_service.listing()
                       if p.get("purok")}),
        types=property_service.PROPERTY_TYPES,
        collectors=[(u["id"], u.get("full_name"))
                    for u in storage.read("users")
                    if u.get("role") == "tricycle_collector"],
        statuses=["Collected", "Pending", "Not Collected"],
        selected_date=date,
        today=timeutil.today_str(),
    )


def _tricycle_coverage() -> dict:
    """barangay -> the tricycle covering it, from active assignments."""
    coverage = {}
    for row in storage.read(assignment_service.TRICYCLE_COLLECTION):
        if row.get("status") not in assignment_service.ACTIVE_STATUSES:
            continue
        if row.get("barangay_id"):
            coverage[row["barangay_id"]] = row.get("tricycle_code")
    return coverage


@city_bp.route("/mrf")
@role_required("city_admin")
def mrf():
    date = request.args.get("date") or timeutil.today_str()
    counts = mrf_service.city_counts(date)

    return render_template(
        "city-hall-admin/mrf.html",
        page_title="MRF",
        rows=mrf_service.city_listing(
            date=date,
            barangay_id=request.args.get("barangay") or None,
            truck=request.args.get("truck") or None,
            status=request.args.get("status") or None),
        deliveries=mrf_service.deliveries_listing(date),
        load=mrf_service.city_totals(date),
        counts=counts,
        stats=[
            ("Pending MRFs", counts["pending"], f"of {counts['total']} barangay MRFs", "recycle"),
            ("Not Collected", counts["missed"], "Missed pickups today", "alert"),
            ("Collected from MRF", counts["collected"],
             f"{counts['percent']}% of the city", "check"),
            ("Delivered to Landfill", counts["delivered"], "Trips completed today", "truck"),
        ],
        barangays=user_service.barangay_options(),
        trucks=vehicle_service.in_service(vehicle_service.TRUCK),
        statuses=[mrf_service.PENDING, mrf_service.COLLECTED, mrf_service.NOT_COLLECTED],
        selected_date=date,
    )


@city_bp.route("/carry-over", methods=["GET", "POST"])
@role_required("city_admin")
def carry_over():
    actor = current_user()["id"]

    if request.method == "POST":
        action = request.form.get("action")
        target = request.form.get("id", "")
        try:
            if action == "reassign":
                row = carryover_service.reassign(
                    target, request.form.get("truck", ""), actor)
                flash(f"{_barangay_name(row['barangay_id'])} carry-over reassigned "
                      f"to {row['current_truck']}.", "success")
            elif action == "reschedule":
                row = carryover_service.reschedule(
                    target, request.form.get("reschedule_date", ""), actor)
                flash(f"{_barangay_name(row['barangay_id'])} carry-over rescheduled "
                      f"to {timeutil.display_date(row['reschedule_date'])}.", "success")
            else:
                flash("Unknown action.", "danger")
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        return redirect(url_for("city.carry_over"))

    counts = carryover_service.counts()
    # Blank means the open worklist. Only "Collected" is offered as the other
    # view, so anything else is treated as no filter rather than 404-ing.
    selected_status = ("Collected" if request.args.get("status") == "Collected"
                       else "")
    return render_template(
        "city-hall-admin/carryover.html",
        page_title="Carry-Over",
        rows=carryover_service.listing(
            status=selected_status,
            barangay_id=request.args.get("barangay") or ""),
        counts=counts,
        selected_status=selected_status,
        stats=[
            ("Pending Carry-Overs", counts["pending"], "Awaiting collection", "repeat"),
            ("Not Yet Reassigned", counts["unassigned"], "No truck assigned", "alert"),
            ("Overdue", counts["overdue"], "Past their rescheduled date", "clock"),
            ("Closed", counts["collected"], "Collected on a later run", "check"),
        ],
        trucks=vehicle_service.in_service(vehicle_service.TRUCK),
        barangays=user_service.barangay_options(),
        today=timeutil.today_str(),
    )


def _barangay_name(barangay_id):
    row = storage.find_one("barangays", id=barangay_id)
    return row["name"] if row else barangay_id


@city_bp.route("/reports", methods=["GET", "POST"])
@role_required("city_admin")
def reports():
    """
    History feed plus the report generator.

    A generated report is rendered as a printable page and offered as CSV of
    the same rows -- both from one build, so the printout and the spreadsheet
    cannot disagree.
    """
    generated = errors = None

    if request.method == "POST":
        try:
            generated = report_service.build(
                request.form.get("report_type", ""),
                request.form.get("start_date", ""),
                request.form.get("end_date", ""),
                request.form.get("barangay") or None)
        except ValidationError as exc:
            errors = exc.errors
            for message in exc.errors.values():
                flash(message, "danger")

    return render_template(
        "city-hall-admin/reports.html",
        page_title="History & Reports",
        feed=history_service.feed(None, limit=14),
        report_types=report_service.TYPES,
        barangays=user_service.barangay_options(),
        generated=generated,
        errors=errors or {},
        today=timeutil.today_str(),
        form=request.form,
    )


@city_bp.route("/reports/print", methods=["POST"])
@role_required("city_admin")
def report_print():
    """A clean, printable page -- no navigation, no controls."""
    try:
        report = report_service.build(
            request.form.get("report_type", ""),
            request.form.get("start_date", ""),
            request.form.get("end_date", ""),
            request.form.get("barangay") or None)
    except ValidationError as exc:
        for message in exc.errors.values():
            flash(message, "danger")
        return redirect(url_for("city.reports"))

    return render_template("city-hall-admin/report_print.html", report=report)


@city_bp.route("/reports/csv", methods=["POST"])
@role_required("city_admin")
def report_csv():
    try:
        report = report_service.build(
            request.form.get("report_type", ""),
            request.form.get("start_date", ""),
            request.form.get("end_date", ""),
            request.form.get("barangay") or None)
    except ValidationError as exc:
        for message in exc.errors.values():
            flash(message, "danger")
        return redirect(url_for("city.reports"))

    # utf-8-sig: Excel opens a plain UTF-8 CSV as the system codepage and
    # mangles the barangay names that carry accents.
    return Response(
        report_service.to_csv(report).encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{report_service.csv_filename(report)}"'},
    )


@city_bp.route("/profile")
@role_required("city_admin")
def profile():
    # Change Password posts to auth.change_password (shared by all roles).
    return render_template("city-hall-admin/profile.html", page_title="Profile")
