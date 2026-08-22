"""
Public viewer -- the citizen-facing pages. No authentication.

Four tabs, mobile-first, because residents open this on a phone at the kerb:

    /public/            live map of the collectors working right now
    /public/schedule    month calendar + the coming week
    /public/today       what is accepted today
    /public/report      "was your waste collected?" -- anonymous

Nothing here trusts the client. The report form re-validates every field, the
rate limit is enforced server-side, and a resident's report can never overwrite
a collector's record (see public_report_service).
"""

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from config import Config
from services import public_report_service, schedule_service, timeutil
from services.validation import ValidationError

public_bp = Blueprint("public", __name__)

TABS = [
    ("public.viewer", "Live Map", "radar"),
    ("public.schedule", "Schedule", "calendar"),
    ("public.today", "Today's Waste", "leaf"),
    ("public.report", "Report", "home"),
]

# Reminders shown on the Today's Waste page.
REMINDERS = [
    "Segregate your waste before the collector arrives — mixed waste is not collected.",
    "Bring your waste out before the collector reaches your purok.",
    "Special or hazardous waste is only collected on its scheduled day.",
    "If your waste was missed, tell your barangay through the Report tab.",
]


@public_bp.context_processor
def inject_public():
    return {
        "tabs": TABS,
        "map_barangays": public_report_service.barangay_options(),
    }


def _client_ip() -> str:
    """
    The caller's address, honouring one proxy hop.

    Only the first entry of X-Forwarded-For is used, and only as a rate-limit
    input -- the header is attacker-controllable, so it must never be treated
    as identity or authorisation.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


@public_bp.route("/")
def viewer():
    return render_template(
        "public-viewer/map.html",
        page_title="Live Tracking",
        active_tab="public.viewer",
        today_row=schedule_service.for_date(),
    )


@public_bp.route("/schedule")
def schedule():
    today = timeutil.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        if not 1 <= month <= 12 or not 2000 <= year <= 2100:
            raise ValueError
    except (TypeError, ValueError):
        year, month = today.year, today.month

    return render_template(
        "public-viewer/schedule.html",
        page_title="Waste Collection Schedule",
        active_tab="public.schedule",
        cal=schedule_service.month_calendar(year, month),
        upcoming=schedule_service.upcoming_days(6),
        week=schedule_service.week(),
    )


@public_bp.route("/today")
def today():
    return render_template(
        "public-viewer/today.html",
        page_title="Today's Waste",
        active_tab="public.today",
        cards=schedule_service.todays_cards(),
        today_row=schedule_service.for_date(),
        notes=REMINDERS,
    )


@public_bp.route("/report", methods=["GET", "POST"])
def report():
    """
    The anonymous feedback flow: Barangay -> Purok -> property -> status.

    Built as a plain form that reloads with each choice, so it works with no
    JavaScript at all; the selects are enhanced to update in place where JS is
    available. A resident at the kerb on a poor connection should still be able
    to report.
    """
    ip = _client_ip()
    fingerprint = request.cookies.get("gcts_device", "")

    barangay_id = request.form.get("barangay_id") or request.args.get("barangay") or ""
    purok = request.form.get("purok") or request.args.get("purok") or ""

    if request.method == "POST" and request.form.get("action") == "submit":
        try:
            saved = public_report_service.submit(request.form, ip, fingerprint)
        except ValidationError as exc:
            for message in exc.errors.values():
                flash(message, "danger")
        else:
            if saved["disputed"]:
                flash("Thank you. Your report differs from what the collector "
                      "recorded, so your barangay admin has been asked to "
                      "check it.", "warning")
            else:
                flash("Thank you. Your report has been sent to your barangay "
                      "admin.", "success")
            # Phase 7 notifies the barangay and city admins here.
            return redirect(url_for("public.report", barangay=barangay_id))

    return render_template(
        "public-viewer/report.html",
        page_title="Report Collection",
        active_tab="public.report",
        barangays=public_report_service.barangay_options(),
        puroks=public_report_service.purok_options(barangay_id),
        properties=public_report_service.property_options(
            barangay_id, purok, request.args.get("search", "")),
        selected_barangay=barangay_id,
        selected_purok=purok,
        remaining=public_report_service.remaining_today(ip, fingerprint),
        limit=Config.PUBLIC_REPORT_DAILY_LIMIT,
    )


@public_bp.get("/api/properties")
def api_properties():
    """Cascading options for the report form, when JavaScript is available."""
    barangay_id = request.args.get("barangay", "")
    return jsonify({
        "puroks": public_report_service.purok_options(barangay_id),
        "properties": public_report_service.property_options(
            barangay_id, request.args.get("purok", ""),
            request.args.get("search", "")),
    })
