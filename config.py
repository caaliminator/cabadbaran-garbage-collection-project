"""
Application configuration.

Every tunable that a deployment might need to change lives here, so no route,
template, or JS file has to be edited to point the system at a different city,
turn a layer on, or move the data directory.

Nothing in this file is a credential. SECRET_KEY comes from the environment;
if it is absent a random key is generated once and persisted to
`instance/.secret_key` so sessions survive a restart without anybody having to
paste a secret into source control.

Deployment overrides, all optional and all read from the environment:

    SECRET_KEY      required in production -- see the warning in _secret_key()
    DATA_ROOT       where the JSON store and uploads live. On a host with an
                    ephemeral filesystem (Render, Heroku, most containers)
                    this MUST point at a mounted persistent disk, or every
                    record is lost on the next deploy or restart.
    FLASK_ENV       set to "production" to harden the session cookie
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Where mutable data lives. Defaults to the project directory for local use;
# a deployment points it at a persistent disk.
DATA_ROOT = Path(os.environ.get("DATA_ROOT", BASE_DIR))

IS_PRODUCTION = os.environ.get("FLASK_ENV", "").lower() == "production"


def _secret_key() -> str:
    """
    Env var first, then a generated key persisted outside source control.

    On an ephemeral filesystem the generated key does not survive a restart,
    which silently signs everyone out on every deploy. In production that is a
    misconfiguration worth refusing to start over, rather than shipping a
    confusing intermittent bug.
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env

    if IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY must be set in production. Without it a new key is "
            "generated on each restart, signing out every user and "
            "invalidating every CSRF token.")

    instance = DATA_ROOT / "instance"
    instance.mkdir(parents=True, exist_ok=True)
    key_file = instance / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


class Config:
    # ---- Flask ------------------------------------------------------------
    SECRET_KEY = _secret_key()
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Only over HTTPS in production. Left off locally, where the app is served
    # over plain http on a LAN and a secure-only cookie would never be sent.
    SESSION_COOKIE_SECURE = IS_PRODUCTION
    TEMPLATES_AUTO_RELOAD = not IS_PRODUCTION

    # Inactivity timeout for admin sessions (Phase 1 enforces it).
    SESSION_IDLE_MINUTES = 60
    # "Remember me" lifetime.
    REMEMBER_DAYS = 30

    # ---- Storage ----------------------------------------------------------
    DATA_DIR = DATA_ROOT / "data"
    GEO_DIR = DATA_DIR / "geo"

    # Image proofs live OUTSIDE static/. The spec names
    # /static/uploads/proofs/, but it also requires that only admins and the
    # owning collector can view a proof -- and anything under static/ is served
    # to anyone who guesses the URL. They are served instead by a
    # permission-checked route, /proofs/<date>/<filename>.
    UPLOAD_DIR = DATA_ROOT / "uploads" / "proofs"
    MAX_PROOF_BYTES = 5 * 1024 * 1024          # 5 MB, per spec 6.4
    ALLOWED_PROOF_TYPES = ("image/jpeg", "image/png")
    ALLOWED_PROOF_EXTENSIONS = (".jpg", ".jpeg", ".png")

    # ---- City ------------------------------------------------------------
    CITY_NAME = "Cabadbaran City"
    TOTAL_BARANGAYS = 31
    # Hard ceiling on registered truck units for the whole city.
    MAX_TRUCKS = 8
    # Zone colour groups: (first_number, last_number, key, label).
    # Group membership is derived from a barangay's number, never from
    # coordinates, so it stays correct before the real boundaries arrive.
    ZONE_GROUPS = (
        (1, 6, "zone-a", "Barangay 1-6"),
        (7, 14, "zone-b", "Barangay 7-14"),
        (15, 22, "zone-c", "Barangay 15-22"),
        (23, 31, "zone-d", "Barangay 23-31"),
    )

    # ---- Map --------------------------------------------------------------
    # Cabadbaran City centre. The only coordinates in the codebase, and they
    # exist purely so an empty map opens somewhere sensible.
    MAP_DEFAULT_CENTER = (9.1226, 125.5344)
    MAP_DEFAULT_ZOOM = 12
    MAP_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
    MAP_TILE_ATTRIBUTION = "&copy; OpenStreetMap contributors"

    # ---- Hotspot layer ----------------------------------------------------
    # On by default: in "derived" mode the layer computes hotspots from Not
    # Collected entries and resident reports, so it shows something real
    # without waiting for external survey data. Set to False to hide it.
    HOTSPOT_LAYER_ENABLED = True
    # "file"    -> read data/geo/hotspots.geojson as-is
    # "derived" -> compute from Not Collected entries + public reports
    HOTSPOT_SOURCE = "derived"
    # Report/entry counts at or above which a derived hotspot takes a severity.
    HOTSPOT_SEVERITY_THRESHOLDS = {"low": 1, "medium": 3, "high": 6}
    # Date window used when the caller does not pass from/to.
    HOTSPOT_DEFAULT_WINDOW_DAYS = 30

    # ---- Public viewer ----------------------------------------------------
    PUBLIC_REPORT_DAILY_LIMIT = 3              # per IP per day

    # ---- Real time (wired in Phase 7) -------------------------------------
    GPS_THROTTLE_SECONDS = 8
    TRUCK_APPROACH_METRES = 500
    ARRIVAL_REMINDER_HOURS = 2
    POLL_FALLBACK_SECONDS = 30


def as_dict() -> dict:
    """Uppercase config values, for `app.config.from_object`-style access."""
    return {k: v for k, v in vars(Config).items() if k.isupper()}
