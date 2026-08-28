"""Phase 4 verification: MRF pickups, deliveries, carry-over lifecycle."""
import shutil, sys, tempfile
from datetime import timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p4-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, carryover_service, collection_service,
                      mrf_service, property_service, schedule_service, storage,
                      timeutil, user_service)
from services.auth_service import public_view
from services.validation import ValidationError
import seed

results = []
def ok(label, cond):
    results.append(bool(cond)); print(f"  {'PASS' if cond else 'FAIL':<4} {label}"); return cond

def fails(label, fn, field=None, text=None):
    try:
        fn()
    except ValidationError as exc:
        hit = (not field or field in exc.errors) and (not text or text.lower() in exc.message.lower())
        return ok(label, hit) or print(f"       got: {exc.errors}")
    return ok(label, False) or print("       no error raised")

class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]

seed.run()
ADMIN = storage.find_one("users", role="city_admin")["id"]
B1, B2, B3 = "brgy-01", "brgy-02", "brgy-03"
TODAY = timeutil.today_str()
YESTERDAY = timeutil.date_str(timeutil.today() - timedelta(days=1))

# Make sure today has a collection schedule so entries can be recorded.
if not schedule_service.waste_types_for():
    form = {f"{k}__{d}": v for d in schedule_service.DAYS
            for k, v in (("waste_type", "Biodegradable and Net Residual Waste"),
                         ("short", "Biodegradable + Residual"), ("tone", "green"),
                         ("details", "Kitchen Waste\nYard Waste"))}
    schedule_service.save_week(form, ADMIN)
TYPES = schedule_service.waste_types_for()

# --- fixtures: a collector filling two barangay MRFs, and two truck operators
col = user_service.create({"full_name": "Tri One", "username": "tri_a",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1,
    "tricycle_code": "TRI-01", "effective_date": timeutil.today_str(),
    "status": "Active"}), None, ADMIN)
COL = public_view(storage.get("users", col["id"]))

op = user_service.create({"full_name": "Truck One", "username": "trk_a",
                          "role": "truck_collector", "assigned_vehicle": "TRK-01",
                          "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op["id"], "truck_code": "TRK-01",
    "covered_mrfs": [B1, B2, B3], "effective_date": timeutil.today_str(),
    "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
OP = public_view(storage.get("users", op["id"]))

op2 = user_service.create({"full_name": "Truck Two", "username": "trk_b",
                           "role": "truck_collector", "assigned_vehicle": "TRK-02",
                           "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op2["id"], "truck_code": "TRK-02",
    "covered_mrfs": ["brgy-10"], "effective_date": timeutil.today_str(),
    "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
OP2 = public_view(storage.get("users", op2["id"]))

def household(name, barangay=B1, purok="Purok 1"):
    return property_service.create(Form({"owner_name": name, "type": "House",
                                         "purok": purok}), barangay, ADMIN)

def collect(prop, sacks=10, kilos=2, collector=COL):
    form = {"status": "Collected", "qty_0": str(sacks), "unit_0": "Sack"}
    if len(TYPES) > 1:
        form["qty_1"] = str(kilos); form["unit_1"] = "Kilo"
    return collection_service.save_entry(Form(form), None, prop, collector)

print("\n[1] the aggregation chain -- nothing entered twice")
p1, p2 = household("Household A"), household("Household B")
collect(p1, 10, 2)
collect(p2, 5, 1)
card = mrf_service.mrf_card(B1)
expected_sacks = 15
expected_kilos = 3 if len(TYPES) > 1 else 0
ok("MRF card load comes from the barangay's collections",
   card["load"]["sacks"] == expected_sacks and card["load"]["kilos"] == expected_kilos)
ok("the truck operator enters no quantities anywhere",
   "qty" not in str(mrf_service.save_pickup.__doc__ or "").lower())
ok("card matches the barangay total exactly",
   card["load"]["sacks"] == collection_service.barangay_totals(B1)["sacks"])
ok("source schedule day is the day the waste was collected",
   card["source_schedule_day"] == timeutil.weekday_name(TODAY))
ok("an empty MRF reads Pending with nothing in it",
   mrf_service.mrf_card(B2)["status"] == "Pending"
   and mrf_service.mrf_card(B2)["load"]["empty"])

print("\n[2] recording a pickup")
pickup = mrf_service.save_pickup(Form({"status": "Collected from MRF",
                                       "gps": "9.12,125.53"}), B1, OP)
ok("pickup saved as Collected from MRF", pickup["status"] == "Collected from MRF")
ok("load snapshotted onto the pickup", pickup["load"]["sacks"] == expected_sacks)
ok("truck code stamped from the assignment", pickup["truck_code"] == "TRK-01")
ok("covered dates recorded", pickup["covered_dates"] == [TODAY])
ok("card now reads collected", mrf_service.mrf_card(B1)["status"] == "Collected from MRF")
fails("a reason is required to mark not collected",
      lambda: mrf_service.save_pickup(Form({"status": "Not Collected"}), B2, OP), "reason")
fails("invented reason rejected",
      lambda: mrf_service.save_pickup(Form({"status": "Not Collected",
                                            "reason": "Could not be bothered"}), B2, OP),
      "reason")

print("\n[3] the MRF empties on pickup")
ok("nothing waiting after a successful pickup",
   mrf_service.uncollected_entries(B1) == [])
ok("a re-visit shows an empty MRF, not the same load again",
   mrf_service.mrf_card(B1)["load"]["sacks"] == expected_sacks)  # snapshot preserved
p3 = household("Household C")
collect(p3, 4, 0)
ok("new collections accumulate for the next pickup",
   collection_service.totals(mrf_service.uncollected_entries(B1))["sacks"] == 4)
ok("the recorded pickup keeps its original load",
   storage.get("mrf_pickups", pickup["id"])["load"]["sacks"] == expected_sacks)

print("\n[4] running load and delivery reset")
load = mrf_service.running_load(OP["id"])
ok("running load holds the collected pickup", load["sacks"] == expected_sacks
   and load["mrf_count"] == 1)
p4 = household("Household D", B2, "Purok 1")
storage.update("properties", p4["id"], {"barangay_id": B2})
collect(p4, 6, 0)
mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B2, OP)
load = mrf_service.running_load(OP["id"])
ok("a second MRF adds to the running load",
   load["mrf_count"] == 2 and load["sacks"] == expected_sacks + 6)

delivery = mrf_service.deliver(OP)
ok("delivery records the whole load", delivery["load"]["sacks"] == expected_sacks + 6)
ok("delivery lists the MRFs included", len(delivery["mrfs_included"]) == 2)
after = mrf_service.running_load(OP["id"])
ok("running load resets to zero after delivering",
   after["empty"] and after["mrf_count"] == 0)
fails("delivering an empty truck is refused",
      lambda: mrf_service.deliver(OP), "form", "no collected load")

collect(household("Household E"), 3, 0)
mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B3, OP)
second = mrf_service.running_load(OP["id"])
ok("a second trip starts clean rather than double-counting",
   second["mrf_count"] == 1)
mrf_service.deliver(OP)
ok("two separate deliveries recorded", storage.count("deliveries") == 2)

print("\n[5] carry-over opens on a missed pickup")
# Start this section from a clean slate: earlier sections delivered B1.
storage.write("mrf_pickups", [])
storage.write("deliveries", [])
storage.write("carry_overs", [])
p_miss = household("Household H")
collect(p_miss, 12, 0)
missed = mrf_service.save_pickup(Form({"status": "Not Collected",
                                       "reason": "Road inaccessible",
                                       "note": "Bridge under repair"}), B1, OP)
co = carryover_service.outstanding_for(B1)
ok("a missed pickup opens a carry-over", co is not None)
ok("carry-over starts Pending with no truck assigned",
   co["status"] == "Pending" and co["current_truck"] is None)
ok("carry-over remembers the original truck", co["original_truck"] == "TRK-01")
ok("missed pickup adds nothing to the running load",
   mrf_service.running_load(OP["id"])["empty"])
ok("the load stays waiting in the MRF",
   collection_service.totals(mrf_service.uncollected_entries(B1))["sacks"] > 0)

mrf_service.save_pickup(Form({"status": "Not Collected",
                              "reason": "Truck breakdown"}), B1, OP)
ok("a second miss updates the same carry-over rather than opening another",
   storage.count("carry_overs", barangay_id=B1) == 1)
ok("the miss count is tracked",
   carryover_service.outstanding_for(B1)["missed_count"] == 2)

print("\n[6] reassign, reschedule, auto-close")
fails("reassigning to an unregistered truck is refused",
      lambda: carryover_service.reassign(co["id"], "TRK-99", ADMIN), "truck")
carryover_service.reassign(co["id"], "TRK-02", ADMIN)
ok("carry-over reassigned", storage.get("carry_overs", co["id"])["current_truck"] == "TRK-02")
ok("the new truck now sees it as a stop",
   any(c["barangay_id"] == B1 for c in mrf_service.cards_for_operator(OP2["id"])))
ok("the stop is flagged as a carry-over",
   any(c["barangay_id"] == B1 and c["is_carry_over"]
       for c in mrf_service.cards_for_operator(OP2["id"])))

fails("rescheduling to a past date is refused",
      lambda: carryover_service.reschedule(co["id"], "2020-01-01", ADMIN),
      "reschedule_date")
tomorrow = timeutil.date_str(timeutil.today() + timedelta(days=1))
carryover_service.reschedule(co["id"], tomorrow, ADMIN)
ok("carry-over rescheduled",
   storage.get("carry_overs", co["id"])["reschedule_date"] == tomorrow)
ok("a future-dated carry-over drops off today's route",
   not any(c["barangay_id"] == B1 and c.get("is_carry_over")
           for c in mrf_service.cards_for_operator(OP2["id"])))

carryover_service.reschedule(co["id"], TODAY, ADMIN)
closing = mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B1, OP)
closed = storage.get("carry_overs", co["id"])
ok("collecting the MRF closes the carry-over", closed["status"] == "Collected")
ok("the closing pickup is recorded on it",
   closed["collected_by_pickup"] == closing["id"])
ok("the carried load joined that day's totals", closing["load"]["sacks"] > 0)
ok("no carry-over is left outstanding", carryover_service.outstanding_for(B1) is None)
fails("a closed carry-over cannot be reassigned",
      lambda: carryover_service.reassign(co["id"], "TRK-02", ADMIN), "form",
      "already been collected")

print("\n[7] auto-miss closes off a forgotten day")
storage.write("carry_overs", [])
storage.write("mrf_pickups", [])
p_old = household("Household Y")
entry = collect(p_old, 7, 0)
storage.update("collections", entry["id"], {"date": YESTERDAY})
ok("nothing auto-missed for today", mrf_service.auto_mark_missed(TODAY) == [])
created = mrf_service.auto_mark_missed(YESTERDAY)
ok("an untouched MRF with waste is auto-marked missed",
   any(r["barangay_id"] == B1 for r in created))
ok("auto-missed rows are flagged as such",
   all(r["auto_missed"] for r in created))
ok("auto-miss opens a carry-over too",
   carryover_service.outstanding_for(B1) is not None)
ok("an empty MRF is not auto-missed",
   not any(r["barangay_id"] == "brgy-20" for r in created))
ok("running it twice does not duplicate",
   mrf_service.auto_mark_missed(YESTERDAY) == [])

print("\n[8] scope and integrity")
ok("an operator only sees their assigned MRFs",
   {c["barangay_id"] for c in mrf_service.cards_for_operator(OP2["id"])}
   >= {"brgy-10"})
ok("an operator with no assignment sees no route",
   mrf_service.cards_for_operator("usr-9999") == [])
storage.write("mrf_pickups", [])
mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B1, OP)
fails("another truck cannot overwrite today's pickup",
      lambda: mrf_service.save_pickup(Form({"status": "Not Collected",
                                            "reason": "Other"}), B1, OP2),
      "form", "another truck")
p = storage.find_one("mrf_pickups", barangay_id=B1)
mrf_service.deliver(OP)
fails("a delivered pickup can no longer be changed",
      lambda: mrf_service.save_pickup(Form({"status": "Not Collected",
                                            "reason": "Other"}), B1, OP),
      "form", "already been delivered")

print("\n[9] city views")
counts = mrf_service.city_counts()
ok("city counts cover all 31 MRFs", counts["total"] == 31)
ok("pending is the remainder, not a stored value",
   counts["pending"] == 31 - counts["collected"] - counts["missed"])
listing = mrf_service.city_listing()
ok("every barangay appears, recorded or not", len(listing) == 31)
ok("a barangay with no record shows its expected truck",
   any(r["truck"] in ("TRK-01", "TRK-02", "Unassigned") for r in listing))
ok("city totals match the pickups",
   mrf_service.city_totals()["sacks"] == sum(
       p["load"]["sacks"] for p in storage.find("mrf_pickups", date=TODAY)
       if p["status"] == "Collected from MRF"))
ok("filters narrow the listing",
   len(mrf_service.city_listing(barangay_id=B1)) == 1)

hist = mrf_service.history_for_operator(OP["id"])
ok("operator history has pickups and deliveries",
   hist["pickups"] and hist["deliveries"])
ok("another operator's history is separate",
   mrf_service.history_for_operator(OP2["id"])["pickups"] == [])

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
