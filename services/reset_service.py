"""
Clearing the operational data, leaving a store real records can go into.

This is the one definition of "what is a transactional record". Both callers
use it -- `tools/reset_data.py` from a terminal and the City Hall Admin's
Danger Zone from the browser -- so the two can never drift into deleting
different things.

WHAT GOES
    Every record of something that happened: properties, collection entries,
    MRF pickups, landfill deliveries, carry-overs, unavailability requests,
    resident reports, notifications, frozen day history, and both assignment
    tables. Image proofs go with them -- a photo whose entry no longer exists
    is an orphan nobody can reach or review.

    Accounts created by `tools/make_demo_day.py` go too. They are marked
    `demo_generated`, which is exactly what that flag is for.

WHAT STAYS
    The real logins, and the reference data the system cannot start without:
    the barangays (with their purok lists), the vehicle registry, and the
    weekly waste schedule. That is configuration, not sample data -- deleting
    it would leave an app that cannot accept real records either.

    Geo files under data/geo/ are untouched; replacing those is a separate job
    with a separate tool (see docs/DATA_REQUIREMENTS.md).

AFTERWARDS
    Vehicles are free again, and collector accounts lose the fields that
    mirrored a now-deleted assignment. A barangay admin keeps its barangay --
    that is the scope of the account, not a mirror of an assignment.
"""

import shutil
from pathlib import Path

from config import Config
from services import storage

ACTOR = "reset"

# Records of what happened. All of it is sample data on a fresh install.
WIPE = (
    "assignments_tricycle",
    "assignments_truck",
    "properties",
    "collections",
    "mrf_pickups",
    "deliveries",
    "carry_overs",
    "unavailable_requests",
    "public_reports",
    "notifications",
    "history",
)

# Configuration. Seeded once, edited through the admin UI, never sample data.
KEEP = ("barangays", "vehicles", "waste_schedule")

# Written onto a *collector's* account by an assignment or a shift. With the
# assignment gone they would describe a route that no longer exists.
COLLECTOR_ROLES = ("tricycle_collector", "truck_collector")

STALE_COLLECTOR_FIELDS = {
    "assigned_barangay": None,
    "assigned_barangays": [],
    "assigned_vehicle": None,
    "assigned_puroks": [],
    "assigned_purok": None,
}

# Duty state belongs to a shift, and every shift is over.
STALE_DUTY_FIELDS = {
    "on_duty": False,
    "duty_since": None,
    "last_location": None,
}


def demo_accounts() -> list[dict]:
    return [u for u in storage.read("users") if u.get("demo_generated")]


def kept_accounts() -> list[dict]:
    return [u for u in storage.read("users") if not u.get("demo_generated")]


def proof_count() -> int:
    proofs = Path(Config.UPLOAD_DIR)
    if not proofs.exists():
        return 0
    return sum(1 for p in proofs.rglob("*") if p.is_file())


def plan() -> dict:
    """What a run would remove, without removing any of it."""
    counts = {name: storage.count(name) for name in WIPE}
    counts["users (demo accounts)"] = len(demo_accounts())
    counts["image proofs"] = proof_count()
    return counts


def kept() -> dict:
    """What a run would leave behind."""
    counts = {name: storage.count(name) for name in KEEP}
    counts["users (real logins)"] = len(kept_accounts())
    return counts


def run(actor: str = ACTOR) -> dict:
    """
    Clear the store. Returns the counts that were removed.

    Deliberately not wrapped in a single transaction: storage locks per
    collection, and one all-or-nothing write across eleven of them is not
    something the storage layer offers. A partial run leaves less data, never
    inconsistent data -- every collection here is independently emptied, and
    the derived fields are rewritten from scratch at the end.
    """
    removed = plan()

    for name in WIPE:
        storage.write(name, [])

    for user in storage.read("users"):
        if user.get("demo_generated"):
            storage.delete("users", user["id"])
            continue
        changes = dict(STALE_DUTY_FIELDS)
        if user.get("role") in COLLECTOR_ROLES:
            changes.update(STALE_COLLECTOR_FIELDS)
        storage.update("users", user["id"], changes, actor)

    # Every unit is free again: the assignments holding them are gone.
    for unit in storage.read("vehicles"):
        if unit.get("status") != "available":
            storage.update("vehicles", unit["id"], {"status": "available"}, actor)

    proofs = Path(Config.UPLOAD_DIR)
    if proofs.exists():
        shutil.rmtree(proofs, ignore_errors=True)
    proofs.mkdir(parents=True, exist_ok=True)

    return removed
