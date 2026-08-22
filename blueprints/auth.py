"""
Login, logout, session handling, CSRF, and the access-control decorators.

One `/login` serves every role; the account's own role decides where it lands.
Public viewer pages need no account at all.

Three things are enforced here for the whole application, not per blueprint:

  * CSRF -- every state-changing request must carry a token that matches the
    one in the session, checked in a `before_app_request` hook so a form that
    forgets its token fails closed rather than silently posting
  * idle timeout -- admin sessions expire after a period of inactivity
  * role + scope -- `@role_required` gates the route, and the scope helpers
    make sure a barangay admin or collector can only reach their own data.
    Scope is always derived from the session, never from a request parameter.
"""

import hmac
import secrets
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, session, url_for)
from markupsafe import Markup

from services import auth_service, timeutil
from services.auth_service import ROLES, AuthError

auth_bp = Blueprint("auth", __name__)

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_token() -> str:
    """The session's CSRF token, minted on first use."""
    if CSRF_SESSION_KEY not in session:
        session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return session[CSRF_SESSION_KEY]


def csrf_field() -> Markup:
    """`{{ csrf_field() }}` -- the hidden input every POST form needs."""
    return Markup(
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{csrf_token()}">')


def _csrf_ok() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    supplied = request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER, "")
    return hmac.compare_digest(str(expected), str(supplied))


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

@auth_bp.before_app_request
def _guard_request():
    """CSRF check, session expiry, and `g.user` for the current request."""
    g.user = _load_session_user()

    if g.user and _session_expired():
        name = g.user["name"]
        session.clear()
        g.user = None
        flash(f"Your session timed out after a period of inactivity, {name}. "
              "Please sign in again.", "warning")
        return redirect(url_for("auth.login", next=request.path))

    if request.method not in SAFE_METHODS and not _csrf_ok():
        # A stale tab after a logout is the common cause, so say something
        # actionable rather than just "403".
        abort(400, description="Your session token expired or did not match. "
                               "Reload the page and try again.")

    if g.user:
        session["last_seen"] = timeutil.stamp()
    return None


def _load_session_user() -> dict | None:
    """
    Resolve the session to a live user record on every request.

    Reading the account fresh each time is deliberate: if an admin deactivates
    or deletes a user, or changes their role, that takes effect on the user's
    very next request rather than whenever they happen to log out.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None

    record = auth_service.by_id(user_id)
    if not record or record.get("status") != "Active" or record.get("role") not in ROLES:
        session.clear()
        return None

    return auth_service.public_view(record)


def _session_expired() -> bool:
    """Idle timeout, applied to admin roles only (spec section 3)."""
    if not g.user or g.user["role"] not in auth_service.ADMIN_ROLES:
        return False

    last_seen = timeutil.parse_stamp(session.get("last_seen"))
    if not last_seen:
        return False

    idle_minutes = current_app.config.get("SESSION_IDLE_MINUTES", 60)
    return (timeutil.now() - last_seen) > timedelta(minutes=idle_minutes)


@auth_bp.app_context_processor
def _inject_auth():
    """Auth helpers available to every template, logged in or not."""
    return {
        "csrf_field": csrf_field,
        "csrf_token": csrf_token,
        "current_user": getattr(g, "user", None),
    }


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def current_user() -> dict | None:
    """The signed-in user as a plain dict, or None. Never carries the hash."""
    return getattr(g, "user", None)


def role_required(*roles: str):
    """
    Gate a view to one or more roles.

    Accepts role keys (`city_admin`) or the older portal keys (`city`), so the
    existing blueprints keep working while they are migrated.
    """
    wanted = {auth_service.PORTAL_ROLES.get(r, r) for r in roles}

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for("auth.login", next=request.path))
            if user["role"] not in wanted:
                # Signed in, but as the wrong role: this is a permission
                # failure, not a login prompt.
                abort(403, description="Your account does not have access to this page.")
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Kept so existing blueprints calling `login_required("city")` still work.
login_required = role_required


def scoped_barangay_id() -> str | None:
    """The barangay a barangay admin is limited to. None for the city admin."""
    user = current_user()
    if not user:
        return None
    return user.get("barangay_id")


def require_barangay_scope(barangay_id: str) -> None:
    """
    Abort unless the signed-in user may touch this barangay's data.

    Call this in any route that takes a barangay from the request. The city
    admin passes everything; a barangay admin passes only their own; a
    collector passes only a barangay they are assigned to.
    """
    user = current_user()
    if not user:
        abort(403)
    if user["role"] == "city_admin":
        return
    allowed = set(user.get("barangay_ids") or [])
    if user.get("barangay_id"):
        allowed.add(user["barangay_id"])
    if barangay_id not in allowed:
        abort(403, description="That barangay is outside your assigned scope.")


def require_owner(owner_id: str) -> None:
    """Abort unless the signed-in user owns this record, or is the city admin."""
    user = current_user()
    if not user:
        abort(403)
    if user["role"] == "city_admin" or user["id"] == owner_id:
        return
    abort(403, description="That record belongs to another user.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    user = current_user()
    if user and request.method == "GET":
        return redirect(url_for(auth_service.home_endpoint(user["role"])))

    errors, username = {}, ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username:
            errors["username"] = "Username is required."
        if not password:
            errors["password"] = "Password is required."

        if not errors:
            try:
                record = auth_service.authenticate(username, password)
            except AuthError as exc:
                errors[exc.field] = exc.message
            else:
                _start_session(record, remember=bool(request.form.get("remember")))
                view = auth_service.public_view(record)
                flash(f"Welcome back, {view['name']}.", "success")

                if view["must_change_password"]:
                    flash("Please set a new password before you continue.", "warning")

                return redirect(_safe_next() or
                                url_for(auth_service.home_endpoint(view["role"])))

    return render_template(
        "auth/login.html",
        errors=errors,
        username=username,
        forgot=request.args.get("forgot"),
    )


def _start_session(record: dict, remember: bool) -> None:
    """
    Fresh session, then the identity. Rotating the session on login stops a
    token fixed before authentication from being reused afterwards.
    """
    session.clear()
    session["user_id"] = record["id"]
    session["role"] = record["role"]
    session["last_seen"] = timeutil.stamp()
    session.permanent = remember
    if remember:
        days = current_app.config.get("REMEMBER_DAYS", 30)
        current_app.permanent_session_lifetime = timedelta(days=days)
    csrf_token()


def _safe_next() -> str | None:
    """
    Honour ?next= only for local paths.

    `//evil.example` and `https://evil.example` are both absolute despite the
    leading slash, so a bare `startswith("/")` check is not enough to stop an
    open redirect.
    """
    target = request.args.get("next") or request.form.get("next")
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["POST"])
def change_password():
    """
    Shared by every role's Profile page. Posts here, returns there -- so the
    rules live in one place instead of four.
    """
    user = current_user()
    if not user:
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("auth.login"))

    back = _safe_next() or url_for(auth_service.home_endpoint(user["role"]))

    try:
        auth_service.change_password(
            user["id"],
            request.form.get("current_password", ""),
            request.form.get("new_password", ""),
            request.form.get("confirm_password", ""),
        )
    except AuthError as exc:
        flash(exc.message, "danger")
    else:
        flash("Your password has been updated.", "success")

    return redirect(back)
