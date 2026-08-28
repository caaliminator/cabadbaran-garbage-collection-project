"""
Clear the demo records and leave a store you can put real data into.

    python tools/reset_data.py --dry-run   # say what would go, change nothing
    python tools/reset_data.py             # do it, after a confirmation
    python tools/reset_data.py --yes       # do it without asking

WHAT GOES
---------
Every operational record: properties, collection entries, MRF pickups,
landfill deliveries, carry-overs, unavailability requests, resident reports,
notifications, the frozen day history, and both assignment tables. Image
proofs are deleted with them -- a photo whose entry no longer exists is an
orphan nobody can reach or review.

Accounts created by `tools/make_demo_day.py` (the `brgy01_admin` / `tri01` /
`trk01` cast) go too. They are marked `demo_generated`, which is exactly what
that flag is for.

WHAT STAYS
----------
The four real logins, and the reference data the system cannot start without:
the 31 barangays, the vehicle registry, and the weekly waste schedule. Those
are configuration, not sample records -- deleting them would leave an app that
cannot accept real data either.

Geo files under data/geo/ are left alone. If they are still the illustrative
shapes from make_demo_geo.py, replacing them is a separate job with a separate
tool -- see docs/DATA_REQUIREMENTS.md.

AFTERWARDS
----------
Vehicles go back to `available`, and collector accounts lose the fields that
mirrored a now-deleted assignment (route, vehicle, duty state, last known
position). A barangay admin keeps its barangay -- that is the scope of the
account, not a mirror of an assignment. Signing in as `city_admin` and
assigning a collector is the first real step.
"""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
#
# Only collectors. A barangay admin's `assigned_barangay` is not a mirror of
# anything -- it is the scope of the account itself, set in User Management,
# and clearing it would leave an admin who can see no barangay at all.
COLLECTOR_ROLES = ("tricycle_collector", "truck_collector")

STALE_COLLECTOR_FIELDS = {
    "assigned_barangay": None,
    "assigned_barangays": [],
    "assigned_vehicle": None,
    "assigned_puroks": [],
    "assigned_purok": None,
}

# Duty state belongs to a shift, and every shift is over. Cleared for every
# role, since only collectors ever carry it.
STALE_DUTY_FIELDS = {
    "on_duty": False,
    "duty_since": None,
    "last_location": None,
}


def plan() -> dict:
    """What a run would remove, without removing any of it."""
    demo_users = [u for u in storage.read("users") if u.get("demo_generated")]
    counts = {name: storage.count(name) for name in WIPE}
    counts["users (demo accounts)"] = len(demo_users)

    proofs = Path(Config.UPLOAD_DIR)
    counts["image proofs"] = (
        sum(1 for p in proofs.rglob("*") if p.is_file()) if proofs.exists() else 0)
    return counts


def run(assume_yes: bool = False, dry_run: bool = False) -> int:
    storage.bootstrap()
    counts = plan()
    total = sum(counts.values())

    print("Cabadbaran City data store: "
          f"{Config.DATA_DIR}\n")
    print("  Would remove:" if dry_run else "  Removing:")
    for name, count in counts.items():
        print(f"    {name:28} {count:>5}")
    print()
    print("  Keeping:")
    for name in KEEP:
        print(f"    {name:28} {storage.count(name):>5}")
    keepers = [u for u in storage.read("users") if not u.get("demo_generated")]
    print(f"    {'users (real logins)':28} {len(keepers):>5} "
          f"({', '.join(u['username'] for u in keepers)})")
    print()

    if dry_run:
        print("Dry run -- nothing was changed.")
        return 0

    if not total:
        print("Nothing to remove; the store is already clear.")
        return 0

    if not assume_yes:
        answer = input(f"Delete {total} record(s)? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1

    for name in WIPE:
        storage.write(name, [])

    for user in storage.read("users"):
        if user.get("demo_generated"):
            storage.delete("users", user["id"])
            continue
        changes = dict(STALE_DUTY_FIELDS)
        if user.get("role") in COLLECTOR_ROLES:
            changes.update(STALE_COLLECTOR_FIELDS)
        storage.update("users", user["id"], changes, ACTOR)

    # Every unit is free again: the assignments that were holding them are gone.
    for unit in storage.read("vehicles"):
        if unit.get("status") != "available":
            storage.update("vehicles", unit["id"], {"status": "available"}, ACTOR)

    proofs = Path(Config.UPLOAD_DIR)
    if proofs.exists():
        shutil.rmtree(proofs, ignore_errors=True)
    proofs.mkdir(parents=True, exist_ok=True)

    print(f"Done. {total} record(s) removed.\n")
    print("  Sign in as city_admin and start with User Management, then")
    print("  Tricycle/Truck to assign routes, then register properties.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be removed, change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    args = parser.parse_args()
    return run(assume_yes=args.yes, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
