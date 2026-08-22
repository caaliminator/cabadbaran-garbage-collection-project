"""
Set a known password on the demo/admin accounts.

    python tools/set_demo_passwords.py                  # uses DEMO_PASSWORD below
    python tools/set_demo_passwords.py MyOwnPassword    # or pass your own

Only for the seeded walkthrough accounts. Real accounts get their passwords
from the City Hall Admin's User Management page; this exists so the capstone
demo has logins you can hand to a panel without digging through seed output.
Stored hashed, exactly like every other password in the system.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.security import generate_password_hash
from seed import seed_password
from services import storage

# Same password seed.py and make_demo_day.py use, so running this can only
# restore the documented credentials -- never invent a third set.
DEMO_PASSWORD = seed_password()

# The accounts seed.py creates. Accounts created by make_demo_day.py are found
# via their `demo_generated` flag, so this covers the whole demo cast without
# ever touching a real account made through User Management.
ACCOUNTS = ["city_admin", "brgy_admin", "tri_collector", "truck_collector"]


def main() -> int:
    password = sys.argv[1] if len(sys.argv) > 1 else DEMO_PASSWORD
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return 1

    storage.bootstrap()

    targets = list(ACCOUNTS) + sorted(
        u["username"] for u in storage.find("users")
        if u.get("demo_generated") and u["username"] not in ACCOUNTS)

    changed, missing = [], []
    for username in targets:
        user = storage.find_one("users", username=username)
        if not user:
            missing.append(username)
            continue
        storage.update("users", user["id"], {
            "password_hash": generate_password_hash(password),
            "must_change_password": False,
            "status": "Active",
        }, "tools/set_demo_passwords")
        changed.append(username)

    for username in changed:
        print(f"  {username:<18} {password}")
    if missing:
        print(f"\n  not found (run `python seed.py --demo` first): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
