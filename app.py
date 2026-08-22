"""
Cabadbaran City -- Garbage Collection Tracking System
Flask application factory and entry point.

Run with:  python app.py      (http://127.0.0.1:5000)

Started through Flask-SocketIO rather than `app.run`, so the live map, the
dashboard counters, and the notification bell update over a websocket. The
REST endpoints stay in place as the polling fallback, so the app is fully
usable if the socket cannot connect.
"""

import os
from pathlib import Path

from flask import Flask, redirect, url_for
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from blueprints import sockets
from blueprints.api_geo import api_geo_bp
from blueprints.api_live import api_live_bp
from blueprints.auth import auth_bp
from blueprints.brgy import brgy_bp
from blueprints.city import city_bp
from blueprints.collector import collector_bp
from blueprints.public import public_bp
from services import realtime, storage, timeutil

# threading mode keeps the dependency list to flask-socketio alone: no
# eventlet or gevent, and it works with the built-in server on a LAN or a
# small VPS, which is the deployment this is for.
socketio = SocketIO(async_mode="threading", cors_allowed_origins=[],
                    logger=False, engineio_logger=False)


def create_app():
    app = Flask(__name__)
    app.config.from_object(config.Config)

    # Behind a platform proxy (Render, nginx, a load balancer) the real client
    # address and scheme arrive in X-Forwarded-* headers. Without this Flask
    # sees the proxy's own address, which would make the public report rate
    # limit count every resident in the city as one device.
    if os.environ.get("TRUST_PROXY", "1" if config.IS_PRODUCTION else "0") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Create the data directory and any missing JSON collection before the
    # first request can touch them.
    storage.bootstrap()

    app.register_blueprint(auth_bp)
    app.register_blueprint(city_bp, url_prefix="/city")
    app.register_blueprint(brgy_bp, url_prefix="/barangay")
    app.register_blueprint(collector_bp, url_prefix="/collector")
    app.register_blueprint(public_bp, url_prefix="/public")
    app.register_blueprint(api_geo_bp, url_prefix="/api/geo")
    app.register_blueprint(api_live_bp, url_prefix="/api/live")

    # Real-time layer. Same-origin only: this app serves its own pages, so
    # there is no reason to accept a socket from anywhere else.
    socketio.init_app(app, async_mode="threading", cors_allowed_origins=[])
    realtime.init(socketio)
    sockets.register(socketio)

    # ---- Static assets ----------------------------------------------------
    # Stylesheets and scripts are versioned by their own modification time, so
    # a changed file arrives at the browser as a new URL. Without this a cached
    # CSS file silently keeps overriding an updated one, which looks exactly
    # like the change never worked -- and on Render every deploy would need a
    # hard refresh to take effect.
    _asset_versions: dict[str, str] = {}

    @app.template_global()
    def asset(filename: str) -> str:
        from flask import url_for

        version = _asset_versions.get(filename)
        if version is None or not config.IS_PRODUCTION:
            try:
                version = str(int((Path(app.static_folder) / filename).stat().st_mtime))
            except OSError:
                version = "0"
            _asset_versions[filename] = version
        return url_for("static", filename=filename, v=version)

    # ---- Template globals -------------------------------------------------
    @app.context_processor
    def inject_globals():
        from flask import g

        from services import history_service, notification_service, schedule_service

        # The lazy end-of-day job: on the first request of a new day, close off
        # and freeze any day that has not been summarised yet. No scheduler, no
        # extra dependency, and it still runs correctly if the server was off
        # overnight. Guarded per-request so one page load does it at most once.
        if not getattr(g, "_history_checked", False):
            g._history_checked = True
            try:
                history_service.ensure_frozen()
            except Exception:
                # A summary that cannot be written must never take the app
                # down; the next request tries again.
                app.logger.exception("End-of-day summary failed")

            # The T-2h arrival reminders ride along on the same lazy check,
            # for the same reason: no scheduler, and it still fires correctly
            # after the server has been switched off.
            try:
                from services import triggers
                triggers.check_arrival_reminders()
            except Exception:
                app.logger.exception("Arrival reminder check failed")

        user = getattr(g, "user", None)
        now = timeutil.now()

        return {
            "now": now,
            "today_date": timeutil.display_date(now),
            "today_time": timeutil.display_time(now),
            "today_schedule": schedule_service.for_date(),
            "alerts": notification_service.for_user(user, limit=8),
            "unread_count": notification_service.unread_count(user),
            "app_name": "Garbage Collection Tracking System",
            "city_name": "Cabadbaran City",
        }

    # ---- Filters ----------------------------------------------------------
    # Status -> badge tone. Lives here rather than in a service because it is
    # purely presentational: the same status means the same colour on every
    # screen, and nothing below the template layer needs to know about it.
    STATUS_TONES = {
        "Collected": "success",
        "Collected from MRF": "success",
        "Delivered": "success",
        "Active": "success",
        "Available": "success",
        "Pending": "warning",
        "Temporary Replacement": "warning",
        "Not Collected": "danger",
        "Missed Pickup": "danger",
        "Unavailable": "danger",
        "Disputed": "warning",
        "Inactive": "muted",
        "Ended": "muted",
    }

    @app.template_filter("tone")
    def status_tone(status):
        """Map a status string to a badge tone used by the CSS."""
        return STATUS_TONES.get(status, "muted")

    @app.template_filter("comma")
    def comma(value):
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return value

    @app.template_filter("unit")
    def unit(unit_name, qty=1):
        """Render a load unit: 'Sack' -> sack/sacks, 'Kilo' -> kg."""
        from services import collection_service
        return collection_service.format_unit(unit_name, qty)

    @app.template_filter("by_waste_type")
    def by_waste_type(lines):
        """
        Load lines gathered into one row per waste type.

        Loads are stored keyed by (type, unit) because the chart needs sacks
        and kilos as separate series. A reader does not: two rows saying
        "Bag -- 31 sacks" and "Bag -- 4 kg" read as two different wastes. This
        is a filter rather than a field on the load dict because pickups,
        deliveries, and carry-overs store a *snapshot* of their load -- records
        written before this existed have no grouped field, and regrouping at
        render time covers old and new alike.
        """
        from services import collection_service
        return collection_service.group_by_type(lines or [])

    @app.template_filter("datetime_display")
    def datetime_display(value):
        """An ISO audit timestamp as 'April 27, 2026 10:15 AM'."""
        parsed = timeutil.parse_stamp(value)
        if not parsed:
            return "—"
        return f"{timeutil.display_date(parsed)} {timeutil.display_time(parsed)}"

    @app.template_filter("date_display")
    def date_display(value):
        return timeutil.display_date(value) if value else "—"

    @app.template_filter("initials")
    def initials(name):
        parts = [p for p in str(name).split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    # ---- Routes / errors --------------------------------------------------
    @app.route("/")
    def index():
        return redirect(url_for("auth.login"))

    @app.errorhandler(400)
    def bad_request(error):
        from flask import render_template
        return render_template("error.html", code=400,
                               title="Request could not be processed",
                               message=getattr(error, "description", None) or
                                       "The form could not be submitted. Reload "
                                       "the page and try again."), 400

    @app.errorhandler(403)
    def forbidden(error):
        from flask import render_template
        return render_template("error.html", code=403,
                               title="Access denied",
                               message=getattr(error, "description", None) or
                                       "Your account does not have permission to "
                                       "view this page."), 403

    @app.errorhandler(404)
    def not_found(_):
        from flask import render_template
        return render_template("error.html", code=404,
                               title="Page not found",
                               message="The page you are looking for does not exist "
                                       "or may have been moved."), 404

    @app.errorhandler(500)
    def server_error(_):
        from flask import render_template
        return render_template("error.html", code=500,
                               title="Something went wrong",
                               message="An unexpected error occurred. "
                                       "Please try again in a moment."), 500

    return app


app = create_app()

if __name__ == "__main__":
    # Local development. In production a WSGI server imports `app` from this
    # module instead -- see docs/DEPLOYMENT.md.
    #
    # socketio.run, not app.run: the websocket transport needs it.
    socketio.run(app,
                 host=os.environ.get("HOST", "0.0.0.0"),
                 port=int(os.environ.get("PORT", 5000)),
                 debug=not config.IS_PRODUCTION,
                 allow_unsafe_werkzeug=True)
