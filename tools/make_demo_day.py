"""
Fill the store with one believable day of activity, so every dashboard, table,
chart, and counter has something real to show.

    python tools/make_demo_day.py            # today
    python tools/make_demo_day.py --days 5   # today plus the four days before
    python tools/make_demo_day.py --reset     # clear generated activity first

Why this exists: a freshly seeded system is correct but empty -- every card
reads 0, every table shows its empty state, and the chart has nothing to draw.
That is the right behaviour for a new install and the wrong thing to put in
front of a capstone panel. This writes the records a working day would have
produced, through the same storage module the app uses, so nothing here is a
special case the real code has to know about.

It is additive and idempotent per date: a property that already has an entry
for a date is left alone, so running it twice does not double any total.

Everything it writes is ordinary data. Delete it from the UI, or run with
--reset, and the system is back to a clean seeded state.
"""

import argparse
import os
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.security import generate_password_hash

from config import Config
from seed import seed_password
from services import schedule_service, storage, timeutil

ACTOR = "tools/make_demo_day"

# Defined once, in seed.py, so this generator, seed.py and
# tools/set_demo_passwords.py can never disagree about what the password is.
# They used to: seed.py generated a random one while this fell back to a
# hardcoded literal, which left a deploy with two different passwords across
# its accounts.
DEMO_PASSWORD = seed_password()

# Barangays that get a full cast of properties and collectors. Ten is enough to
# make every citywide figure look like a city rather than a test fixture, and
# few enough that the generator stays quick on a JSON store.
ACTIVE_BARANGAYS = 10

# Truck routes: which barangay MRFs each truck covers. Four trucks over the
# first ten barangays, matching the "4 trucks, 31 MRFs" shape of the spec.
TRUCK_ROUTES = {
    "TRK-01": [1, 2, 3],
    "TRK-02": [4, 5, 6],
    "TRK-03": [7, 8],
    "TRK-04": [9, 10],
}

FIRST_NAMES = [
    "Nica", "Iliana", "Marites", "Joel", "Rowena", "Danilo", "Cristina",
    "Rodel", "Bernadette", "Arnel", "Lorna", "Edgar", "Michelle", "Ramon",
    "Grace", "Alfredo", "Divina", "Teodoro", "Jocelyn", "Ernesto",
]
LAST_NAMES = [
    "Abayon", "Dwane", "Ocampo", "Bautista", "Villaflor", "Cruz", "Mangubat",
    "Salazar", "Tabada", "Espina", "Baluyot", "Cabaltera", "Genson", "Lumbay",
    "Pacaldo", "Requilme", "Sabanal", "Ugmad", "Vallar", "Yamyamin",
]
ESTABLISHMENTS = [
    "Sari-Sari ni Aling {last}", "{last} Bakery", "{last} Carinderia",
    "{last} Boarding House", "{last} Hardware", "{last} Water Refilling",
]
TAGS = ["", "", "", "Composting", "None Composting", "Has Special Waste"]

REASONS = [
    "Not segregated properly",
    "No garbage taken out",
    "Road inaccessible",
    "Has special/hazardous waste",
    "No one at home",
]

COLLECTOR_NAMES = [
    "Joel Abadilla", "Rowena Barrios", "Danilo Cagampang", "Cristina Dagohoy",
    "Rodel Enriquez", "Bernadette Fabian", "Arnel Gomez", "Lorna Hilario",
    "Edgar Ibarra", "Michelle Jalandoni", "Ramon Kalaw", "Grace Lozada",
]
OPERATOR_NAMES = [
    "Alfredo Marasigan", "Divina Nazareno", "Teodoro Ochoa", "Jocelyn Padilla",
]


def barangay_id(n: int) -> str:
    return f"brgy-{n:02d}"


def barangay_number(bid: str) -> int | None:
    try:
        return int(str(bid).split("-")[-1])
    except (ValueError, AttributeError):
        return None


def active_numbers() -> list[int]:
    """
    Which barangays get activity: the first ACTIVE_BARANGAYS, plus whichever
    ones the accounts that already exist are assigned to.

    Without that second part, `seed.py --demo` puts brgy_admin and
    tri_collector in Poblacion 1 while this tool fills Antonio Luna through
    Del Pilar -- and the two accounts a panel is most likely to be shown log
    in to an empty dashboard.
    """
    numbers = set(range(1, ACTIVE_BARANGAYS + 1))
    for user in storage.find("users"):
        if user.get("role") not in ("barangay_admin", "tricycle_collector"):
            continue
        n = barangay_number(user.get("assigned_barangay"))
        if n:
            numbers.add(n)
    return sorted(numbers)


def existing_collector_for(bid: str) -> dict | None:
    """
    An active tricycle collector already assigned to this barangay.

    Reusing them matters: creating a second collector for a barangay that has
    one would hand out a tricycle code that is already taken, which the admin's
    own assign form refuses to do.
    """
    for assignment in storage.find("assignments_tricycle", barangay_id=bid):
        if assignment.get("status") not in ("Active", "Temporary Replacement"):
            continue
        user = storage.get("users", assignment.get("collector_id"))
        if user and user.get("status") == "Active":
            return user
    return None


# ---------------------------------------------------------------------------
# Cast: accounts, assignments, properties
# ---------------------------------------------------------------------------

def ensure_user(username: str, payload: dict) -> dict:
    existing = storage.find_one("users", username=username)
    if existing:
        return existing
    record = {
        "full_name": payload["full_name"],
        "username": username,
        "password_hash": generate_password_hash(DEMO_PASSWORD),
        "role": payload["role"],
        "assigned_barangay": payload.get("assigned_barangay"),
        "assigned_barangays": payload.get("assigned_barangays", []),
        "assigned_vehicle": payload.get("assigned_vehicle"),
        "assigned_purok": payload.get("assigned_purok"),
        "assigned_puroks": payload.get("assigned_puroks", []),
        "contact_number": payload.get("contact_number", ""),
        "status": "Active",
        "must_change_password": False,
        "duty_status": "Off Duty",
        "last_location": None,
        "demo_generated": True,
    }
    return storage.insert("users", record, ACTOR)


def build_cast(rng: random.Random) -> dict:
    """Accounts and assignments for the active barangays. Idempotent."""
    barangays = {b["id"]: b for b in storage.find("barangays")}
    cast = {"tricycles": [], "trucks": [], "barangay_admins": []}

    for n in active_numbers():
        bid = barangay_id(n)
        brgy = barangays.get(bid)
        if not brgy:
            continue
        puroks = [p for p in (brgy.get("puroks") or [])] or ["Purok 1"]

        existing_admin = next(
            (u for u in storage.find("users", role="barangay_admin",
                                     assigned_barangay=bid)
             if u.get("status") == "Active"), None)
        admin = existing_admin or ensure_user(f"brgy{n:02d}_admin", {
            "full_name": f"{brgy['name']} Barangay Admin",
            "role": "barangay_admin",
            "assigned_barangay": bid,
        })
        cast["barangay_admins"].append(admin)

        # One tricycle collector per barangay, covering the first three puroks.
        collector = existing_collector_for(bid)
        if not collector:
            collector = ensure_user(f"tri{n:02d}", {
                "full_name": COLLECTOR_NAMES[(n - 1) % len(COLLECTOR_NAMES)],
                "role": "tricycle_collector",
                "assigned_barangay": bid,
                "assigned_vehicle": f"TRI-{n:02d}",
                "assigned_purok": puroks[0],
                "assigned_puroks": puroks[:3],
                "contact_number": f"09{rng.randint(100000000, 999999999)}",
            })
            storage.insert("assignments_tricycle", {
                "collector_id": collector["id"],
                "barangay_id": bid,
                "purok_coverage": puroks[:3],
                "tricycle_code": f"TRI-{n:02d}",
                "status": "Active",
                "note": "",
                "demo_generated": True,
            }, ACTOR)
            _claim_vehicle(f"TRI-{n:02d}")
        cast["tricycles"].append(collector)

    active = set(active_numbers())
    extra = sorted(active - {n for ns in TRUCK_ROUTES.values() for n in ns})

    for i, (truck, numbers) in enumerate(TRUCK_ROUTES.items()):
        covered = [barangay_id(n) for n in numbers if n in active]
        # Any barangay outside the fixed routes still needs a truck, or its MRF
        # sits Pending forever and the barangay admin never sees a pickup.
        if i == 0:
            covered += [barangay_id(n) for n in extra]
        if not covered:
            continue
        operator = ensure_user(f"trk{i + 1:02d}", {
            "full_name": OPERATOR_NAMES[i % len(OPERATOR_NAMES)],
            "role": "truck_collector",
            "assigned_vehicle": truck,
            "assigned_barangays": covered,
            "contact_number": f"09{rng.randint(100000000, 999999999)}",
        })
        cast["trucks"].append(operator)

        if not storage.find_one("assignments_truck", operator_id=operator["id"]):
            storage.insert("assignments_truck", {
                "operator_id": operator["id"],
                "truck_code": truck,
                "covered_mrfs": covered,
                # Feeds the T-2h "the truck will arrive" reminder.
                "planned_pickup_times": {b: t for b, t in
                                         zip(covered, ["09:00", "10:30", "13:00"])},
                "status": "Active",
                "note": "",
                "demo_generated": True,
            }, ACTOR)
        _claim_vehicle(truck)

    return cast


def _claim_vehicle(code: str) -> None:
    unit = storage.find_one("vehicles", code=code)
    if unit and unit.get("status") == "available":
        storage.update("vehicles", unit["id"], {"status": "assigned"}, ACTOR)


def build_properties(rng: random.Random, per_barangay: int = 24) -> int:
    """Households and establishments, so the denominators stop reading '/ 8'."""
    made = 0
    barangays = {b["id"]: b for b in storage.find("barangays")}

    for n in active_numbers():
        bid = barangay_id(n)
        brgy = barangays.get(bid)
        if not brgy:
            continue
        have = len(storage.find("properties", barangay_id=bid))
        if have >= per_barangay:
            continue
        puroks = [p for p in (brgy.get("puroks") or [])][:3] or ["Purok 1"]

        for i in range(have, per_barangay):
            last = LAST_NAMES[(n * 7 + i) % len(LAST_NAMES)]
            if i % 6 == 5:
                kind = "Establishment"
                owner = ESTABLISHMENTS[i % len(ESTABLISHMENTS)].format(last=last)
            else:
                kind = "House"
                owner = f"{FIRST_NAMES[(n * 3 + i) % len(FIRST_NAMES)]} {last}"

            storage.insert("properties", {
                "owner_name": owner,
                "property_type": kind,
                "barangay_id": bid,
                "purok": puroks[i % len(puroks)],
                "tag": TAGS[(n + i) % len(TAGS)],
                "demo_generated": True,
            }, ACTOR)
            made += 1
    return made


# ---------------------------------------------------------------------------
# A day of activity
# ---------------------------------------------------------------------------

def waste_lines(rng: random.Random, day: str) -> list[dict]:
    """Quantities against the waste types actually scheduled for that day."""
    types = schedule_service.waste_types_for(day) or ["Mixed Waste"]
    lines = []
    for waste_type in types:
        if rng.random() < 0.25:
            continue
        unit = "Kilo" if "Yard" in waste_type or rng.random() < 0.2 else "Sack"
        qty = rng.randint(1, 6) if unit == "Sack" else rng.randint(2, 15)
        lines.append({"type": waste_type, "unit": unit, "qty": qty})
    if not lines:
        lines.append({"type": types[0], "unit": "Sack", "qty": rng.randint(1, 4)})
    return lines


def gps_near(rng: random.Random, bid: str) -> dict | None:
    """A point inside the barangay's rough area, from the MRF coordinates."""
    from services import geo_service

    point = geo_service.point_for_barangay(bid)
    if not point:
        return None
    lat, lng = point
    return {"lat": round(lat + rng.uniform(-.006, .006), 6),
            "lng": round(lng + rng.uniform(-.006, .006), 6)}


def collect_day(rng: random.Random, cast: dict, day: str,
                completion: float = .78) -> dict:
    """
    Tricycle entries for one date. `completion` is how far through their route
    the collectors got -- deliberately short of 1.0 so Pending is a real number
    on the dashboard and not a permanent zero.
    """
    stats = {"collected": 0, "not_collected": 0}
    schedule = schedule_service.for_date(day)

    for index, collector in enumerate(cast["tricycles"]):
        bid = collector.get("assigned_barangay")
        puroks = collector.get("assigned_puroks") or []
        properties = [p for p in storage.find("properties", barangay_id=bid)
                      if not puroks or p.get("purok") in puroks]
        rng.shuffle(properties)
        worked = properties[:int(len(properties) * completion)]

        for i, prop in enumerate(worked):
            if storage.find_one("collections", property_id=prop["id"], date=day):
                continue
            refused = rng.random() < .12
            # Stagger each collector's start and pace, or every barangay's
            # stops land on the same minute and the activity feed reads as
            # generated rather than worked.
            start = 6 * 60 + 20 + index * 7 + rng.randint(0, 12)
            elapsed = start + i * rng.randint(7, 14)
            hour, minute = divmod(elapsed, 60)
            storage.insert("collections", {
                "property_id": prop["id"],
                "barangay_id": bid,
                "purok": prop.get("purok"),
                "date": day,
                "status": "Not Collected" if refused else "Collected",
                "collector_id": collector["id"],
                "tricycle_code": collector.get("assigned_vehicle"),
                "gps": gps_near(rng, bid),
                "timestamp": f"{day}T{hour:02d}:{minute:02d}:00+08:00",
                "waste": [] if refused else waste_lines(rng, day),
                "reason": rng.choice(REASONS) if refused else "",
                # No photo is invented: a not-collected entry made through the
                # app requires one, and a fake path would render a broken image
                # in the admin's View modal.
                "image_proof_path": None,
                "note": "",
                "source": "collector",
                "disputed": False,
                "schedule_day": timeutil.weekday_name(day),
                "waste_type": schedule.get("short"),
                "demo_generated": True,
            }, collector["id"])
            stats["not_collected" if refused else "collected"] += 1

    return stats


def pickup_day(rng: random.Random, cast: dict, day: str) -> dict:
    """
    Truck pickups at the MRFs, one missed on purpose so the Carry-Over page and
    the Missed Pickup counter have a real record behind them.
    """
    from services import carryover_service, mrf_service

    stats = {"collected": 0, "missed": 0, "delivered": 0}
    missed_once = False

    for operator in cast["trucks"]:
        assignment = storage.find_one("assignments_truck", operator_id=operator["id"])
        if not assignment:
            continue
        picked = []

        for bid in assignment.get("covered_mrfs", []):
            if storage.find_one("mrf_pickups", barangay_id=bid, date=day):
                continue
            card = mrf_service.mrf_card(bid, day)
            if card["load"].get("empty") and rng.random() < .5:
                continue   # nothing waiting in that MRF, so nothing to pick up

            miss = not missed_once and rng.random() < .5
            record = storage.insert("mrf_pickups", {
                "barangay_id": bid,
                "date": day,
                "source_schedule_day": card["source_schedule_day"],
                "waste_type": card["waste_type"],
                "load": {"lines": [], "sacks": 0, "kilos": 0, "total": "0",
                         "empty": True} if miss else card["load"],
                "covered_dates": card["covered_dates"],
                "truck_code": assignment.get("truck_code"),
                "operator_id": operator["id"],
                "status": "Not Collected" if miss else "Collected from MRF",
                "gps": gps_near(rng, bid),
                "timestamp": f"{day}T{rng.randint(9, 15):02d}:{rng.randint(0, 59):02d}:00+08:00",
                "reason": "MRF was locked / no attendant" if miss else "",
                "note": "",
                "delivery_id": None,
                "auto_missed": False,
                "demo_generated": True,
            }, operator["id"])

            if miss:
                missed_once = True
                stats["missed"] += 1
                carryover_service.open_for(record, actor=operator["id"])
            else:
                stats["collected"] += 1
                picked.append(record)
                carryover_service.close_for(bid, record, actor=operator["id"])

        # One landfill run per truck that actually picked anything up.
        if picked:
            load = mrf_service.running_load(operator["id"], day)
            if not load.get("empty"):
                delivery = storage.insert("deliveries", {
                    "truck_code": assignment.get("truck_code"),
                    "operator_id": operator["id"],
                    "date": day,
                    "timestamp": f"{day}T16:{rng.randint(0, 59):02d}:00+08:00",
                    "mrfs_included": [p["barangay_id"] for p in picked],
                    "source_schedule_day": picked[0].get("source_schedule_day"),
                    "waste_type": picked[0].get("waste_type"),
                    "load": load,
                    "gps": gps_near(rng, picked[0]["barangay_id"]),
                    "demo_generated": True,
                }, operator["id"])
                for p in picked:
                    storage.update("mrf_pickups", p["id"],
                                   {"delivery_id": delivery["id"]}, operator["id"])
                stats["delivered"] += 1

    return stats


def resident_reports(rng: random.Random, day: str, count: int = 4) -> int:
    """A few anonymous reports, including one that disputes a Collected entry."""
    made = 0
    for _ in range(count):
        bid = barangay_id(rng.choice(active_numbers()))
        properties = storage.find("properties", barangay_id=bid)
        if not properties:
            continue
        prop = rng.choice(properties)
        storage.insert("public_reports", {
            "barangay_id": bid,
            "purok": prop.get("purok"),
            "property_id": prop["id"],
            "status_reported": "Not Collected",
            "comment": rng.choice([
                "Wala pa nakolekta hangtod karon.",
                "The tricycle passed but did not stop.",
                "Nobody collected our garbage this morning.",
                "Still waiting since 7 AM.",
            ]),
            "device_fingerprint": f"demo-{rng.randint(1000, 9999)}",
            "date": day,
            "demo_generated": True,
        }, "public")
        made += 1

        # A resident contradicting a collector is a case the spec calls out, so
        # leave one on the board for the barangay admin to review.
        entry = storage.find_one("collections", property_id=prop["id"], date=day)
        if entry and entry.get("status") == "Collected" and not entry.get("disputed"):
            storage.update("collections", entry["id"], {"disputed": True}, "public")
    return made


def put_collectors_on_duty(rng: random.Random, cast: dict, keep: int = 7) -> int:
    """
    Park a handful of vehicles on the map, so Live Tracking is not an empty
    frame. Positions are inside their own barangay.
    """
    on = 0
    for collector in cast["tricycles"][:keep]:
        point = gps_near(rng, collector.get("assigned_barangay"))
        if not point:
            continue
        storage.update("users", collector["id"], {
            "duty_status": "On Duty",
            "duty_changed_at": timeutil.stamp(),
            "last_location": {**point, "at": timeutil.stamp()},
        }, ACTOR)
        on += 1

    for operator in cast["trucks"][:2]:
        covered = operator.get("assigned_barangays") or []
        point = gps_near(rng, covered[0]) if covered else None
        if not point:
            continue
        storage.update("users", operator["id"], {
            "duty_status": "On Duty",
            "duty_changed_at": timeutil.stamp(),
            "last_location": {**point, "at": timeutil.stamp()},
        }, ACTOR)
        on += 1
    return on


def unavailable_requests(rng: random.Random, cast: dict, day: str) -> int:
    """Two open requests, so the admin's reassignment counters are not zero."""
    made = 0
    for user in (cast["tricycles"][-1:] + cast["trucks"][-1:]):
        if storage.find_one("unavailable_requests", user_id=user["id"], status="Pending"):
            continue
        storage.insert("unavailable_requests", {
            "user_id": user["id"],
            "role": user["role"],
            "affected_date": day,
            "unavailable_until": "",
            "reason": rng.choice(["Sick", "Family emergency", "Vehicle repair"]),
            "notes": "",
            "status": "Pending",
            "demo_generated": True,
        }, user["id"])
        made += 1
    return made


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

GENERATED = ["collections", "mrf_pickups", "deliveries", "carry_overs",
             "public_reports", "unavailable_requests", "notifications",
             "properties", "assignments_tricycle", "assignments_truck",
             "users", "history"]


def reset() -> dict:
    """Remove everything this tool created, leaving the seeded base intact."""
    removed = {}

    def purge(collection: str, keep) -> None:
        gone = 0
        for record in list(storage.find(collection)):
            if not keep(record):
                storage.delete(collection, record["id"])
                gone += 1
        if gone:
            removed[collection] = gone

    demo_users = {u["id"] for u in storage.find("users") if u.get("demo_generated")}

    def is_generated(record: dict) -> bool:
        return bool(record.get("demo_generated")
                    or record.get("created_by") == ACTOR
                    or record.get("created_by") in demo_users)

    for collection in GENERATED:
        purge(collection, lambda r: not is_generated(r))

    # Carry-overs and their notifications are written by the services, not by
    # this tool, so they carry no marker of their own. What identifies them is
    # that the pickup they point at is now gone -- a dangling row the admin's
    # Carry-Over page would otherwise show forever.
    pickups = {p["id"] for p in storage.find("mrf_pickups")}
    purge("carry_overs", lambda r: r.get("last_pickup_id") in pickups)

    for unit in storage.find("vehicles"):
        if unit.get("status") == "assigned":
            storage.update("vehicles", unit["id"], {"status": "available"}, ACTOR)
    return removed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3,
                        help="how many days back to fill, today included")
    parser.add_argument("--reset", action="store_true",
                        help="delete generated activity, then stop")
    parser.add_argument("--seed", type=int, default=20260820,
                        help="random seed, so a demo is reproducible")
    args = parser.parse_args()

    storage.bootstrap()

    if args.reset:
        removed = reset()
        if removed:
            for name, count in removed.items():
                print(f"  removed {count:>4} from {name}")
        else:
            print("  nothing generated to remove")
        return 0

    rng = random.Random(args.seed)
    print(f"Filling {Config.CITY_NAME} with demo activity "
          f"({len(active_numbers())} barangays, {args.days} day(s))")

    cast = build_cast(rng)
    print(f"  accounts        {len(cast['tricycles'])} tricycle collectors, "
          f"{len(cast['trucks'])} truck operators, "
          f"{len(cast['barangay_admins'])} barangay admins")

    made = build_properties(rng)
    total = len(storage.find("properties"))
    print(f"  properties      {made} created, {total} registered in total")

    today = timeutil.today()
    for offset in range(args.days - 1, -1, -1):
        day = timeutil.date_str(today - timedelta(days=offset))
        if not schedule_service.is_collection_day(day):
            print(f"  {day}      no collection scheduled, skipped")
            continue
        # Past days finish; today is deliberately still in progress.
        c = collect_day(rng, cast, day, completion=.78 if offset == 0 else .96)
        m = pickup_day(rng, cast, day)
        r = resident_reports(rng, day, count=4 if offset == 0 else 2)
        print(f"  {day}      {c['collected']} collected, "
              f"{c['not_collected']} refused, {m['collected']} MRFs picked up, "
              f"{m['missed']} missed, {m['delivered']} landfill runs, "
              f"{r} resident reports")

    day = timeutil.date_str(today)
    on_duty = put_collectors_on_duty(rng, cast)
    requests = unavailable_requests(rng, cast, day)
    print(f"  live map        {on_duty} vehicles on duty")
    print(f"  unavailable     {requests} open request(s)")

    if os.environ.get("SEED_DEMO_PASSWORD"):
        print("")
        print("Done. Accounts use the password in SEED_DEMO_PASSWORD.")
    else:
        print(f"\nDone. Every account's password is: {DEMO_PASSWORD}")
    print("Undo with: python tools/make_demo_day.py --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
