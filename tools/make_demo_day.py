"""
Fill the store with the weeks a working system would have behind it, so the
system is presented mid-life rather than on its first morning.

    python tools/make_demo_day.py              # the last three weeks, to today
    python tools/make_demo_day.py --days 14    # a shorter history
    python tools/make_demo_day.py --reset      # clear generated activity first

Why this exists: a freshly seeded system is correct but empty -- every card
reads 0, every table shows its empty state, and the chart has nothing to draw.
That is the right behaviour for a new install and the wrong thing to put in
front of a capstone panel.

What it tells, day by day, in the order it would have happened:

  * a few days before go-live, City Hall sets up the accounts and the
    barangays register their households -- so every "Date Created" is a real
    spread of dates, not one minute at boot;
  * from go-live, every scheduled collection day: the tricycles work their
    rounds under that day's waste type, the trucks collect the MRFs, some
    stops are missed, residents report, disputes are raised and settled, and
    a collector is off sick now and then;
  * missed MRF loads become carry-overs that City Hall arranges and a truck
    collects on a later day;
  * each finished day is closed and frozen into History, as the app does at
    midnight, and a few households register mid-way;
  * today is left in progress.

Every date is counted back from today, so it works the same on each Render
boot, and the random seed is fixed, so the same demo comes out each time.

It writes through the same storage module the app uses, so nothing here is a
special case the real code has to know about, and it is idempotent: running it
twice on the same day adds nothing. Run with --reset and the system is back
to a clean seeded state.
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
from seed import SEED_ACTOR, seed_password
from services import schedule_service, storage, timeutil

ACTOR = "tools/make_demo_day"

# How much history the demo has behind it: three weeks, today included.
DEFAULT_DAYS = 21


# ---------------------------------------------------------------------------
# Time and bulk writes
# ---------------------------------------------------------------------------

def stamp_at(day, hour: int, minute: int = 0) -> str:
    """An audit timestamp on a given day, in Manila time like every other."""
    hour, minute = min(max(hour, 0), 23), min(max(minute, 0), 59)
    return f"{timeutil.date_str(day)}T{hour:02d}:{minute:02d}:00+08:00"


def set_created(name: str, stamps: dict) -> None:
    """
    Give records the date they were really created, in one write.

    Straight through a transaction rather than storage.update, which would
    stamp `updated_at` as now and make every backdated record look edited at
    boot.
    """
    if not stamps:
        return
    with storage.transaction(name) as rows:
        for row in rows:
            if row.get("id") in stamps:
                row["created_at"] = stamps[row["id"]]


def insert_many(name: str, records: list, actor) -> list:
    """
    A batch in one write (storage.insert_many). storage.insert rewrites the
    whole file per record, and a day of a city's collections is hundreds of
    them -- weeks of that one at a time took minutes, and Render runs this on
    every boot.
    """
    return storage.insert_many(name, records, actor)

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
        planned = {b: t for b, t in zip(covered, ["09:00", "10:30", "13:00", "14:30"])}

        # A truck that already has an operator -- seed.py --demo gives TRK-01
        # to the truck_collector login -- keeps that operator and takes the
        # route, rather than a second operator being made for the same truck.
        # Two active assignments on one truck, with overlapping MRFs, was what
        # this used to produce.
        from services import assignment_service
        existing = next((a for a in storage.find("assignments_truck", truck_code=truck)
                         if assignment_service.is_active(a)), None)
        held_by = storage.get("users", existing["operator_id"]) if existing else None
        if held_by:
            storage.update("assignments_truck", existing["id"],
                           {"covered_mrfs": covered, "planned_pickup_times": planned}, ACTOR)
            storage.update("users", held_by["id"], {"assigned_barangays": covered}, ACTOR)
            cast["trucks"].append(storage.get("users", held_by["id"]))
            _claim_vehicle(truck)
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


def plan_properties(rng: random.Random, go_live, days: int,
                    per_barangay: int = 24) -> tuple[int, dict]:
    """
    Households and establishments, registered the way a barangay would.

    Most are entered in the registration drive over the few days before
    go-live; the rest -- one in eight -- are new households that register
    part-way through. Those are returned by the day they register, for the
    day loop to add on that day, so no earlier day counts a household that
    did not exist yet.

    Returns (how many were registered now, {date: [records]} for later).
    """
    barangays = {b["id"]: b for b in storage.find("barangays")}
    now, later = [], {}

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

            if i % 8 == 7 and days > 4:
                registered = go_live + timedelta(days=rng.randint(2, days - 2))
            else:
                registered = go_live - timedelta(days=rng.randint(1, 3))
            record = {
                "owner_name": owner,
                # "type", the field the app reads -- this used to write
                # "property_type", which left every demo household untyped.
                "type": kind,
                "barangay_id": bid,
                "purok": puroks[i % len(puroks)],
                "tag": TAGS[(n + i) % len(TAGS)],
                "created_at": stamp_at(registered, rng.randint(8, 16), rng.randint(0, 59)),
                "demo_generated": True,
            }
            if registered <= go_live:
                now.append(record)
            else:
                later.setdefault(timeutil.date_str(registered), []).append(record)

    insert_many("properties", now, ACTOR)
    return len(now), later


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
                completion: float = .78, absent: set | None = None) -> dict:
    """
    Tricycle entries for one date. `completion` is roughly how far through
    their route the collectors got -- deliberately short of 1.0 so Pending is
    a real number on the dashboard -- and each collector's own figure varies
    around it, because no two rounds finish the same. A collector in `absent`
    was off that day on an approved request and records nothing.

    One batch per day: every property's entry for the date is written in a
    single store write (see insert_many).
    """
    stats = {"collected": 0, "not_collected": 0}
    schedule = schedule_service.for_date(day)
    absent = absent or set()
    done = {e["property_id"] for e in storage.find("collections", date=day)}
    batch = []
    # Refusal rates move a little from day to day too: a rainy morning, a
    # purok that forgot to segregate.
    refusal = rng.uniform(.06, .12)

    for index, collector in enumerate(cast["tricycles"]):
        if collector["id"] in absent:
            continue
        bid = collector.get("assigned_barangay")
        puroks = collector.get("assigned_puroks") or []
        # Only households already registered by that day.
        properties = [p for p in storage.find("properties", barangay_id=bid)
                      if (not puroks or p.get("purok") in puroks)
                      and (p.get("created_at") or "")[:10] <= day]
        rng.shuffle(properties)
        own = min(1.0, max(.4, completion + rng.uniform(-.06, .04)))
        worked = properties[:int(len(properties) * own)]

        for i, prop in enumerate(worked):
            if prop["id"] in done:
                continue
            refused = rng.random() < refusal
            # Stagger each collector's start and pace, or every barangay's
            # stops land on the same minute and the activity feed reads as
            # generated rather than worked.
            start = 6 * 60 + 20 + index * 7 + rng.randint(0, 12)
            elapsed = start + i * rng.randint(7, 14)
            hour, minute = divmod(elapsed, 60)
            stamp = f"{day}T{hour:02d}:{minute:02d}:00+08:00"
            batch.append({
                "created_at": stamp,
                "created_by": collector["id"],
                "property_id": prop["id"],
                "barangay_id": bid,
                "purok": prop.get("purok"),
                "date": day,
                "status": "Not Collected" if refused else "Collected",
                "collector_id": collector["id"],
                "tricycle_code": collector.get("assigned_vehicle"),
                "gps": gps_near(rng, bid),
                "timestamp": stamp,
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
            })
            stats["not_collected" if refused else "collected"] += 1

    insert_many("collections", batch, ACTOR)
    return stats


def arrange_carry_overs(rng: random.Random, cast: dict, day: str,
                        share: float = .6) -> int:
    """
    The City Hall Admin's morning, simulated: some of the carry-overs still in
    Missed Collection get a truck and today's date, which puts them on that
    truck's Carry-Over Stops list for the day.

    Written through carryover_service's own restage step rather than
    reschedule(), because reschedule() rightly refuses a date in the past and
    this is filling in past days.
    """
    from services import carryover_service

    admin = storage.find_one("users", role="city_admin")
    trucks = [storage.find_one("assignments_truck", operator_id=o["id"])
              for o in cast["trucks"]]
    trucks = [t for t in trucks if t and t.get("truck_code")]
    if not admin or not trucks:
        return 0

    arranged = 0
    for row in storage.read("carry_overs"):
        if carryover_service.stage_of(row) != carryover_service.MISSED:
            continue
        if (row.get("batch_date") or "") >= day or rng.random() > share:
            continue
        # Usually the truck that covers that MRF; sometimes another one, which
        # is the rescue the Carry-Over page exists for.
        own = [t for t in trucks if row["barangay_id"] in (t.get("covered_mrfs") or [])]
        truck = (own[0] if own and rng.random() < .6 else rng.choice(trucks))
        carryover_service._restage(row, {"current_truck": truck["truck_code"],
                                         "reschedule_date": day}, admin["id"])
        arranged += 1
    return arranged


def pickup_day(rng: random.Random, cast: dict, day: str,
               misses: int = 2, collect_carry_overs: bool = True) -> dict:
    """
    One day of truck work: the day's regular stops, each holding that day's
    batch only, plus any carry-over stops arranged for the day. Some regular
    stops are missed on purpose, so the Carry-Over page and the Missed Pickup
    counter have real records behind them.
    """
    from services import carryover_service, mrf_service, triggers

    stats = {"collected": 0, "missed": 0, "delivered": 0, "carried": 0}
    missed_today = 0
    MISSES_PER_DAY = misses

    for operator in cast["trucks"]:
        assignment = storage.find_one("assignments_truck", operator_id=operator["id"])
        if not assignment:
            continue
        picked = []

        # Carry-over stops first: an earlier day's load, arranged for today.
        # Left Pending on the live day so the truck page has some to show.
        if collect_carry_overs:
            for row in carryover_service.pending_for_truck(assignment.get("truck_code"), day):
                card = mrf_service.carry_over_card(row, day)
                record = storage.insert("mrf_pickups", {
                    "barangay_id": row["barangay_id"], "date": day,
                    "kind": mrf_service.CARRY_OVER, "carry_over_id": row["id"],
                    "source_schedule_day": card["source_schedule_day"],
                    "waste_type": card["waste_type"], "load": card["load"],
                    "covered_dates": card["covered_dates"],
                    "truck_code": assignment.get("truck_code"),
                    "operator_id": operator["id"],
                    "status": mrf_service.COLLECTED,
                    "gps": gps_near(rng, row["barangay_id"]),
                    "timestamp": (_t := stamp_at(day, rng.randint(8, 10), rng.randint(0, 59))),
                    "created_at": _t,
                    "reason": "", "note": "", "delivery_id": None,
                    "auto_missed": False, "demo_generated": True,
                }, operator["id"])
                carryover_service.close(row, record, actor=operator["id"])
                picked.append(record)
                stats["carried"] += 1

        for bid in assignment.get("covered_mrfs", []):
            if mrf_service.regular_pickup(bid, day):
                continue
            card = mrf_service.mrf_card(bid, day)
            if card["load"].get("empty") and rng.random() < .5:
                continue   # nothing came in today, so nothing to pick up

            # Today's misses are what the Carry-Over page's Missed Collection
            # tab shows, so today leans a little harder on them.
            chance = .5 if day == timeutil.today_str() else .35
            miss = missed_today < MISSES_PER_DAY and rng.random() < chance
            record = storage.insert("mrf_pickups", {
                "barangay_id": bid,
                "date": day,
                "kind": mrf_service.REGULAR,
                "carry_over_id": None,
                "source_schedule_day": card["source_schedule_day"],
                "waste_type": card["waste_type"],
                "load": mrf_service.EMPTY_LOAD if miss else card["load"],
                "covered_dates": card["covered_dates"],
                "truck_code": assignment.get("truck_code"),
                "operator_id": operator["id"],
                "status": "Not Collected" if miss else "Collected from MRF",
                "gps": gps_near(rng, bid),
                "timestamp": (_t := stamp_at(day, rng.randint(9, 15), rng.randint(0, 59))),
                "created_at": _t,
                "reason": "MRF was locked / no attendant" if miss else "",
                "note": "",
                "delivery_id": None,
                "auto_missed": False,
                "demo_generated": True,
            }, operator["id"])

            # Today's pickups raise the same alerts a live one would, so the
            # barangay's tricycle collectors and admin open a bell that says
            # what the truck did. Past days stay quiet: a demo's bell full of
            # last week's news is not what a real morning looks like.
            live = day == timeutil.today_str()
            if miss:
                missed_today += 1
                stats["missed"] += 1
                opened = carryover_service.open_for(record, card["load"],
                                                    actor=operator["id"])
                if live:
                    triggers.on_carry_over_created(opened, record)
            else:
                stats["collected"] += 1
                picked.append(record)
                if live:
                    triggers.on_mrf_collected(record)

        # One landfill run per truck that actually picked anything up.
        if picked:
            load = mrf_service.running_load(operator["id"], day)
            if not load.get("empty"):
                delivery = storage.insert("deliveries", {
                    "truck_code": assignment.get("truck_code"),
                    "operator_id": operator["id"],
                    "date": day,
                    "timestamp": (_t := stamp_at(day, 16, rng.randint(0, 59))),
                    "created_at": _t,
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


def stage_carry_overs(rng: random.Random, cast: dict) -> dict:
    """
    Walk some of the open carry-overs through the stages an admin would.

    A carry-over is Missed until somebody gives it both a truck and a date;
    with both it is Pending; once the truck actually collects it is Collected.
    The first and last of those fall out of the day generation on their own.
    Pending does not -- it is the product of two admin decisions -- so it is
    made here, through carryover_service, exactly as the Carry-Over page would.

    Two rows are deliberately left short of Pending: one with nothing set and
    one with a truck but no date. They are what puts a real row behind the
    "Needs a collection date" note, which is otherwise a feature nobody can see
    until the city misses a pickup in front of the panel.
    """
    from services import carryover_service, vehicle_service

    admin = storage.find_one("users", role="city_admin")
    if not admin:
        return {"pending": 0, "missed": 0}

    trucks = sorted(vehicle_service.in_service(vehicle_service.TRUCK))
    open_rows = [r for r in storage.read("carry_overs")
                 if carryover_service.stage_of(r) == carryover_service.MISSED]
    if not trucks or not open_rows:
        return {"pending": 0, "missed": len(open_rows)}

    rng.shuffle(open_rows)
    # Keep up to three behind, and never stage them all away -- a Missed
    # Collection tab left empty is the exact case this exists to avoid, and
    # the old max(0, n - 1) arranged the last one whenever only one was left.
    hold_back = min(3, len(open_rows))
    to_stage, left = open_rows[:len(open_rows) - hold_back], open_rows[len(open_rows) - hold_back:]

    tomorrow = timeutil.date_str(timeutil.today() + timedelta(days=1))
    # The first goes to a truck that has an operator, for today, so a truck's
    # page opens with a live Carry-Over Stops section rather than only ever
    # hearing about them in the future.
    operated = [a["truck_code"] for a in storage.read("assignments_truck")
                if a.get("operator_id") and a.get("truck_code") in trucks]
    # Oldest first, so today's live stop is a genuinely earlier day's load.
    to_stage.sort(key=lambda r: r.get("batch_date") or "")
    pending = 0
    for index, row in enumerate(to_stage):
        today_stop = index == 0 and operated
        carryover_service.reassign(
            row["id"], rng.choice(operated) if today_stop else rng.choice(trucks),
            admin["id"])
        carryover_service.reschedule(
            row["id"], timeutil.today_str() if today_stop else tomorrow, admin["id"])
        pending += 1

    # The second one held back gets a truck and no date, so the incomplete
    # case is on screen rather than only reachable by hand.
    if len(left) > 1:
        carryover_service.reassign(left[1]["id"], rng.choice(trucks), admin["id"])

    return {"pending": pending, "missed": len(left)}


def resident_reports(rng: random.Random, day: str, count: int = 4) -> int:
    """A few anonymous reports, including one that disputes a Collected entry."""
    made = 0
    entries = {e["property_id"]: e for e in storage.find("collections", date=day)}
    disputed = set()
    reports = []
    for _ in range(count):
        bid = barangay_id(rng.choice(active_numbers()))
        properties = [p for p in storage.find("properties", barangay_id=bid)
                      if (p.get("created_at") or "")[:10] <= day]
        if not properties:
            continue
        prop = rng.choice(properties)
        reports.append({
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
            "created_at": stamp_at(day, rng.randint(8, 17), rng.randint(0, 59)),
            "demo_generated": True,
        })
        made += 1

        # A resident contradicting a collector is a case the spec calls out, so
        # leave one on the board for the barangay admin to review.
        entry = entries.get(prop["id"])
        if entry and entry.get("status") == "Collected" and not entry.get("disputed"):
            disputed.add(entry["id"])

    insert_many("public_reports", reports, "public")
    # Flagged in one write of the collections file, not one per report.
    if disputed:
        with storage.transaction("collections") as rows:
            for row in rows:
                if row.get("id") in disputed:
                    row["disputed"] = True
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
# The weeks before today
# ---------------------------------------------------------------------------

def backdate_setup(rng: random.Random, go_live) -> int:
    """
    Accounts and assignments get the days City Hall really set them up on:
    the City Hall Admin a week before go-live, the barangay admins a few days
    after that, the collectors and truck operators last, each at a working
    hour. Only records made in this run -- demo ones, and the seeded accounts
    that seed.py created at boot -- are moved; anything older already has a
    real date and keeps it.
    """
    today = timeutil.today_str()
    offsets = {"city_admin": 7, "barangay_admin": 5, "tricycle_collector": 3,
               "truck_collector": 3}

    def fresh(row):
        return ((row.get("created_at") or "")[:10] == today
                and (row.get("demo_generated") or row.get("created_by") in (SEED_ACTOR, ACTOR)))

    stamps, joined = {}, {}
    for user in storage.read("users"):
        if not fresh(user):
            continue
        back = offsets.get(user.get("role"), 4) - rng.randint(0, 1)
        stamps[user["id"]] = stamp_at(go_live - timedelta(days=back),
                                      rng.randint(8, 16), rng.randint(0, 59))
        joined[user["id"]] = stamps[user["id"]]
    set_created("users", stamps)

    for name, who in (("assignments_tricycle", "collector_id"),
                      ("assignments_truck", "operator_id")):
        rows = {}
        for row in storage.read(name):
            if fresh(row):
                # Assigned the day the account was made, an hour later.
                base = joined.get(row.get(who)) or stamp_at(go_live - timedelta(days=2), 10)
                rows[row["id"]] = base[:11] + f"{min(int(base[11:13]) + 1, 23):02d}" + base[13:]
        set_created(name, rows)
        with storage.transaction(name) as all_rows:
            for row in all_rows:
                if row["id"] in rows and not row.get("effective_date"):
                    row["effective_date"] = timeutil.date_str(go_live)

    # The seeded demo households came from the same registration drive.
    set_created("properties", {
        row["id"]: stamp_at(go_live - timedelta(days=rng.randint(1, 3)),
                            rng.randint(8, 16), rng.randint(0, 59))
        for row in storage.read("properties") if fresh(row)})
    return len(stamps)


def past_leave(rng: random.Random, cast: dict, go_live, days: int) -> dict:
    """
    A few unavailability requests across the weeks, decided by City Hall the
    way the Unavailability page records them. An approved day off is a day
    that collector did not work, so it is returned as {date: {collector ids}}
    for the day loop to leave their round untouched.
    """
    admin = storage.find_one("users", role="city_admin")
    absent: dict = {}
    if not admin or days < 8:
        return absent
    # Built once. The dates come from the shared random sequence, which a
    # second run walks differently, so "is this one there already" could not
    # recognise them and every re-run added four more.
    if storage.find_one("unavailable_requests",
                        lambda r: r.get("demo_generated") and r.get("status") != "Pending"):
        return absent
    stories = [
        (cast["tricycles"][1:2], "Sick", "Approved", "Fever. Will be back the day after."),
        (cast["trucks"][2:3], "Vehicle repair", "Approved", "Clutch replaced at the city motor pool."),
        (cast["tricycles"][4:5], "Family emergency", "Rejected",
         "Please arrange a swap with the Purok 2 collector instead."),
        (cast["tricycles"][6:7], "Sick", "Approved", ""),
    ]
    for (who, reason, decision, note), when in zip(
            stories, sorted(rng.sample(range(3, days - 2), min(4, days - 5)))):
        if not who:
            continue
        user = who[0]
        day = go_live + timedelta(days=when)
        if storage.find_one("unavailable_requests", user_id=user["id"],
                            affected_date=timeutil.date_str(day)):
            continue
        asked = stamp_at(day - timedelta(days=1), rng.randint(17, 20), rng.randint(0, 59))
        decided = stamp_at(day - timedelta(days=1), 21, rng.randint(0, 59))
        storage.insert("unavailable_requests", {
            "user_id": user["id"], "role": user["role"],
            "affected_date": timeutil.date_str(day), "unavailable_until": "",
            "reason": reason, "notes": "", "status": decision,
            "decided_by": admin["id"], "decided_at": decided,
            "decision_note": note, "resolved_at": decided,
            "created_at": asked, "demo_generated": True,
        }, user["id"])
        if decision == "Approved" and user["role"] == "tricycle_collector":
            absent.setdefault(timeutil.date_str(day), set()).add(user["id"])
    return absent


def settle_disputes(rng: random.Random, day: str) -> int:
    """
    The barangay admins' review, the morning after: most disputes raised on
    earlier days are settled -- usually in the collector's favour, sometimes
    the resident's -- and a few are left open, as a real backlog would be.
    """
    # The same outcome public_report_service.resolve_dispute records, applied
    # in one write of the collections file for the whole morning's review
    # rather than one full rewrite per dispute.
    admins = {u.get("assigned_barangay"): u["id"]
              for u in storage.find("users", role="barangay_admin")}
    settled = 0
    with storage.transaction("collections") as rows:
        for entry in rows:
            if not entry.get("disputed") or (entry.get("date") or "") >= day:
                continue
            if rng.random() > .7:
                continue
            by = admins.get(entry.get("barangay_id")) or ACTOR
            if rng.random() < .75:
                entry.update({"disputed": False,
                              "dispute_resolution": "Collector's record upheld."})
            else:
                entry.update({"status": "Not Collected", "disputed": False, "waste": [],
                              "reason": "Resident report upheld by barangay admin",
                              "dispute_resolution": "Resident's report upheld."})
            entry.update({"updated_at": stamp_at(day, rng.randint(8, 11), rng.randint(0, 59)),
                          "updated_by": by})
            settled += 1
    return settled


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

    # What the app's own end-of-day job derived from the demo goes with it:
    # the frozen daily summaries, and the MRFs it marked missed because the
    # day closed with nothing recorded. Both describe records that are now
    # gone, and left in place a regenerated day would keep its old frozen
    # figures -- a day is never frozen twice -- and its old missed pickups.
    purge("history", lambda r: False)
    purge("mrf_pickups", lambda r: not r.get("auto_missed"))

    # Carry-overs and their notifications are written by the services, not by
    # this tool, so they carry no marker of their own. What identifies them is
    # that the pickup they point at is now gone -- a dangling row the admin's
    # Carry-Over page would otherwise show forever.
    pickups = {p["id"] for p in storage.find("mrf_pickups")}
    purge("carry_overs", lambda r: r.get("last_pickup_id") in pickups)

    # The same goes for the alerts the services raised about them: an alert
    # about a pickup or a carry-over that no longer exists is news about
    # nothing, and left behind it piles up across resets in a barangay's bell.
    carry_overs = {c["id"] for c in storage.find("carry_overs")}
    purge("notifications", lambda n: (
        (not n.get("carry_over_id") or n["carry_over_id"] in carry_overs)
        and (not n.get("pickup_id") or n["pickup_id"] in pickups)))

    for unit in storage.find("vehicles"):
        if unit.get("status") == "assigned":
            storage.update("vehicles", unit["id"], {"status": "available"}, ACTOR)
    return removed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="how many days of history, today included")
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

    from services import history_service, mrf_service

    rng = random.Random(args.seed)
    days = max(1, args.days)
    today = timeutil.today()
    go_live = today - timedelta(days=days - 1)
    print(f"Filling {Config.CITY_NAME} with demo activity "
          f"({len(active_numbers())} barangays, {days} day(s), "
          f"live since {timeutil.display_date(go_live)})")

    cast = build_cast(rng)
    print(f"  accounts        {len(cast['tricycles'])} tricycle collectors, "
          f"{len(cast['trucks'])} truck operators, "
          f"{len(cast['barangay_admins'])} barangay admins")

    made, registers_later = plan_properties(rng, go_live, days)
    backdated = backdate_setup(rng, go_live)
    total = len(storage.find("properties"))
    print(f"  properties      {made} registered before go-live, "
          f"{sum(len(v) for v in registers_later.values())} to register later, "
          f"{total} in total")
    print(f"  set-up dates    {backdated} accounts dated to the week before go-live")

    absent = past_leave(rng, cast, go_live, days)

    # A day this tool has already built is left exactly as it is. Skipping
    # only duplicate entries was not enough: a second run filled in the
    # households the first had left unvisited, on days already frozen into
    # History -- so the records stopped matching the figures reported.
    built = {c["date"] for c in storage.read("collections") if c.get("demo_generated")}

    for offset in range(days - 1, -1, -1):
        day = timeutil.date_str(today - timedelta(days=offset))
        is_today = offset == 0
        if day in built:
            print(f"  {day}      already built, left as it is")
            continue

        # New households that registered today, before the round begins.
        joined = registers_later.pop(day, [])
        insert_many("properties", joined, ACTOR)
        settle_disputes(rng, day)

        if schedule_service.is_collection_day(day):
            # Past days finish; today is deliberately still in progress.
            c = collect_day(rng, cast, day,
                            completion=.78 if is_today else rng.uniform(.9, .98),
                            absent=absent.get(day))
            # The admin arranges some of the earlier misses for this day. On
            # past days the truck then collects them; today they are left
            # Pending, so the truck page has live carry-over stops to show.
            arranged = arrange_carry_overs(rng, cast, day, share=.5 if is_today else .6)
            m = pickup_day(rng, cast, day, misses=3 if is_today else rng.choice([0, 1, 1, 2]),
                           collect_carry_overs=not is_today)
            r = resident_reports(rng, day, count=4 if is_today else rng.randint(1, 3))
            print(f"  {day}      {c['collected']} collected, "
                  f"{c['not_collected']} refused, {m['collected']} MRFs picked up, "
                  f"{m['missed']} missed, {m['carried']} of {arranged} carry-overs "
                  f"collected, {m['delivered']} landfill runs, {r} resident reports"
                  + (f", {len(joined)} new household(s)" if joined else "")
                  + (", a collector off on approved leave" if absent.get(day) else ""))
        else:
            print(f"  {day}      no collection scheduled"
                  + (f", {len(joined)} new household(s)" if joined else ""))

        # Close the day as the app does at midnight: anything left without a
        # status is marked missed, and the day is frozen into History.
        if not is_today:
            mrf_service.auto_mark_missed(day, ACTOR)
            history_service.freeze(day, ACTOR)

    staged = stage_carry_overs(rng, cast)
    print(f"  carry-overs     {staged['pending']} rescheduled and pending, "
          f"{staged['missed']} still unassigned")

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
