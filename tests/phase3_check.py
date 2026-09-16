"""Phase 3 verification: tricycle collector app, entries, proofs, duty."""
import io, shutil, sys, tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p3-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, collection_service, duty_service,
                      property_service, schedule_service, storage, timeutil,
                      unavailable_service, user_service)
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

seed.run()
ADMIN = storage.find_one("users", role="city_admin")["id"]
B1 = "brgy-01"

class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]

class Upload:
    """Stand-in for a Werkzeug FileStorage."""
    def __init__(self, name, data=b"\xff\xd8\xff-fake-jpeg", mimetype="image/jpeg"):
        self.filename, self.mimetype = name, mimetype
        self.stream = io.BytesIO(data)
    def save(self, path):
        self.stream.seek(0)
        Path(path).write_bytes(self.stream.read())

# --- fixtures ---
col = user_service.create({"full_name": "Route Collector", "username": "route1",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
other = user_service.create({"full_name": "Other Collector", "username": "route2",
                             "role": "tricycle_collector", "assigned_barangay": B1,
                             "assigned_vehicle": "TRI-02", "password": "goodpass1",
                             "confirm_password": "goodpass1"}, ADMIN)
assignment, _ = assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1, "tricycle_code": "TRI-01",
    "effective_date": timeutil.today_str(),
    "status": "Active"}), None, ADMIN)

from services.auth_service import public_view
COL = public_view(storage.get("users", col["id"]))

in_route = property_service.create(Form({"owner_name": "Nica Abayon", "type": "House",
                                         "purok": "Purok 1", "tag": "None Composting"}), B1, ADMIN)
in_route2 = property_service.create(Form({"owner_name": "Iliana Dwane", "type": "House",
                                          "purok": "Purok 2"}), B1, ADMIN)
# Coverage is the whole barangay, so a property in a far purok is still on
# the route -- it is only another barangay that is out of scope.
far_purok = property_service.create(Form({"owner_name": "Far Away", "type": "House",
                                          "purok": "Purok 5"}), B1, ADMIN)

# A household in a barangay this collector is NOT assigned to. The route must
# never show it -- and neither must the record page, or a collector could file
# against any address in the city by editing the URL.
OTHER_B = "brgy-02"
out_of_route = property_service.create(
    Form({"owner_name": "Ricardo Cruz", "type": "House", "purok": "Purok 1"}),
    OTHER_B, ADMIN)

print("\n[1] route scoping")
route = property_service.for_collector(assignment)
ok("route holds every property in the assigned barangay",
   {p["id"] for p in route} == {in_route["id"], in_route2["id"], far_purok["id"]})
ok("another barangay's household is NOT on the route",
   out_of_route["id"] not in {p["id"] for p in route})
ok("every row on the route belongs to the assigned barangay",
   all(p["barangay_id"] == assignment["barangay_id"] for p in route))
ok("no assignment means no route, not the whole barangay",
   property_service.for_collector(None) == [])
ok("the out-of-route property does exist -- it is scoped out, not missing",
   property_service.get(out_of_route["id"]) is not None)
fails("property must belong to the chosen barangay's purok list",
      lambda: property_service.create(Form({"owner_name": "X", "type": "House",
                                            "purok": "Purok 99"}), B1, ADMIN), "purok")
fails("only House or Establishment",
      lambda: property_service.create(Form({"owner_name": "X", "type": "Mansion",
                                            "purok": "Purok 1"}), B1, ADMIN), "type")

print("\n[2] the status model")
ok("no record means Pending",
   collection_service.status_for(in_route["id"]) == "Pending")
c = collection_service.counts(route)
ok("counts start all pending", c["pending"] == 3 and c["collected"] == 0)

today_types = schedule_service.waste_types_for()
if not today_types:   # a Sunday run would otherwise have nothing to record
    form = {f"{k}__{d}": v for d in schedule_service.DAYS
            for k, v in (("waste_type", "Biodegradable and Net Residual Waste"),
                         ("short", "Biodegradable + Residual"), ("tone", "green"),
                         ("details", "Kitchen Waste\nYard Waste"))}
    schedule_service.save_week(form, ADMIN)
    today_types = schedule_service.waste_types_for()
ok("waste types come from today's schedule row", len(today_types) > 0)

print("\n[3] collected entries")
entry = collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "15", "unit_0": "Sack",
          "qty_1": "3", "unit_1": "Kilo", "gps": "9.1226, 125.5344",
          "note": "Left at the gate"}), None, in_route, COL)
ok("entry saved as Collected", entry["status"] == "Collected")
ok("two waste lines recorded", len(entry["waste"]) == 2)
ok("gps parsed into lat/lng", entry["gps"] == {"lat": 9.1226, "lng": 125.5344})
ok("collector and vehicle stamped from the session, not the form",
   entry["collector_id"] == COL["id"] and entry["tricycle_code"] == "TRI-01")
ok("filed under today's Manila date", entry["date"] == timeutil.today_str())
ok("property now reads Collected",
   collection_service.status_for(in_route["id"]) == "Collected")

fails("collected needs at least one quantity above zero",
      lambda: collection_service.save_entry(
          Form({"status": "Collected", "qty_0": "0", "qty_1": "0"}), None, in_route2, COL),
      "waste", "greater than zero")
fails("negative quantity rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Collected", "qty_0": "-5"}), None, in_route2, COL), "qty_0")
fails("non-numeric quantity rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Collected", "qty_0": "many"}), None, in_route2, COL), "qty_0")
fails("absurd quantity rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Collected", "qty_0": "5000"}), None, in_route2, COL), "qty_0")
saved = collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "2", "unit_0": "Sack", "gps": "not,a,point"}),
    None, in_route2, COL)
ok("malformed gps is dropped, not guessed at", saved["gps"] is None)

print("\n[4] not-collected entries and image proof")
fails("not-collected needs a reason",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": ""}), {}, in_route, COL), "reason")
fails("not-collected needs an image proof",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": "Not segregated properly"}),
          {}, in_route, COL), "proof", "image proof is required")
fails("invented reason rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": "Felt like it"}),
          {"proof": Upload("p.jpg")}, in_route, COL), "reason")
fails("non-image upload rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": "Not segregated properly"}),
          {"proof": Upload("payload.php", b"<?php ?>", "application/x-php")}, in_route, COL),
      "proof")
fails("oversized image rejected",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": "Not segregated properly"}),
          {"proof": Upload("big.jpg", b"x" * (Config.MAX_PROOF_BYTES + 1))}, in_route, COL),
      "proof")

# No written location is asked for any more: the entry names a registered
# property, which carries its own barangay and purok, so "where" is answered
# before the collector types anything.
# A stray `location` is posted here on purpose: the field is gone from the
# form, so this stands in for an old cached page or a hand-rolled request, and
# proves the value is dropped rather than quietly stored.
missed = collection_service.save_entry(
    Form({"status": "Not Collected", "reason": "Not segregated properly",
          "location": "Roadside, corner of Rizal St.",
          "note": "Mixed residual with recyclables"}),
    {"proof": Upload("evidence.JPG")}, in_route, COL)
ok("not-collected saves without a written location being required",
   missed["status"] == "Not Collected")
ok("a location posted anyway is ignored, not stored", "location" not in missed)
ok("the property still answers where it happened",
   missed["property_id"] == in_route["id"]
   and missed["barangay_id"] == in_route["barangay_id"]
   and missed["purok"] == in_route["purok"])
ok("not-collected saved with proof", missed["status"] == "Not Collected"
   and missed["image_proof_path"])
ok("proof filed under today's date folder",
   missed["image_proof_path"].startswith(timeutil.today_str() + "/"))
ok("proof filename is random, not the phone's",
   "evidence" not in missed["image_proof_path"])
ok("proof file exists on disk",
   collection_service.proof_path(missed["image_proof_path"]) is not None)
ok("collected fields cleared on a not-collected entry", missed["waste"] == [])
ok("re-saving replaced rather than duplicated",
   storage.count("collections", property_id=in_route["id"]) == 1)

print("\n[5] proof access control and path traversal")
admin_view = public_view(storage.get("users", ADMIN))
brgy_admin = public_view(user_service.create(
    {"full_name": "B Admin", "username": "badmin1", "role": "barangay_admin",
     "assigned_barangay": B1, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
far_admin = public_view(user_service.create(
    {"full_name": "Far Admin", "username": "badmin2", "role": "barangay_admin",
     "assigned_barangay": "brgy-05", "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
other_view = public_view(storage.get("users", other["id"]))

ok("owning collector may view", collection_service.may_view_proof(missed, COL))
ok("city admin may view", collection_service.may_view_proof(missed, admin_view))
ok("same-barangay admin may view", collection_service.may_view_proof(missed, brgy_admin))
ok("another barangay's admin may NOT view",
   not collection_service.may_view_proof(missed, far_admin))
ok("another collector may NOT view",
   not collection_service.may_view_proof(missed, other_view))
ok("path traversal refused",
   collection_service.proof_path("../../config.py") is None)
ok("absolute path refused", collection_service.proof_path("/etc/passwd") is None)
ok("unknown file refused", collection_service.proof_path("2020-01-01/nope.jpg") is None)

print("\n[6] same-day correction, past days locked")
fresh = collection_service.entry_for(in_route["id"])
ok("own entry is editable today", collection_service.can_edit(fresh, COL["id"]))
ok("another collector cannot edit it",
   not collection_service.can_edit(fresh, other["id"]))
storage.update("collections", fresh["id"], {"date": "2020-01-01"})
ok("a past-day entry is locked",
   not collection_service.can_edit(storage.get("collections", fresh["id"]), COL["id"]))
storage.update("collections", fresh["id"], {"date": timeutil.today_str()})

storage.update("collections", fresh["id"], {"collector_id": other["id"]})
fails("cannot overwrite another collector's entry for today",
      lambda: collection_service.save_entry(
          Form({"status": "Collected", "qty_0": "1"}), None, in_route, COL),
      "form", "another collector")
storage.update("collections", fresh["id"], {"collector_id": COL["id"]})

print("\n[7] load totals -- sacks and kilos never mixed")
t = collection_service.totals(collection_service.entries_for_date())
ok("sacks and kilos totalled separately", t["sacks"] == 2 and t["kilos"] == 0)
collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "10", "unit_0": "Sack",
          "qty_1": "4", "unit_1": "Kilo"}), None, in_route, COL)
t = collection_service.totals(collection_service.entries_for_date())
ok("totals aggregate across entries", t["sacks"] == 12 and t["kilos"] == 4)
ok("display keeps the units apart", t["total"] == "12 sacks and 4 kg")
ok("not-collected entries add nothing to the load",
   collection_service.totals([{"status": "Not Collected", "waste": []}])["empty"])
ok("barangay total matches the entries",
   collection_service.barangay_totals(B1)["sacks"] == 12)

print("\n[8] duty and live position")
user_row = storage.get("users", COL["id"])
ok("collectors start off duty", not duty_service.is_on_duty(user_row))
ok("position refused while off duty",
   duty_service.record_location(COL["id"], 9.12, 125.53) is None)
duty_service.set_duty(COL["id"], True)
ok("on duty flag set", duty_service.is_on_duty(storage.get("users", COL["id"])))
ok("position accepted on duty",
   duty_service.record_location(COL["id"], 9.12, 125.53, 12.5) is not None)
ok("out-of-range coordinates refused",
   duty_service.record_location(COL["id"], 999, 125.53) is None)
ok("active collector appears on the live list",
   any(c["id"] == COL["id"] and c["has_position"]
       for c in duty_service.active_collectors("tricycle_collector")))
ok("counts reflect who is on duty",
   duty_service.active_counts()["tricycles"] == 1)
duty_service.set_duty(COL["id"], False)
ok("going off duty clears the position",
   storage.get("users", COL["id"])["last_location"] is None)
ok("off-duty collector leaves the live list",
   duty_service.active_counts()["total"] == 0)

duty_service.set_duty(COL["id"], True)
duty_service.record_location(COL["id"], 9.12, 125.53)
storage.update("users", COL["id"], {"last_location": {
    "lat": 9.12, "lng": 125.53, "at": "2020-01-01T00:00:00+08:00"}})
ok("a stale position is not drawn on the map",
   not duty_service.active_collectors("tricycle_collector")[0]["has_position"])
duty_service.set_duty(COL["id"], False)

print("\n[9] unavailable requests")
tomorrow = timeutil.date_str(timeutil.today() + __import__("datetime").timedelta(days=1))
req = unavailable_service.create(Form({"affected_date": tomorrow,
                                       "reason": "Sick / medical leave",
                                       "notes": "Fever"}), COL)
ok("request created as Pending", req["status"] == "Pending")
fails("overlapping request rejected",
      lambda: unavailable_service.create(Form({"affected_date": tomorrow,
                                               "reason": "Personal leave"}), COL),
      "affected_date", "already have a pending request")
fails("past date rejected",
      lambda: unavailable_service.create(Form({"affected_date": "2020-01-01",
                                               "reason": "Personal leave"}), COL),
      "affected_date")
fails("end before start rejected",
      lambda: unavailable_service.create(Form({"affected_date": tomorrow,
                                               "unavailable_until": "2020-01-01",
                                               "reason": "Personal leave"}), COL),
      "unavailable_until")
ok("shows as unavailable on the affected date",
   assignment_service.is_unavailable(COL["id"], tomorrow))
ok("still available today", not assignment_service.is_unavailable(COL["id"]))
ok("counter picks the request up",
   assignment_service.tricycle_counts()["unavailable_requests"] == 0)
unavailable_service.resolve(req["id"], ADMIN)
ok("resolved requests stop counting",
   not assignment_service.is_unavailable(COL["id"], tomorrow))

print("\n[10] history")
h = collection_service.history_for_collector(COL["id"])
ok("history returns this collector's entries", len(h) == 2)
ok("history resolves the owner name",
   any(r["owner_name"] == "Nica Abayon" for r in h))
ok("history is newest first",
   h[0]["timestamp"] >= h[1]["timestamp"])
ok("history filters by search",
   len(collection_service.history_for_collector(COL["id"], search="Iliana")) == 1)
ok("history filters by date",
   len(collection_service.history_for_collector(COL["id"], date="2020-01-01")) == 0)
ok("another collector's history is empty",
   collection_service.history_for_collector(other["id"]) == [])

print("\n[11] deleting a property keeps its history")
before = storage.count("collections")
property_service.delete(in_route2["id"], B1, ADMIN)
ok("collection records survive the property being deleted",
   storage.count("collections") == before)
ok("history still renders a deleted property",
   any(r["owner_name"] == "Deleted property"
       for r in collection_service.history_for_collector(COL["id"])))

print("\n[12] the midnight reset")
from datetime import timedelta
from services import rollover_service

TOMORROW = timeutil.today() + timedelta(days=1)

# -- what needs no reset: status is date-keyed, not a flag ------------------
today_counts = collection_service.counts(route)
before_rows = storage.count("collections")
tomorrow_counts = collection_service.counts(route, TOMORROW)
ok("today has recorded work", today_counts["collected"] + today_counts["not_collected"] > 0)
ok("the next day starts with every property Pending",
   tomorrow_counts["pending"] == tomorrow_counts["total"]
   and tomorrow_counts["collected"] == 0
   and tomorrow_counts["not_collected"] == 0)
ok("and it costs no writes -- nothing is cleared, the date simply moves on",
   storage.count("collections") == before_rows)
ok("today's record is untouched by reading tomorrow",
   collection_service.counts(route) == today_counts)

# -- what does need resetting: duty is a single value, not a dated one ------
duty_service.set_duty(COL["id"], True)
ok("a shift started today is left alone", rollover_service.run() == {"duty_ended": []})
ok("and the collector is still on duty",
   duty_service.is_on_duty(storage.get("users", COL["id"])))

storage.update("users", COL["id"], {
    "duty_changed_at": timeutil.stamp().replace(
        timeutil.today_str(), timeutil.date_str(timeutil.today() - timedelta(days=1))),
    "last_location": {"lat": 9.12, "lng": 125.53, "at": timeutil.stamp()},
}, "test")
ok("a shift left open overnight is found",
   [u["id"] for u in rollover_service.stale_duty()] == [COL["id"]])
ok("the rollover ends it", rollover_service.run()["duty_ended"] == [COL["id"]])
after = storage.get("users", COL["id"])
ok("the collector is off duty", not duty_service.is_on_duty(after))
ok("and yesterday's position is dropped from the live map",
   after.get("last_location") is None)
ok("running it again writes nothing", rollover_service.run() == {"duty_ended": []})

duty_service.set_duty(COL["id"], True)
storage.update("users", COL["id"], {"duty_changed_at": None}, "test")
ok("duty with no timestamp counts as stale, not as today's",
   [u["id"] for u in rollover_service.stale_duty()] == [COL["id"]])
rollover_service.run()

# Carry-overs and collection records must survive the rollover: an
# uncollected property rolling into the next day is the point of that record,
# not stale state to be swept up.
carried = storage.count("carry_overs")
entries = storage.count("collections")
duty_service.set_duty(COL["id"], True)
storage.update("users", COL["id"], {"duty_changed_at": None}, "test")
rollover_service.run()
ok("the rollover leaves carry-overs alone", storage.count("carry_overs") == carried)
ok("and leaves every collection record alone", storage.count("collections") == entries)
ok("and today's figures are unchanged by it",
   collection_service.counts(route) == today_counts)

print("\n[13] tag exemptions -- stops nothing is expected from")
storage.write("collections", [])

# The rule pairs a tag with what is actually being collected. Set the week up
# explicitly so the test does not depend on whichever day it runs on.
BIO = "Biodegradable and Net Residual Waste"
SPECIAL = "Special Waste"
RECYCLE = "Recyclable Waste"

def set_all_days(waste_type, short, details):
    form = {}
    for d in schedule_service.DAYS:
        form[f"waste_type__{d}"] = waste_type
        form[f"short__{d}"] = short
        form[f"tone__{d}"] = "green"
        form[f"details__{d}"] = details
    schedule_service.save_week(form, ADMIN)

composting = property_service.create(
    Form({"owner_name": "Composting House", "type": "House",
          "purok": "Purok 1", "tag": "Composting"}), B1, ADMIN)
no_special = property_service.create(
    Form({"owner_name": "No Special House", "type": "House",
          "purok": "Purok 1", "tag": "No Special Waste"}), B1, ADMIN)
plain = property_service.create(
    Form({"owner_name": "Plain House", "type": "House",
          "purok": "Purok 1", "tag": "None Composting"}), B1, ADMIN)
trio = [composting, no_special, plain]

ok("a tag alone exempts nothing",
   property_service.exemption_for("Composting", None) is None)
ok("composting is exempt on a biodegradable day",
   property_service.exemption_for("Composting", BIO))
ok("but NOT on a recyclable day -- they still put recyclables out",
   property_service.exemption_for("Composting", RECYCLE) is None)
ok("no-special-waste is exempt on a special waste day",
   property_service.exemption_for("No Special Waste", SPECIAL))
ok("but NOT on a biodegradable day",
   property_service.exemption_for("No Special Waste", BIO) is None)
ok("a tag with no rule never exempts",
   property_service.exemption_for("Senior Citizen", BIO) is None
   and property_service.exemption_for("None Composting", BIO) is None)
ok("an untagged property never exempts",
   property_service.exemption_for(None, BIO) is None
   and property_service.exemption_for("", BIO) is None)

set_all_days(BIO, "Biodegradable + Residual", "Kitchen Waste")
rows = {r["owner_name"]: r for r in collection_service.route_with_status(trio)}
ok("the composting house is marked exempt today",
   rows["Composting House"]["exempt"] is True
   and rows["Composting House"]["exempt_note"])
ok("the no-special house is not, on a biodegradable day",
   rows["No Special House"]["exempt"] is False)
ok("the plain house is never exempt", rows["Plain House"]["exempt"] is False)

c = collection_service.counts(trio)
ok("an exempt stop leaves the register total alone", c["total"] == 3)
ok("but comes out of what today expects", c["expected"] == 2 and c["exempt"] == 1)
ok("and is not counted Pending -- nobody is waiting on it", c["pending"] == 2)

# Collect the two that are expected: the round is 100% done, not 67%.
for prop in (no_special, plain):
    collection_service.save_entry(
        Form({"status": "Collected", "qty_0": "1", "unit_0": "Sack"}), None, prop, COL)
c = collection_service.counts(trio)
ok("a round with every expected stop done reads 100%, not 67%",
   c["collected"] == 2 and c["percent"] == 100 and c["pending"] == 0)

# A photo proves a refusal. There is nothing to photograph at a gate that was
# never going to have waste at it.
saved = collection_service.save_entry(
    Form({"status": "Not Collected", "reason": "Nothing expected (property tag)"}),
    None, composting, COL)
ok("an exempt stop can be recorded with no photo",
   saved["status"] == "Not Collected" and saved["image_proof_path"] is None)
ok("once recorded it stops being exempt -- it is a real entry now",
   collection_service.route_with_status([composting])[0]["exempt"] is False)
fails("a non-exempt refusal still demands a photo",
      lambda: collection_service.save_entry(
          Form({"status": "Not Collected", "reason": "No garbage taken out"}),
          None, far_purok, COL), "proof")

# Cleared first: the stops above now hold entries, and a recorded stop counts
# as a real one whatever its tag says.
storage.write("collections", [])
set_all_days(SPECIAL, "Special Waste", "Battery")
rows = {r["owner_name"]: r for r in collection_service.route_with_status(trio)}
ok("on a special waste day the exemption swaps over",
   rows["No Special House"]["exempt"] is True
   and rows["Composting House"]["exempt"] is False)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
