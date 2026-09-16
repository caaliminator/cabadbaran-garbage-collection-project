"""
Clear the demo records and leave a store you can put real data into.

    python tools/reset_data.py --dry-run   # say what would go, change nothing
    python tools/reset_data.py             # do it, after a confirmation
    python tools/reset_data.py --yes       # do it without asking

What counts as a transactional record is defined once, in
`services/reset_service.py`. This tool and the City Hall Admin's Danger Zone
both call it, so the two can never drift into removing different things --
read that module for the full what-goes / what-stays.

In short: every operational record and the demo-generated accounts go; the
real logins, the barangays (with their puroks), the vehicle registry and the
weekly schedule stay. Geo files under data/geo/ are left alone.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from services import reset_service, storage


def run(assume_yes: bool = False, dry_run: bool = False) -> int:
    storage.bootstrap()
    counts = reset_service.plan()
    total = sum(counts.values())

    print(f"Cabadbaran City data store: {Config.DATA_DIR}")
    print()
    print("  Would remove:" if dry_run else "  Removing:")
    for name, count in counts.items():
        print(f"    {name:28} {count:>5}")
    print()
    print("  Keeping:")
    for name, count in reset_service.kept().items():
        print(f"    {name:28} {count:>5}")
    keepers = reset_service.kept_accounts()
    print(f"    {'':28} ({', '.join(u['username'] for u in keepers)})")
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

    reset_service.run()

    print(f"Done. {total} record(s) removed.")
    print()
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
