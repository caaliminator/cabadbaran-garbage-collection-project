"""
User accounts -- create, edit, reset password, delete.

Only the City Hall Admin reaches any of this; the routes enforce that. What
lives here are the rules that must hold regardless of who is calling:

  * usernames are unique, case-insensitively
  * the required fields depend on the role -- a barangay admin needs a
    barangay, a collector needs a barangay and a vehicle of the matching type
  * a vehicle belongs to at most one account
  * the city cannot be left without an active City Hall Admin, and nobody can
    delete or deactivate themselves out of the building
"""

from services import auth_service, storage, vehicle_service
from services.auth_service import ROLES
from services.validation import ValidationError, Validator

# Roles the City Hall Admin can create, in the order the dropdown shows them.
CREATABLE_ROLES = ("barangay_admin", "tricycle_collector", "truck_collector",
                   "city_admin")

ROLE_NEEDS_BARANGAY = ("barangay_admin", "tricycle_collector")
ROLE_NEEDS_VEHICLE = {"tricycle_collector": vehicle_service.TRICYCLE,
                      "truck_collector": vehicle_service.TRUCK}


def role_options() -> list[tuple[str, str]]:
    return [(role, ROLES[role]["label"]) for role in CREATABLE_ROLES]


def barangay_options() -> list[tuple[str, str]]:
    rows = sorted(storage.read("barangays"), key=lambda b: b.get("number", 0))
    return [(b["id"], b["name"]) for b in rows]


def vehicle_options(role: str | None = None, including: str | None = None):
    """Codes selectable for a role: tricycles for one, trucks for the other."""
    wanted = ROLE_NEEDS_VEHICLE.get(role)
    if wanted:
        return vehicle_service.available(wanted, including=including)
    return (vehicle_service.available(vehicle_service.TRICYCLE, including) +
            vehicle_service.available(vehicle_service.TRUCK, including))


def vehicles_held(exclude_user: str | None = None) -> set[str]:
    """Vehicle codes already attached to an account."""
    return {u["assigned_vehicle"] for u in storage.read("users")
            if u.get("assigned_vehicle") and u.get("id") != exclude_user}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def listing(search: str = "", role: str = "", barangay: str = "",
            status: str = "") -> list[dict]:
    """
    Accounts for the User List table, newest first, with display fields
    resolved. Filtering happens here rather than in the template so the
    counter under the table reflects what the filters actually matched.
    """
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    needle = (search or "").strip().lower()

    rows = []
    for user in storage.read("users"):
        if role and user.get("role") != role:
            continue
        if barangay and user.get("assigned_barangay") != barangay:
            continue
        if status and user.get("status") != status:
            continue
        if needle and needle not in (f"{user.get('full_name', '')} "
                                     f"{user.get('username', '')}").lower():
            continue

        covered = user.get("assigned_barangays") or []
        rows.append({
            "id": user["id"],
            "full_name": user.get("full_name"),
            "username": user.get("username"),
            "role": user.get("role"),
            "role_label": auth_service.role_label(user.get("role")),
            "barangay_id": user.get("assigned_barangay"),
            "barangay": names.get(user.get("assigned_barangay")) or "—",
            "barangays": [names.get(b) for b in covered if names.get(b)],
            "vehicle": user.get("assigned_vehicle") or "—",
            "contact": user.get("contact_number") or "—",
            "status": user.get("status", "Active"),
            "created_at": user.get("created_at"),
        })

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def counts() -> dict:
    users = storage.read("users")
    return {
        "total": len(users),
        "active": sum(1 for u in users if u.get("status") == "Active"),
        "inactive": sum(1 for u in users if u.get("status") != "Active"),
        "collectors": sum(1 for u in users
                          if u.get("role") in auth_service.COLLECTOR_ROLES),
    }


def collectors(role: str, only_unassigned: bool = False,
               including: str | None = None) -> list[dict]:
    """Active collector accounts of one role, for the assign dropdowns."""
    from services import assignment_service

    held = assignment_service.assigned_collector_ids(role)
    out = []
    for user in storage.read("users"):
        if user.get("role") != role or user.get("status") != "Active":
            continue
        if only_unassigned and user["id"] in held and user["id"] != including:
            continue
        out.append({"id": user["id"], "full_name": user.get("full_name"),
                    "username": user.get("username"),
                    "vehicle": user.get("assigned_vehicle")})
    return sorted(out, key=lambda u: u["full_name"] or "")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(form, existing: dict | None = None) -> Validator:
    """Shared rules for create and edit. `existing` is set when editing."""
    v = Validator(form)
    valid_barangays = {b["id"] for b in storage.read("barangays")}

    v.text("full_name", "Full Name", required=True)
    username = v.username()
    role = v.choice("role", "Role", CREATABLE_ROLES)
    v.phone("contact_number", "Contact Number")

    if username and auth_service.username_taken(
            username, excluding_id=existing["id"] if existing else None):
        v.fail("username", f"The username {username!r} is already taken.")

    # --- role-dependent fields ---
    barangay = str(form.get("assigned_barangay", "") or "").strip()
    if role in ROLE_NEEDS_BARANGAY:
        if not barangay:
            v.fail("assigned_barangay",
                   f"{ROLES[role]['label']} accounts must have an assigned barangay.")
        elif barangay not in valid_barangays:
            v.fail("assigned_barangay", "That barangay does not exist.")
    else:
        # A city admin or truck operator is not tied to one barangay.
        barangay = ""
    v.data["assigned_barangay"] = barangay or None

    vehicle = str(form.get("assigned_vehicle", "") or "").strip().upper()
    wanted_type = ROLE_NEEDS_VEHICLE.get(role)
    if wanted_type:
        if not vehicle:
            v.fail("assigned_vehicle",
                   f"{ROLES[role]['label']} accounts must have an assigned vehicle.")
        else:
            unit = vehicle_service.by_code(vehicle)
            if not unit:
                v.fail("assigned_vehicle", f"{vehicle} is not in the vehicle registry.")
            elif unit.get("type") != wanted_type:
                v.fail("assigned_vehicle",
                       f"{vehicle} is a {unit.get('type')} unit; this role needs a "
                       f"{wanted_type}.")
            elif unit.get("status") == "inactive":
                v.fail("assigned_vehicle", f"{vehicle} is withdrawn from service.")
            elif vehicle in vehicles_held(
                    exclude_user=existing["id"] if existing else None):
                holder = next((u["full_name"] for u in storage.read("users")
                               if u.get("assigned_vehicle") == vehicle), "another user")
                v.fail("assigned_vehicle", f"{vehicle} is already assigned to {holder}.")
    else:
        vehicle = ""
    v.data["assigned_vehicle"] = vehicle or None

    return v


def _validate_new_password(v: Validator, form) -> str:
    password = str(form.get("password", "") or "")
    confirm = str(form.get("confirm_password", "") or "")
    if not password:
        v.fail("password", "Password is required.")
    elif len(password) < auth_service.MIN_PASSWORD_LENGTH:
        v.fail("password", f"Password must be at least "
                           f"{auth_service.MIN_PASSWORD_LENGTH} characters.")
    elif password != confirm:
        v.fail("confirm_password", "The passwords do not match.")
    return password


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def create(form, actor: str | None = None) -> dict:
    v = _validate(form)
    password = _validate_new_password(v, form)
    v.raise_if_invalid()

    return storage.insert("users", {
        "full_name": v.data["full_name"],
        "username": v.data["username"],
        "password_hash": auth_service.hash_password(password),
        "role": v.data["role"],
        "assigned_barangay": v.data["assigned_barangay"],
        "assigned_barangays": [],
        "assigned_vehicle": v.data["assigned_vehicle"],
        "assigned_purok": None,
        "contact_number": v.data["contact_number"],
        "status": "Active",
        "must_change_password": False,
    }, actor)


def edit(user_id: str, form, actor: str | None = None) -> dict:
    existing = storage.get("users", user_id)
    if not existing:
        raise ValidationError({"form": "That account no longer exists."})

    v = _validate(form, existing=existing)

    status = str(form.get("status", existing.get("status", "Active"))).strip()
    if status not in ("Active", "Inactive"):
        v.fail("status", "Status must be Active or Inactive.")

    # Losing the last active City Hall Admin would lock everyone out of user
    # management permanently -- there is no other way to create one.
    if (existing.get("role") == "city_admin"
            and (v.data.get("role") != "city_admin" or status != "Active")
            and _active_city_admins(excluding=user_id) == 0):
        v.fail("role", "This is the only active City Hall Admin. Create another "
                       "one before changing this account.")

    v.raise_if_invalid()

    return storage.update("users", user_id, {
        "full_name": v.data["full_name"],
        "username": v.data["username"],
        "role": v.data["role"],
        "assigned_barangay": v.data["assigned_barangay"],
        "assigned_vehicle": v.data["assigned_vehicle"],
        "contact_number": v.data["contact_number"],
        "status": status,
    }, actor)


def set_status(user_id: str, status: str, actor: str | None = None) -> dict:
    user = storage.get("users", user_id)
    if not user:
        raise ValidationError({"form": "That account no longer exists."})
    if status not in ("Active", "Inactive"):
        raise ValidationError({"form": "Status must be Active or Inactive."})

    if status == "Inactive":
        if user_id == actor:
            raise ValidationError({"form": "You cannot deactivate your own account."})
        if (user.get("role") == "city_admin"
                and _active_city_admins(excluding=user_id) == 0):
            raise ValidationError({"form": (
                "This is the only active City Hall Admin. Create another one first.")})

    return storage.update("users", user_id, {"status": status}, actor)


def delete(user_id: str, actor: str | None = None) -> dict:
    user = storage.get("users", user_id)
    if not user:
        raise ValidationError({"form": "That account no longer exists."})
    if user_id == actor:
        raise ValidationError({"form": "You cannot delete your own account."})
    if user.get("role") == "city_admin" and _active_city_admins(excluding=user_id) == 0:
        raise ValidationError({"form": (
            "This is the only active City Hall Admin. Create another one first.")})

    # Assignments outlive the account otherwise, leaving a route pointing at
    # a collector who no longer exists.
    for collection, field in (("assignments_tricycle", "collector_id"),
                              ("assignments_truck", "operator_id")):
        for row in storage.find(collection, **{field: user_id}):
            storage.delete(collection, row["id"])

    return storage.delete("users", user_id)


def reset_password(user_id: str, form, actor: str | None = None) -> dict:
    v = Validator(form)
    password = str(form.get("new_password", "") or "")
    confirm = str(form.get("confirm_password", "") or "")
    if not password:
        v.fail("new_password", "Enter a new password.")
    elif len(password) < auth_service.MIN_PASSWORD_LENGTH:
        v.fail("new_password", f"Password must be at least "
                               f"{auth_service.MIN_PASSWORD_LENGTH} characters.")
    elif password != confirm:
        v.fail("confirm_password", "The passwords do not match.")
    v.raise_if_invalid()

    return auth_service.reset_password(user_id, password, confirm, actor)


def _active_city_admins(excluding: str | None = None) -> int:
    return sum(1 for u in storage.read("users")
               if u.get("role") == "city_admin" and u.get("status") == "Active"
               and u.get("id") != excluding)
