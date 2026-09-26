"""
Field collector portals -- tricycle (household route) and truck (MRF route).

Both roles share one blueprint because their screens are structurally
identical: a work list, a per-stop record form, an unavailability request, a
history feed, and a profile. Only the unit of work differs -- a household for
the tricycle, a barangay MRF for the truck.

Designed mobile-first (these are used one-handed, outdoors, on a phone) but the
templates lay out into multiple columns from 900px up, so the same pages work
at a desk.

Both sides now run on the JSON store. The truck operator records no
quantities: an MRF card's load is computed from that barangay's collection
entries, so the number the truck carries is the number the tricycles brought in
(spec section 7).
"""

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)

from blueprints.auth import current_user, role_required
from services import (assignment_service, collection_service, duty_service,
                      mrf_service, property_service, schedule_service, storage,
                      timeutil, unavailable_service)
from services.validation import ValidationError

collector_bp = Blueprint("collector", __name__)


# ---- Sidebar models -------------------------------------------------------

TRICYCLE_NAV = [
    {"group": None, "items": [
        # One page for the day's round: its tabs are what the separate
        # Collection List used to be.
        ("collector.tricycle_route", "Property", "home"),
    ]},
    {"group": "Duty", "items": [
        ("collector.tricycle_unavailable", "Unavailable for Duty", "alert"),
        ("collector.tricycle_history", "History", "file"),
    ]},
    {"group": "Settings", "items": [
        ("collector.tricycle_profile", "Profile", "user"),
        ("auth.logout", "Log Out", "logout"),
    ]},
]

TRUCK_NAV = [
    {"group": None, "items": [
        ("collector.truck_route", "MRF", "recycle"),
    ]},
    {"group": "Duty", "items": [
        ("collector.truck_unavailable", "Unavailable for Duty", "alert"),
        ("collector.truck_history", "History", "file"),
    ]},
    {"group": "Settings", "items": [
        ("collector.truck_profile", "Profile", "user"),
        ("auth.logout", "Log Out", "logout"),
    ]},
]

NAVS = {"tricycle": TRICYCLE_NAV, "truck": TRUCK_NAV}


def _shell(role, **extra):
    """Common template context for either collector portal."""
    user = current_user() or {}
    context = {
        "nav": NAVS[role],
        "user": user,
        "portal": role,
        # The profile card and the unavailability form both render the
        # signed-in collector's own details -- same dict, one source.
        "profile": user,
        "on_duty": duty_service.is_on_duty(storage.get("users", user["id"]) or {}),
        "home_endpoint": f"collector.{role}_route",
        "profile_endpoint": f"collector.{role}_profile",
        "tracking_endpoint": None,
        "portal_label": user.get("portal_label", "Garbage Collection Tracking System"),
    }
    context.update(extra)
    return context


def _my_assignment():
    """
    The signed-in tricycle collector's active assignment, or None.

    A Temporary Replacement is an active assignment, so the replacement sees
    the route; the original collector's own assignment is what determines
    whether they still see it (spec section 7).
    """
    user = current_user() or {}
    for row in storage.find(assignment_service.TRICYCLE_COLLECTION,
                            collector_id=user.get("id")):
        if assignment_service.is_active(row):
            return row
    return None


# ===========================================================================
# TRICYCLE COLLECTOR -- household property route
# ===========================================================================

@collector_bp.route("/tricycle/")
@role_required("tricycle_collector")
def tricycle_route():
    assignment = _my_assignment()
    properties = property_service.for_collector(assignment)
    rows = collection_service.route_with_status(properties)
    entries = collection_service.entries_for_date(
        collector_id=(current_user() or {}).get("id"))

    # Four tabs over today's list. Pending is the round still to do, by purok
    # then name -- households whose tag says nothing is expected today come
    # last, since nobody is waiting on them. Not Collected and Collected are
    # the day's recorded stops, newest first. All is every stop, in that
    # order -- which is the order the rows are sent in, so each tab is just a
    # filter over it.
    def by_place(r):
        return (1 if r["exempt"] else 0, r.get("purok") or "", r.get("owner_name") or "")

    def newest_first(group):
        return sorted(group, key=lambda r: (r.get("entry") or {}).get("timestamp") or "",
                      reverse=True)

    pending = sorted((r for r in rows if r["status"] == collection_service.PENDING),
                     key=by_place)
    missed = newest_first(r for r in rows if r["status"] == collection_service.NOT_COLLECTED)
    done = newest_first(r for r in rows if r["status"] == collection_service.COLLECTED)
    for group, name in ((pending, "pending"), (missed, "not_collected"),
                        (done, "collected")):
        for r in group:
            r["group"] = name

    tab = request.args.get("tab", "pending")
    tab = tab if tab in ("pending", "not_collected", "collected", "all") else "pending"

    return render_template(
        "tricycle-collector/route.html",
        page_title="Property",
        rows=pending + missed + done,
        tab=tab,
        tab_counts={"pending": len(pending), "not_collected": len(missed),
                    "collected": len(done), "all": len(rows)},
        puroks=sorted({r["purok"] for r in rows if r.get("purok")}),
        assignment=assignment,
        barangay=property_service.barangay_name(assignment.get("barangay_id"))
                 if assignment else None,
        counts=collection_service.counts(properties),
        totals=collection_service.totals(entries),
        today_row=schedule_service.for_date(),
        mrf=_mrf_today(assignment),
        **_shell("tricycle"),
    )


def _mrf_today(assignment):
    """
    The collector's barangay MRF as they need to see it: whether the truck
    has come for today's load, and whether any missed load is still owed and
    when it will be taken. This is the page every MRF alert links to.
    """
    from services import carryover_service

    barangay_id = (assignment or {}).get("barangay_id")
    if not barangay_id:
        return None
    card = mrf_service.mrf_card(barangay_id)
    pickup = card.get("entry") or {}
    stamp = timeutil.parse_stamp(pickup.get("timestamp"))

    owed = []
    for row in storage.find("carry_overs", barangay_id=barangay_id):
        if not carryover_service.is_open(row):
            continue
        missed_on = row.get("batch_date") or row.get("first_missed_date")
        owed.append({
            "missed": missed_on or "",
            "missed_display": timeutil.display_date(missed_on),
            "arranged": carryover_service.stage_of(row) == carryover_service.PENDING,
            "truck": row.get("current_truck"),
            "date_display": timeutil.display_date(row.get("reschedule_date"))
                            if row.get("reschedule_date") else "",
        })
    owed.sort(key=lambda r: r["missed"])

    return {
        **card,
        "truck": pickup.get("truck_code"),
        "time_display": timeutil.display_time(stamp) if stamp else "",
        "owed": owed,
    }


@collector_bp.route("/tricycle/list")
@role_required("tricycle_collector")
def tricycle_list():
    """
    The old Collection List. Its job -- the whole day's list, searchable and
    filterable -- is now the Property page's All tab, so this sends anything
    still pointing here (a bookmark, an older notification) to that tab.
    """
    return redirect(url_for("collector.tricycle_route", tab="all"))


@collector_bp.route("/tricycle/record/<property_id>", methods=["GET", "POST"])
@role_required("tricycle_collector")
def tricycle_record(property_id):
    user = current_user()
    assignment = _my_assignment()
    prop = property_service.get(property_id)

    # Scope check: the property must be inside this collector's own route.
    # Without this, any collector could record against any household in the
    # city by editing the URL.
    if not prop or not assignment:
        abort(404)
    if prop.get("barangay_id") != assignment.get("barangay_id"):
        abort(403, description="That property is outside your assigned route.")

    entry = collection_service.entry_for(property_id)
    editable = collection_service.can_edit(entry, user["id"])

    if request.method == "POST":
        if not editable:
            if _is_replay():
                return _replay_refused(
                    ValidationError({"form": "That entry can no longer be changed."}),
                    prop["owner_name"])
            flash("That entry can no longer be changed.", "danger")
            return redirect(url_for("collector.tricycle_route"))
        try:
            saved = collection_service.save_entry(
                request.form, request.files, prop, user)
        except ValidationError as exc:
            if _is_replay():
                return _replay_refused(exc, prop["owner_name"])
            for message in exc.errors.values():
                flash(message, "danger")
            return redirect(url_for("collector.tricycle_record",
                                    property_id=property_id))

        # Phase 7 emits collection_saved here so the barangay and city
        # dashboards update live.
        flash(f"{prop['owner_name']} recorded as {saved['status'].lower()}.",
              "success")
        return redirect(url_for("collector.tricycle_route"))

    return render_template(
        "tricycle-collector/record.html",
        page_title=prop["owner_name"],
        row=prop,
        entry=entry,
        editable=editable,
        waste_types=schedule_service.waste_types_for(),
        units=collection_service.UNITS,
        reasons=collection_service.NOT_COLLECTED_REASONS,
        today_row=schedule_service.for_date(),
        form_date=timeutil.today_str(),
        **_shell("tricycle"),
    )


def _is_replay() -> bool:
    """A record sent by the offline queue rather than by the form itself."""
    return request.headers.get("X-Offline-Replay") == "1"


def _replay_refused(exc: ValidationError, label: str):
    """
    Refuse a replayed record with a real error status and the reason.

    A normal submit is refused with a flash and a redirect back to the form.
    The offline queue follows that redirect, sees a 200 and counts the entry
    as sent -- so a refusal vanished, and the collector was told it had been
    saved. A 409 is what the queue treats as "rejected on its merits": it
    stops retrying and shows the collector this message instead.
    """
    return jsonify({"ok": False, "label": label,
                    "message": " ".join(exc.errors.values())}), 409


@collector_bp.route("/tricycle/history")
@role_required("tricycle_collector")
def tricycle_history():
    date = request.args.get("date") or ""
    assignment = _my_assignment()
    barangay_id = (assignment or {}).get("barangay_id")
    return render_template(
        "tricycle-collector/history.html",
        page_title="History",
        rows=collection_service.history_for_collector(
            current_user()["id"], date=date or None,
            search=request.args.get("search", ""),
            barangay_id=barangay_id),
        assignment=assignment,
        barangay=property_service.barangay_name(barangay_id) if barangay_id else None,
        selected_date=date,
        search=request.args.get("search", ""),
        **_shell("tricycle"),
    )


@collector_bp.route("/tricycle/unavailable", methods=["GET", "POST"])
@role_required("tricycle_collector")
def tricycle_unavailable():
    return _unavailable("tricycle", "tricycle-collector/unavailable.html")


@collector_bp.route("/tricycle/profile")
@role_required("tricycle_collector")
def tricycle_profile():
    # Change Password posts to auth.change_password (shared by all roles).
    return render_template(
        "tricycle-collector/profile.html",
        page_title="Profile",
        assignment=_my_assignment(),
        **_shell("tricycle"),
    )


# ===========================================================================
# DUTY -- shared by both collector roles
# ===========================================================================

@collector_bp.route("/duty", methods=["POST"])
@role_required("tricycle_collector", "truck_collector")
def toggle_duty():
    """
    On Duty starts GPS sharing and puts the collector on every live map;
    Off Duty stops it and drops the marker.
    """
    user = current_user()
    going_on = request.form.get("duty") == "on"
    duty_service.set_duty(user["id"], going_on)

    # Phase 7 emits collector_status here.
    flash("You are now On Duty. Your location is shared while you work."
          if going_on else
          "You are now Off Duty. Location sharing has stopped.", "success")

    return redirect(request.form.get("back") or
                    url_for(f"collector.{'tricycle' if user['role'] == 'tricycle_collector' else 'truck'}_route"))


@collector_bp.route("/location", methods=["POST"])
@role_required("tricycle_collector", "truck_collector")
def push_location():
    """
    Position updates from the phone's Geolocation API, every few seconds while
    on duty.

    The user id comes from the session, never the payload -- otherwise one
    collector could move another's marker. Phase 7 moves this onto a socket
    event; the REST endpoint stays as the polling fallback.
    """
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    updated = duty_service.record_location(
        user["id"], payload.get("lat"), payload.get("lng"),
        payload.get("accuracy"))

    if not updated:
        return jsonify({"ok": False, "reason": "not on duty or invalid position"}), 409
    return jsonify({"ok": True, "at": updated["last_location"]["at"]})


# ===========================================================================
# IMAGE PROOFS
# ===========================================================================

@collector_bp.route("/proofs/<path:relative>")
@role_required("tricycle_collector", "truck_collector", "barangay_admin",
               "city_admin")
def proof(relative):
    """
    Serve a not-collected photo to the people allowed to see it: admins and
    the collector who filed it (spec section 8).

    Proofs are stored outside static/ precisely so that this check is the only
    way in -- anything under static/ is served to whoever guesses the URL.

    Covers a resident's photo too. `proof_owner` finds whichever record
    references the file -- a collector's entry, an entry a resident's photo was
    filed alongside, or the report itself -- so both sides of a dispute are
    viewable through the same permission check.
    """
    entry = collection_service.proof_owner(relative)
    if not entry:
        abort(404)
    if not collection_service.may_view_proof(entry, current_user()):
        abort(403, description="That image belongs to another barangay's record.")

    path = collection_service.proof_path(relative)
    if not path:
        abort(404)
    return send_file(path, mimetype=collection_service.proof_mimetype(path))


# ===========================================================================
# TRUCK COLLECTOR -- barangay MRF route
# ===========================================================================

@collector_bp.route("/truck/")
@role_required("truck_collector")
def truck_route():
    user = current_user()
    assignment = mrf_service.assignment_for(user["id"])
    cards = mrf_service.cards_for_operator(user["id"])
    # Carry-over stops are a list of their own: an earlier day's load, on the
    # date the admin arranged, never folded into today's regular stops.
    carry_cards = mrf_service.carry_over_cards_for_operator(user["id"])

    return render_template(
        "truck-collector/route.html",
        page_title="MRF",
        cards=cards,
        carry_cards=carry_cards,
        assignment=assignment,
        load=mrf_service.running_load(user["id"]),
        today_row=schedule_service.for_date(),
        counts={
            "total": len(cards),
            "collected": sum(1 for c in cards if c["status"] == mrf_service.COLLECTED),
            "pending": sum(1 for c in cards if c["status"] == mrf_service.PENDING),
            "not_collected": sum(1 for c in cards
                                 if c["status"] == mrf_service.NOT_COLLECTED),
        },
        **_shell("truck"),
    )


@collector_bp.route("/truck/record/<barangay_id>", methods=["GET", "POST"])
@role_required("truck_collector")
def truck_record(barangay_id):
    user = current_user()
    assignment = mrf_service.assignment_for(user["id"])
    if not assignment:
        abort(403, description="You do not have an active truck assignment.")

    # Which stop this is: the day's regular stop, or a carry-over stop the
    # admin arranged for today (?carry_over=<id>, or the form field on POST).
    carry_over_id = (request.form.get("carry_over_id")
                     or request.args.get("carry_over") or None)

    # Scope check: a regular stop must be on this truck's route; a carry-over
    # stop must be one arranged for this truck today, or one it already
    # recorded today. Otherwise any operator could record against any barangay
    # by editing the URL.
    if carry_over_id:
        card = next((c for c in mrf_service.carry_over_cards_for_operator(user["id"])
                     if c["carry_over_id"] == carry_over_id
                     and c["barangay_id"] == barangay_id), None)
        if not card:
            abort(403, description="That carry-over is not arranged for your truck today.")
    else:
        if barangay_id not in set(assignment.get("covered_mrfs") or []):
            abort(403, description="That MRF is outside your assigned route.")
        card = mrf_service.mrf_card(barangay_id)

    if request.method == "POST":
        try:
            saved = mrf_service.save_pickup(request.form, barangay_id, user,
                                            carry_over_id=carry_over_id)
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
            if carry_over_id:
                return redirect(url_for("collector.truck_record",
                                        barangay_id=barangay_id,
                                        carry_over=carry_over_id))
            return redirect(url_for("collector.truck_record", barangay_id=barangay_id))

        # Phase 7 emits mrf_pickup_saved (and carry_over_created) here.
        if saved["status"] == mrf_service.NOT_COLLECTED:
            flash(f"{card['barangay']} MRF marked not collected. "
                  + ("The carry-over goes back to the City Hall Admin to arrange again."
                     if carry_over_id else
                     "A carry-over has been opened for the City Hall Admin."),
                  "warning")
        else:
            flash(f"{card['barangay']} MRF "
                  + ("carry-over " if carry_over_id else "")
                  + f"collected. {saved['load']['total']} added to your load.",
                  "success")
        return redirect(url_for("collector.truck_route"))

    return render_template(
        "truck-collector/record.html",
        page_title=f"{card['barangay']} MRF",
        card=card,
        reasons=mrf_service.NOT_COLLECTED_REASONS,
        **_shell("truck"),
    )


@collector_bp.route("/truck/deliver", methods=["POST"])
@role_required("truck_collector")
def truck_deliver():
    user = current_user()
    try:
        delivery = mrf_service.deliver(user, gps=request.form.get("gps"))
    except ValidationError as exc:
        for message in exc.errors.values():
            flash(message, "danger")
    else:
        # Phase 7 emits delivery_saved and notifies the city admin here.
        flash(f"Delivered {delivery['load']['total']} from "
              f"{len(delivery['mrfs_included'])} MRF(s) to the landfill. "
              f"Your running load is now clear.", "success")
    return redirect(url_for("collector.truck_route"))


@collector_bp.route("/truck/unavailable", methods=["GET", "POST"])
@role_required("truck_collector")
def truck_unavailable():
    return _unavailable("truck", "truck-collector/unavailable.html")


@collector_bp.route("/truck/history")
@role_required("truck_collector")
def truck_history():
    history = mrf_service.history_for_operator(
        current_user()["id"],
        date=request.args.get("date") or None,
        search=request.args.get("search", ""))
    return render_template(
        "truck-collector/history.html",
        page_title="History",
        mrf_rows=history["pickups"],
        disposal_rows=history["deliveries"],
        selected_date=request.args.get("date", ""),
        search=request.args.get("search", ""),
        **_shell("truck"),
    )


@collector_bp.route("/truck/profile")
@role_required("truck_collector")
def truck_profile():
    # Change Password posts to auth.change_password (shared by all roles).
    return render_template(
        "truck-collector/profile.html",
        page_title="Profile",
        assignment=mrf_service.assignment_for(current_user()["id"]),
        **_shell("truck"),
    )


# ---- Shared -------------------------------------------------------------

def _unavailable(role, template):
    """Unavailability request form -- identical for both collector roles."""
    user = current_user()

    if request.method == "POST":
        try:
            unavailable_service.create(request.form, user)
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        else:
            # Phase 7 notifies the city admin here.
            flash("Request submitted. The City Hall Admin has been notified so "
                  "your route can be covered.", "success")
        return redirect(url_for(f"collector.{role}_unavailable"))

    return render_template(
        template,
        page_title="Unavailable for Duty",
        reasons=unavailable_service.REASONS,
        requests=unavailable_service.for_user(user["id"]),
        today=timeutil.today_str(),
        **_shell(role),
    )
