"""
Seed the JSON store with the reference data the system cannot start without.

    python seed.py

Idempotent by design: every record is matched on a natural key (barangay id,
vehicle code, username, schedule day) and skipped if already present. Re-run it
as often as you like -- it will never duplicate a row, and it will never
overwrite an edit an admin has since made through the UI.

What it creates:
  * 31 barangays, numbered 1-31, with puroks and a derived zone colour group
  * 31 tricycle units (TRI-01..TRI-31) and 8 truck units (TRK-01..TRK-08)
  * the default weekly waste schedule
  * one City Hall Admin account -- the only account that can create the rest
  * placeholder geo files, pre-filled with all 31 barangay ids so that
    supplying the real data is a fill-in-the-blanks job

The admin password is never hardcoded. Set SEED_ADMIN_PASSWORD to choose one;
otherwise a strong password is generated and printed once, here, and nowhere
else -- it is stored only as a werkzeug hash.
"""

import json
import os
import secrets
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

from config import Config
from services import geo_service, storage, timeutil

SEED_ACTOR = "seed"

# ---------------------------------------------------------------------------
# The 31 barangays of Cabadbaran City, in the canonical order that assigns
# each its number 1-31. Numbering drives the four map zone colour groups
# (1-6, 7-14, 15-22, 23-31), so this order is data, not cosmetics -- if the
# city has an official barangay numbering, replace this list to match it.
# ---------------------------------------------------------------------------

BARANGAY_NAMES = [
    "Antonio Luna", "Bay-ang", "Bayabas", "Caasinan", "Cabinet", "Calamba",
    "Calibunan", "Comagascas", "Concepcion", "Del Pilar", "Katugasan",
    "Kauswagan", "La Union", "Mabini", "Mahaba",
    "Poblacion 1", "Poblacion 2", "Poblacion 3", "Poblacion 4", "Poblacion 5",
    "Poblacion 6", "Poblacion 7", "Poblacion 8", "Poblacion 9", "Poblacion 10",
    "Poblacion 11", "Poblacion 12",
    "Puting Bato", "Sanghan", "Soriano", "Tolosa",
]

# Placeholder until each barangay's real purok list is supplied.
DEFAULT_PUROKS = [f"Purok {n}" for n in range(1, 8)]

TRICYCLE_UNITS = 31   # one per barangay as a floor; add more in the registry
TRUCK_UNITS = Config.MAX_TRUCKS  # 8, the city-wide ceiling

# ---------------------------------------------------------------------------
# Default weekly waste schedule (FUNCTIONALITIES.md section 8).
# "Kitchen Waste" is the agreed term citywide -- never "Food Waste".
# ---------------------------------------------------------------------------

WASTE_SCHEDULE = [
    {"day": "Monday", "waste_type": "Biodegradable and Net Residual Waste",
     "short": "Biodegradable + Residual", "tone": "green",
     "details": ["Kitchen Waste", "Yard Waste",
                 "Diaper / Used Tissue / Sanitary Napkins"]},
    {"day": "Tuesday", "waste_type": "Recyclable Waste",
     "short": "Recyclable", "tone": "blue",
     "details": ["Paper / Cardboard", "Plastic", "Metal / Cans", "Glass"]},
    {"day": "Wednesday", "waste_type": "Biodegradable and Net Residual Waste",
     "short": "Biodegradable + Residual", "tone": "green",
     "details": ["Kitchen Waste", "Yard Waste",
                 "Diaper / Used Tissue / Sanitary Napkins"]},
    {"day": "Thursday", "waste_type": "Residual Waste",
     "short": "Residual", "tone": "amber",
     "details": ["Wrapper", "Tarpaulin", "Cigarette Butt",
                 "Damaged Shoes, Bag, Rag"]},
    {"day": "Friday", "waste_type": "Biodegradable and Net Residual Waste",
     "short": "Biodegradable + Residual", "tone": "green",
     "details": ["Kitchen Waste", "Yard Waste",
                 "Diaper / Used Tissue / Sanitary Napkins"]},
    {"day": "Saturday", "waste_type": "Special Waste",
     "short": "Special Waste", "tone": "red",
     "details": ["Paint Can", "Solvent", "Battery", "Ink Bottle", "Gadget"]},
    {"day": "Sunday", "waste_type": "No Collection",
     "short": "No Collection", "tone": "muted", "details": []},
]


def barangay_id(number: int) -> str:
    return f"brgy-{number:02d}"


# ---------------------------------------------------------------------------
# Seed steps. Each returns (created, skipped).
# ---------------------------------------------------------------------------

def seed_barangays() -> tuple[int, int]:
    created = skipped = 0
    for index, name in enumerate(BARANGAY_NAMES, start=1):
        group = geo_service.zone_group_for(index)
        record = {
            "id": barangay_id(index),
            "number": index,
            "name": name,
            "zone_group": group["key"],
            "zone_label": group["label"],
            "puroks": list(DEFAULT_PUROKS),
            "mrf_name": f"{name} MRF",
        }
        _, made = storage.upsert_by("barangays", "id", record, SEED_ACTOR)
        created, skipped = (created + 1, skipped) if made else (created, skipped + 1)
    return created, skipped


def seed_vehicles() -> tuple[int, int]:
    created = skipped = 0
    units = ([("tricycle", f"TRI-{n:02d}") for n in range(1, TRICYCLE_UNITS + 1)] +
             [("truck", f"TRK-{n:02d}") for n in range(1, TRUCK_UNITS + 1)])
    for kind, code in units:
        record = {"code": code, "type": kind, "status": "available", "note": ""}
        _, made = storage.upsert_by("vehicles", "code", record, SEED_ACTOR)
        created, skipped = (created + 1, skipped) if made else (created, skipped + 1)
    return created, skipped


def seed_schedule() -> tuple[int, int]:
    created = skipped = 0
    for row in WASTE_SCHEDULE:
        _, made = storage.upsert_by("waste_schedule", "day", dict(row), SEED_ACTOR)
        created, skipped = (created + 1, skipped) if made else (created, skipped + 1)
    return created, skipped


def seed_admin() -> tuple[dict | None, str | None]:
    """
    Create the first City Hall Admin. Returns (user, plaintext_password);
    the password is None when the account already existed.
    """
    username = os.environ.get("SEED_ADMIN_USERNAME", "city_admin").strip()
    if storage.find_one("users", username=username):
        return None, None

    password = os.environ.get("SEED_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    user = storage.insert("users", {
        "full_name": os.environ.get("SEED_ADMIN_NAME", "City Hall Administrator"),
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": "city_admin",
        "assigned_barangay": None,
        "assigned_barangays": [],
        "assigned_vehicle": None,
        "contact_number": "",
        "status": "Active",
        "must_change_password": True,
    }, SEED_ACTOR)
    return user, password


# ---------------------------------------------------------------------------
# Geo placeholders
# ---------------------------------------------------------------------------

PLACEHOLDER_BANNER = (
    "PLACEHOLDER -- replace this file with real data. "
    "See docs/DATA_REQUIREMENTS.md. Delete the _placeholder flag when the "
    "data is real, or the app will keep treating this file as a stub."
)


def _write_geo(filename: str, payload) -> bool:
    """Write a geo file only if it is absent. Never overwrite real data."""
    path = geo_service.geo_path(filename)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return True


def seed_geo_placeholders() -> list[str]:
    """
    Ship valid, clearly-marked placeholders pre-filled with all 31 barangay
    ids, so filling in the real data is a matter of pasting coordinates rather
    than authoring files from scratch.
    """
    written = []

    zones = {
        "type": "FeatureCollection",
        "_placeholder": True,
        "_note": PLACEHOLDER_BANNER,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "barangay_id": barangay_id(n),
                    "name": name,
                    "zone_group": geo_service.zone_group_for(n)["key"],
                },
                # null geometry is valid GeoJSON. We deliberately ship no fake
                # polygons: an invented boundary drawn over a real satellite
                # map reads as authoritative when it is not.
                "geometry": None,
            }
            for n, name in enumerate(BARANGAY_NAMES, start=1)
        ],
    }
    if _write_geo(geo_service.ZONES_FILE, zones):
        written.append(geo_service.ZONES_FILE)

    mrfs = [{"_placeholder": True, "_placeholder_note": PLACEHOLDER_BANNER}] + [
        {"barangay_id": barangay_id(n), "name": f"{name} MRF",
         "lat": None, "lng": None}
        for n, name in enumerate(BARANGAY_NAMES, start=1)
    ]
    if _write_geo(geo_service.MRFS_FILE, mrfs):
        written.append(geo_service.MRFS_FILE)

    hotspots = {
        "type": "FeatureCollection",
        "_placeholder": True,
        "_note": PLACEHOLDER_BANNER + " Until then HOTSPOT_SOURCE='derived' "
                 "computes hotspots from collection and report data.",
        "features": [],
    }
    if _write_geo(geo_service.HOTSPOTS_FILE, hotspots):
        written.append(geo_service.HOTSPOTS_FILE)

    return written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DEMO_ACCOUNTS = [
    {"username": "brgy_admin", "full_name": "Barangay Admin (demo)",
     "role": "barangay_admin", "assigned_barangay": "brgy-16"},
    {"username": "tri_collector", "full_name": "Tricycle Collector (demo)",
     "role": "tricycle_collector", "assigned_barangay": "brgy-16",
     "assigned_vehicle": "TRI-16", "assigned_purok": "Purok 1"},
    {"username": "truck_collector", "full_name": "Truck Collector (demo)",
     "role": "truck_collector", "assigned_vehicle": "TRK-01",
     "assigned_barangays": ["brgy-01", "brgy-02", "brgy-03", "brgy-04"]},
]


def seed_demo_accounts() -> list[tuple[str, str]]:
    """
    One account per non-admin role, for walking through the app before User
    Management exists (Phase 2). Opt-in via `python seed.py --demo`.

    Passwords come from SEED_DEMO_PASSWORD when it is set, and are generated
    per account otherwise. Setting it matters on a host with an ephemeral
    filesystem: the data is rebuilt on every restart, and generated passwords
    would change with it, locking you out of your own demo. A fixed one means
    the same logins work no matter how often the instance resets.

    Nothing is hardcoded and nothing is stored unhashed either way.
    """
    shared = os.environ.get("SEED_DEMO_PASSWORD")

    made = []
    for spec in DEMO_ACCOUNTS:
        if storage.find_one("users", username=spec["username"]):
            continue
        password = shared or secrets.token_urlsafe(9)
        storage.insert("users", {
            "full_name": spec["full_name"],
            "username": spec["username"],
            "password_hash": generate_password_hash(password),
            "role": spec["role"],
            "assigned_barangay": spec.get("assigned_barangay"),
            "assigned_barangays": spec.get("assigned_barangays", []),
            "assigned_vehicle": spec.get("assigned_vehicle"),
            "assigned_purok": spec.get("assigned_purok"),
            "contact_number": "",
            "status": "Active",
            "must_change_password": False,
        }, SEED_ACTOR)
        made.append((spec["username"], password))
    return made


DEMO_PROPERTIES = [
    ("Nica Abayon", "House", "Purok 1", "None Composting"),
    ("Iliana Dwane", "House", "Purok 1", "Composting"),
    ("Marites Ocampo", "House", "Purok 1", ""),
    ("Sari-Sari ni Aling Nena", "Establishment", "Purok 1", "Has Special Waste"),
    ("Bautista Residence", "House", "Purok 2", ""),
    ("Villaflor Boarding House", "Establishment", "Purok 2", "Composting"),
    ("Cruz Residence", "House", "Purok 2", ""),
    ("Poblacion Bakery", "Establishment", "Purok 3", "Has Special Waste"),
]


def seed_demo_properties(barangay_id: str) -> int:
    """
    A handful of households so the collector route is walkable before the
    barangay admin's Property List exists (Phase 5). Opt-in via --demo.
    """
    made = 0
    for owner, kind, purok, tag in DEMO_PROPERTIES:
        record = {"owner_name": owner, "type": kind, "barangay_id": barangay_id,
                  "purok": purok, "tag": tag, "note": ""}
        _, created = storage.upsert_by("properties", "owner_name", record, SEED_ACTOR)
        made += 1 if created else 0
    return made


def seed_demo_assignments() -> int:
    """Give the demo collectors a route, so their apps have something to show."""
    made = 0
    tri = storage.find_one("users", username="tri_collector")
    if tri and not storage.find_one("assignments_tricycle", collector_id=tri["id"]):
        storage.insert("assignments_tricycle", {
            "collector_id": tri["id"],
            "barangay_id": tri.get("assigned_barangay") or barangay_id(16),
            "purok_coverage": ["Purok 1", "Purok 2", "Purok 3"],
            "tricycle_code": tri.get("assigned_vehicle") or "TRI-16",
            "status": "Active", "note": "Demo assignment",
        }, SEED_ACTOR)
        storage.update("users", tri["id"],
                       {"assigned_puroks": ["Purok 1", "Purok 2", "Purok 3"],
                        "assigned_purok": "Purok 1"}, SEED_ACTOR)
        made += 1

    trk = storage.find_one("users", username="truck_collector")
    if trk and not storage.find_one("assignments_truck", operator_id=trk["id"]):
        covered = trk.get("assigned_barangays") or [barangay_id(n) for n in range(1, 5)]
        storage.insert("assignments_truck", {
            "operator_id": trk["id"],
            "truck_code": trk.get("assigned_vehicle") or "TRK-01",
            "covered_mrfs": covered,
            "planned_pickup_times": {covered[0]: "09:00"} if covered else {},
            "status": "Active", "note": "Demo assignment",
        }, SEED_ACTOR)
        made += 1
    return made


def run(demo: bool = False) -> int:
    print(f"Seeding {Config.CITY_NAME} data store at {Config.DATA_DIR}")
    storage.bootstrap()

    steps = [
        ("barangays", seed_barangays()),
        ("vehicles", seed_vehicles()),
        ("waste schedule", seed_schedule()),
    ]
    for label, (created, skipped) in steps:
        print(f"  {label:<16} {created:>3} created, {skipped:>3} already present")

    written = seed_geo_placeholders()
    if written:
        print(f"  geo placeholders {len(written):>3} written: {', '.join(written)}")
    else:
        print(f"  geo placeholders   0 written, files already present")

    admin, password = seed_admin()
    if admin:
        print("\n  City Hall Admin account created")
        print(f"    username: {admin['username']}")
        if os.environ.get("SEED_ADMIN_PASSWORD"):
            print("    password: (taken from SEED_ADMIN_PASSWORD)")
        else:
            print(f"    password: {password}")
            print("    ^ shown once, stored only as a hash. Save it now.")
    else:
        print("\n  City Hall Admin account already present, left untouched")

    if demo:
        made = seed_demo_accounts()
        if made and os.environ.get("SEED_DEMO_PASSWORD"):
            # Don't echo a password that is already in the host's environment
            # -- it would sit in the build log for anyone with dashboard access.
            print("\n  Demo accounts created (password from SEED_DEMO_PASSWORD):")
            for username, _ in made:
                print(f"    {username}")
        elif made:
            print("\n  Demo accounts created (shown once):")
            for username, password in made:
                print(f"    {username:<16} {password}")
        else:
            print("\n  Demo accounts already present, left untouched")

        tri = storage.find_one("users", username="tri_collector")
        home = (tri or {}).get("assigned_barangay") or barangay_id(16)
        props = seed_demo_properties(home)
        routes = seed_demo_assignments()
        print(f"  Demo properties  {props:>3} created")
        print(f"  Demo assignments {routes:>3} created")

    Path(Config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    print(f"\nDone. Run `python app.py` to start the app.")
    return 0


if __name__ == "__main__":
    sys.exit(run(demo="--demo" in sys.argv))
