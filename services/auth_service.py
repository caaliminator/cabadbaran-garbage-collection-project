"""
Authentication and account rules.

Everything that touches a password lives here. Routes ask this module whether
a login is valid or a password change is allowed; they never hash, compare, or
store a password themselves.

Passwords are hashed with `werkzeug.security`. No plaintext password is ever
written to `users.json`, logged, or returned from any function in this module.
"""

from werkzeug.security import check_password_hash, generate_password_hash

from services import storage

MIN_PASSWORD_LENGTH = 8

# A real hash of a value nobody knows, compared against when the username does
# not exist. Without this, a missing user returns noticeably faster than a
# wrong password, which lets an attacker enumerate valid usernames by timing.
_DUMMY_HASH = generate_password_hash("no-such-account-timing-equaliser")

# role key -> everything the UI needs to render and route that role.
# `portal` is the existing template/CSS namespace; `home` is where a login of
# that role lands.
ROLES = {
    "city_admin": {
        "label": "City Hall Admin",
        "portal": "city",
        "portal_label": "City Hall Administration",
        "home": "city.dashboard",
        "scope": "city",
    },
    "barangay_admin": {
        "label": "Barangay Admin",
        "portal": "brgy",
        "portal_label": "Garbage Collection Tracking System",
        "home": "brgy.dashboard",
        "scope": "barangay",
    },
    "tricycle_collector": {
        "label": "Tricycle Garbage Collector",
        "portal": "tricycle",
        "portal_label": "Garbage Collection Tracking System",
        "home": "collector.tricycle_route",
        "scope": "assignment",
    },
    "truck_collector": {
        "label": "Truck Garbage Collector",
        "portal": "truck",
        "portal_label": "Garbage Collection Tracking System",
        "home": "collector.truck_route",
        "scope": "assignment",
    },
}

ADMIN_ROLES = ("city_admin", "barangay_admin")
COLLECTOR_ROLES = ("tricycle_collector", "truck_collector")

# portal key -> role key, so existing blueprints can keep speaking "brgy".
PORTAL_ROLES = {meta["portal"]: role for role, meta in ROLES.items()}


class AuthError(Exception):
    """A login or password change that failed a rule, with a human message."""

    def __init__(self, message: str, field: str = "form"):
        super().__init__(message)
        self.message = message
        self.field = field


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def by_username(username: str) -> dict | None:
    """Case-insensitive username lookup -- nobody remembers their capitals."""
    wanted = (username or "").strip().lower()
    if not wanted:
        return None
    for user in storage.read("users"):
        if str(user.get("username", "")).lower() == wanted:
            return user
    return None


def by_id(user_id: str) -> dict | None:
    return storage.get("users", user_id)


def username_taken(username: str, excluding_id: str | None = None) -> bool:
    found = by_username(username)
    return bool(found and found.get("id") != excluding_id)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> dict:
    """
    Return the user record on success, or raise AuthError.

    The wrong-credentials message is deliberately identical whether the
    username or the password was wrong: telling someone "no such user" hands
    them half the answer.
    """
    user = by_username(username)

    if not user:
        check_password_hash(_DUMMY_HASH, password or "")
        raise AuthError("Incorrect username or password. Please try again.")

    if not check_password_hash(user.get("password_hash", ""), password or ""):
        raise AuthError("Incorrect username or password. Please try again.")

    if user.get("status") != "Active":
        raise AuthError(
            "This account is inactive. Contact the City Hall Admin to reactivate it.")

    if user.get("role") not in ROLES:
        raise AuthError("This account has no valid role. Contact the City Hall Admin.")

    return user


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def validate_password(new_password: str, confirm: str) -> None:
    """Shared rules for both self-service change and admin reset."""
    if not new_password:
        raise AuthError("Enter a new password.", "new_password")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            "new_password")
    if new_password != confirm:
        raise AuthError("The passwords do not match.", "confirm_password")


def change_password(user_id: str, current: str, new_password: str,
                    confirm: str) -> dict:
    """Self-service change. The current password must be correct."""
    user = by_id(user_id)
    if not user:
        raise AuthError("Account not found.")

    if not check_password_hash(user.get("password_hash", ""), current or ""):
        raise AuthError("Your current password is not correct.", "current_password")

    validate_password(new_password, confirm)

    if check_password_hash(user.get("password_hash", ""), new_password):
        raise AuthError("The new password must be different from the current one.",
                        "new_password")

    return storage.update("users", user_id, {
        "password_hash": generate_password_hash(new_password),
        "must_change_password": False,
    }, actor=user_id)


def reset_password(user_id: str, new_password: str, confirm: str,
                   actor: str) -> dict:
    """
    Admin-set password (City Hall Admin only -- the route enforces the role).
    No current password needed, and the user is asked to change it at next
    login.
    """
    user = by_id(user_id)
    if not user:
        raise AuthError("Account not found.")
    validate_password(new_password, confirm)
    return storage.update("users", user_id, {
        "password_hash": generate_password_hash(new_password),
        "must_change_password": True,
    }, actor=actor)


def hash_password(password: str) -> str:
    """For account creation in Phase 2. Validate before calling."""
    return generate_password_hash(password)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def role_label(role: str) -> str:
    return ROLES.get(role, {}).get("label", role or "Unknown")


def home_endpoint(role: str) -> str:
    return ROLES.get(role, {}).get("home", "auth.login")


def public_view(user: dict) -> dict:
    """
    The safe, template-friendly shape of a user.

    Never includes `password_hash` -- this is what goes into `g` and into every
    template, so the hash must not travel with it.
    """
    if not user:
        return {}

    role = user.get("role")
    meta = ROLES.get(role, {})
    barangays = {b["id"]: b["name"] for b in storage.read("barangays")}

    barangay_id = user.get("assigned_barangay")
    barangay_ids = user.get("assigned_barangays") or []
    if barangay_id and not barangay_ids:
        barangay_ids = [barangay_id]

    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "name": user.get("full_name"),
        "role": role,
        "role_label": meta.get("label", role),
        "portal": meta.get("portal"),
        "portal_label": meta.get("portal_label", "Garbage Collection Tracking System"),
        "scope": meta.get("scope"),
        "barangay_id": barangay_id,
        "barangay": barangays.get(barangay_id),
        "barangay_ids": barangay_ids,
        "barangays": [barangays.get(b) for b in barangay_ids if barangays.get(b)],
        # Purok coverage and vehicle come from the assignment records built in
        # Phase 2; until then the account's own vehicle field is the source.
        "purok": user.get("assigned_purok"),
        "puroks": user.get("assigned_puroks") or [],
        "vehicle": user.get("assigned_vehicle"),
        "contact": user.get("contact_number"),
        "status": user.get("status"),
        "must_change_password": bool(user.get("must_change_password")),
    }
