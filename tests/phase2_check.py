"""Phase 2 verification against a throwaway data dir."""
import re, shutil, sys, tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p2-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, schedule_service, storage,
                      timeutil, user_service, vehicle_service)
from services.validation import ValidationError
import seed

results = []
def ok(label, cond):
    results.append(bool(cond)); print(f"  {'PASS' if cond else 'FAIL':<4} {label}"); return cond

def fails(label, fn, expect_field=None, expect_text=None):
    try:
        fn()
    except ValidationError as exc:
        hit = True
        if expect_field: hit = expect_field in exc.errors
        if hit and expect_text: hit = expect_text.lower() in exc.message.lower()
        return ok(label, hit) or print(f"       got: {exc.errors}")
    return ok(label, False) or print("       no error raised")

seed.run()
ADMIN = storage.find_one("users", role="city_admin")["id"]
B1, B2 = "brgy-01", "brgy-02"

print("\n[1] user creation validation")
base = {"full_name": "Test Person", "username": "tperson", "role": "barangay_admin",
        "assigned_barangay": B1, "password": "goodpass1", "confirm_password": "goodpass1"}
u1 = user_service.create(dict(base), ADMIN)
ok("valid barangay admin created", u1["role"] == "barangay_admin")
ok("password stored only as a hash",
   u1["password_hash"].startswith(("pbkdf2:", "scrypt:")) and "password" not in u1)
fails("duplicate username rejected",
      lambda: user_service.create(dict(base), ADMIN), "username")
fails("duplicate username is case-insensitive",
      lambda: user_service.create({**base, "username": "TPerson"}, ADMIN), "username")
fails("mismatched passwords rejected",
      lambda: user_service.create({**base, "username": "x1", "confirm_password": "other"}, ADMIN),
      "confirm_password")
fails("short password rejected",
      lambda: user_service.create({**base, "username": "x2", "password": "short",
                                   "confirm_password": "short"}, ADMIN), "password")
fails("bad username format rejected",
      lambda: user_service.create({**base, "username": "no spaces!"}, ADMIN), "username")
fails("barangay admin without a barangay rejected",
      lambda: user_service.create({**base, "username": "x3", "assigned_barangay": ""}, ADMIN),
      "assigned_barangay")
fails("tricycle collector without a vehicle rejected",
      lambda: user_service.create({**base, "username": "x4", "role": "tricycle_collector"}, ADMIN),
      "assigned_vehicle")
fails("wrong vehicle type for the role rejected",
      lambda: user_service.create({**base, "username": "x5", "role": "tricycle_collector",
                                   "assigned_vehicle": "TRK-01"}, ADMIN), "assigned_vehicle")
fails("unregistered vehicle rejected",
      lambda: user_service.create({**base, "username": "x6", "role": "tricycle_collector",
                                   "assigned_vehicle": "TRI-99"}, ADMIN), "assigned_vehicle")
fails("bad contact number rejected",
      lambda: user_service.create({**base, "username": "x7", "contact_number": "12345"}, ADMIN),
      "contact_number")
u2 = user_service.create({**base, "username": "phoneguy", "contact_number": "0917 555 0142"}, ADMIN)
ok("contact number normalised to 11 digits", u2["contact_number"] == "09175550142")

col1 = user_service.create({"full_name": "Collector One", "username": "col1",
                            "role": "tricycle_collector", "assigned_barangay": B1,
                            "assigned_vehicle": "TRI-01", "password": "goodpass1",
                            "confirm_password": "goodpass1"}, ADMIN)
fails("a vehicle cannot be held by two accounts",
      lambda: user_service.create({"full_name": "Collector Two", "username": "col2",
                                   "role": "tricycle_collector", "assigned_barangay": B2,
                                   "assigned_vehicle": "TRI-01", "password": "goodpass1",
                                   "confirm_password": "goodpass1"}, ADMIN), "assigned_vehicle")

print("\n[2] last-admin and self-protection")
fails("cannot delete the only city admin",
      lambda: user_service.delete(ADMIN, "someone-else"), "form", "only active City Hall Admin")
fails("cannot delete your own account",
      lambda: user_service.delete(u1["id"], u1["id"]), "form", "your own account")
fails("cannot deactivate yourself",
      lambda: user_service.set_status(ADMIN, "Inactive", ADMIN), "form", "your own account")
fails("cannot deactivate the only city admin",
      lambda: user_service.set_status(ADMIN, "Inactive", u1["id"]), "form", "only active")
admin2 = user_service.create({"full_name": "Second Admin", "username": "admin2",
                              "role": "city_admin", "password": "goodpass1",
                              "confirm_password": "goodpass1"}, ADMIN)
ok("second city admin can now be created", admin2["role"] == "city_admin")
user_service.set_status(ADMIN, "Inactive", admin2["id"])
ok("with a spare admin, deactivation is allowed",
   storage.get("users", ADMIN)["status"] == "Inactive")
user_service.set_status(ADMIN, "Active", admin2["id"])

print("\n[3] editing and password reset")
edited = user_service.edit(u1["id"], {**base, "full_name": "Renamed Person",
                                      "status": "Active"}, ADMIN)
ok("edit updates the record", edited["full_name"] == "Renamed Person")
ok("edit stamps the audit fields", edited["updated_at"] and edited["updated_by"] == ADMIN)
before = storage.get("users", u1["id"])["password_hash"]
user_service.reset_password(u1["id"], {"new_password": "brandnew1",
                                       "confirm_password": "brandnew1"}, ADMIN)
after = storage.get("users", u1["id"])
ok("reset changes the hash", after["password_hash"] != before)
ok("reset forces a change at next login", after["must_change_password"] is True)
fails("reset rejects a short password",
      lambda: user_service.reset_password(u1["id"], {"new_password": "abc",
                                                     "confirm_password": "abc"}, ADMIN),
      "new_password")

print("\n[4] vehicle registry")
ok("seeded 31 tricycles + 8 trucks",
   vehicle_service.counts("tricycle")["total"] == 31 and
   vehicle_service.counts("truck")["total"] == 8)
fails("truck cap of 8 enforced",
      lambda: vehicle_service.register("TRK-09", "truck", "", ADMIN), "code", "limited to 8")
fails("duplicate code rejected",
      lambda: vehicle_service.register("TRI-01", "tricycle", "", ADMIN), "code", "already registered")
fails("bad code format rejected",
      lambda: vehicle_service.register("BIKE1", "tricycle", "", ADMIN), "code")
fails("prefix must match the type",
      lambda: vehicle_service.register("TRI-40", "truck", "", ADMIN), "code", "must start with TRK")
new_unit = vehicle_service.register("TRI-32", "tricycle", "New donation", ADMIN)
ok("a valid new tricycle registers", new_unit["code"] == "TRI-32")
trk = vehicle_service.by_code("TRK-08")
vehicle_service.set_active(trk["id"], False, ADMIN)
ok("deactivated unit leaves the available list",
   "TRK-08" not in vehicle_service.available("truck"))
ok("a free truck slot opens once one is withdrawn",
   vehicle_service.register("TRK-09", "truck", "", ADMIN)["code"] == "TRK-09")

print("\n[5] tricycle assignments")
ops = {}
for i in range(1, 4):
    ops[i] = user_service.create({"full_name": f"Tri Collector {i}", "username": f"tri{i}",
                                  "role": "tricycle_collector", "assigned_barangay": B1,
                                  "assigned_vehicle": f"TRI-1{i}", "password": "goodpass1",
                                  "confirm_password": "goodpass1"}, ADMIN)

TODAY = timeutil.today_str()


class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]

a1, warn = assignment_service.save_tricycle_assignment(Form({
    "collector_id": ops[1]["id"], "barangay_id": B1, "tricycle_code": "TRI-02",
    "effective_date": TODAY, "status": "Active", "note": ""}), None, ADMIN)
ok("assignment saved", a1["barangay_id"] == B1)
ok("effectivity date stored", a1["effective_date"] == TODAY)
ok("no spurious warnings on a clean save", warn == [])
ok("account mirrors the assignment",
   storage.get("users", ops[1]["id"])["assigned_barangay"] == B1)

fails("one active assignment per collector",
      lambda: assignment_service.save_tricycle_assignment(Form({
          "collector_id": ops[1]["id"], "barangay_id": B2,
          "tricycle_code": "TRI-03", "effective_date": TODAY,
          "status": "Active"}), None, ADMIN), "collector_id", "already holds")

fails("one active assignment per unit",
      lambda: assignment_service.save_tricycle_assignment(Form({
          "collector_id": ops[2]["id"], "barangay_id": B1,
          "tricycle_code": "TRI-02", "effective_date": TODAY,
          "status": "Active"}), None, ADMIN), "tricycle_code", "already assigned")

fails("a date of effectivity is required",
      lambda: assignment_service.save_tricycle_assignment(Form({
          "collector_id": ops[2]["id"], "barangay_id": B1,
          "tricycle_code": "TRI-03", "status": "Active"}), None, ADMIN),
      "effective_date")

_, warn = assignment_service.save_tricycle_assignment(Form({
    "collector_id": ops[2]["id"], "barangay_id": B1, "tricycle_code": "TRI-03",
    "effective_date": TODAY, "status": "Active"}), None, ADMIN)
ok("a second collector on the same barangay warns but still saves",
   warn and "also covered by" in warn[0]
   and storage.count("assignments_tricycle") == 2)

counts = assignment_service.tricycle_counts()
ok("counters reflect reality",
   counts["active_assignments"] == 2 and counts["barangays_covered"] == 1
   and counts["total_barangays"] == 31)

assignment_service.end_assignment(assignment_service.TRICYCLE_COLLECTION, a1["id"], ADMIN)
ok("ending an assignment frees the unit", "TRI-02" in vehicle_service.available("tricycle"))
ok("ended assignments leave the active count",
   assignment_service.tricycle_counts()["active_assignments"] == 1)

print("\n[6] truck assignments and coverage")
tops = {}
for i in range(1, 4):
    tops[i] = user_service.create({"full_name": f"Truck Op {i}", "username": f"trk{i}",
                                   "role": "truck_collector", "assigned_vehicle": f"TRK-0{i}",
                                   "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)

t1, warn = assignment_service.save_truck_assignment(Form({
    "operator_id": tops[1]["id"], "truck_code": "TRK-01",
    "covered_mrfs": ["brgy-01", "brgy-02", "brgy-03", "brgy-04"],
    "effective_date": timeutil.today_str(), "status": "Active", "planned_time__brgy-01": "08:30"}), None, ADMIN)
ok("truck assignment saved with 4 MRFs", len(t1["covered_mrfs"]) == 4)
ok("planned pickup time stored", t1["planned_pickup_times"]["brgy-01"] == "08:30")
ok("uncovered barangays are reported after saving",
   any("still have no assigned truck" in w for w in warn))

fails("bad pickup time rejected",
      lambda: assignment_service.save_truck_assignment(Form({
          "operator_id": tops[2]["id"], "truck_code": "TRK-02",
          "covered_mrfs": ["brgy-05"], "effective_date": timeutil.today_str(), "status": "Active",
          "planned_time__brgy-05": "25:99"}), None, ADMIN), "planned_time__brgy-05")

fails("one active assignment per operator",
      lambda: assignment_service.save_truck_assignment(Form({
          "operator_id": tops[1]["id"], "truck_code": "TRK-02",
          "covered_mrfs": ["brgy-05"], "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN),
      "operator_id", "already holds")

_, warn = assignment_service.save_truck_assignment(Form({
    "operator_id": tops[2]["id"], "truck_code": "TRK-02",
    "covered_mrfs": ["brgy-04", "brgy-05"], "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
ok("double-covered barangay warns but saves",
   any("also covered by" in w for w in warn))

tc = assignment_service.truck_counts()
ok("MRFs covered counted across assignments", tc["mrfs_covered"] == 5)
ok("uncovered list is the remaining 26", len(tc["uncovered"]) == 26)
ok("not reported as fully covered", tc["fully_covered"] is False)

# Cover everything: 8 trucks is enough for 31 barangays.
all_b = [f"brgy-{n:02d}" for n in range(1, 32)]
assignment_service.save_truck_assignment(Form({
    "operator_id": tops[3]["id"], "truck_code": "TRK-03",
    "covered_mrfs": all_b[5:], "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
tc = assignment_service.truck_counts()
ok("full coverage detected once every MRF is assigned",
   tc["fully_covered"] and tc["mrfs_covered"] == 31 and tc["uncovered"] == [])

print("\n[7] waste schedule")
ok("seeded seven days, Monday first",
   len(schedule_service.week()) == 7 and schedule_service.week()[0]["day"] == "Monday")
ok("collection waste types come from the schedule",
   "Kitchen Waste" in schedule_service.for_day("Monday")["details"])
ok("no 'Food Waste' anywhere -- Kitchen Waste is the agreed term",
   not any("food waste" in str(r).lower() for r in schedule_service.week()))

form = {}
for d in schedule_service.DAYS:
    row = schedule_service.for_day(d)
    form[f"waste_type__{d}"] = row["waste_type"]
    form[f"short__{d}"] = row["short"]
    form[f"tone__{d}"] = row["tone"]
    form[f"details__{d}"] = "\n".join(row["details"])
form["waste_type__Tuesday"] = "Recyclable Waste"
form["details__Tuesday"] = "Paper / Cardboard\nPlastic\nGlass"
schedule_service.save_week(form, ADMIN)
ok("schedule edit persists", schedule_service.for_day("Tuesday")["details"] ==
   ["Paper / Cardboard", "Plastic", "Glass"])
ok("saving does not duplicate rows", storage.count("waste_schedule") == 7)
ok("comma-separated details also parse",
   schedule_service._split_details("A, B, B, C") == ["A", "B", "C"])
fails("blank waste type rejected",
      lambda: schedule_service.save_week({**form, "waste_type__Monday": ""}, ADMIN),
      "waste_type__Monday")
ok("a rejected save changes nothing",
   schedule_service.for_day("Monday")["waste_type"] != "")

print("\n[8] listings and filters")
rows = user_service.listing(role="tricycle_collector")
ok("role filter works", rows and all(r["role"] == "tricycle_collector" for r in rows))
ok("search matches name or username",
   len(user_service.listing(search="Truck Op 1")) == 1)
ok("barangay filter works",
   all(r["barangay_id"] == B1 for r in user_service.listing(barangay=B1)))
ok("listing resolves barangay ids to names",
   all(r["barangay"] != r["barangay_id"] for r in user_service.listing(barangay=B1)))
ok("listing never leaks the password hash",
   all("password_hash" not in r for r in user_service.listing()))

print("\n[9] deleting a user removes their assignment")
before_count = storage.count("assignments_truck")
user_service.delete(tops[3]["id"], ADMIN)
ok("assignment deleted with the account",
   storage.count("assignments_truck") == before_count - 1)
ok("no orphaned assignment points at a deleted user",
   all(storage.get("users", r["operator_id"]) for r in storage.read("assignments_truck")))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
