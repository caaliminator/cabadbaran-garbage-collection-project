"""Phase 6 verification: public viewer, anonymous reports, rate limit, disputes."""
import shutil, sys, tempfile
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p6-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, collection_service, property_service,
                      public_report_service, schedule_service, storage,
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
B1, B2 = "brgy-01", "brgy-02"
TODAY = timeutil.today_str()

if not schedule_service.waste_types_for():
    form = {f"{k}__{d}": v for d in schedule_service.DAYS
            for k, v in (("waste_type", "Biodegradable and Net Residual Waste"),
                         ("short", "Biodegradable + Residual"), ("tone", "green"),
                         ("details", "Kitchen Waste\nYard Waste"))}
    schedule_service.save_week(form, ADMIN)

col = user_service.create({"full_name": "Col P6", "username": "col_p6",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1, "purok_coverage": ["Purok 1"],
    "tricycle_code": "TRI-01", "status": "Active"}), None, ADMIN)
COL = public_view(storage.get("users", col["id"]))

p1 = property_service.create(Form({"owner_name": "Nica Abayon", "type": "House",
                                   "purok": "Purok 1"}), B1, ADMIN)
p2 = property_service.create(Form({"owner_name": "Iliana Dwane", "type": "House",
                                   "purok": "Purok 2"}), B1, ADMIN)
p3 = property_service.create(Form({"owner_name": "Other Barangay", "type": "House",
                                   "purok": "Purok 1"}), B2, ADMIN)

IP_A, IP_B = "203.0.113.10", "203.0.113.99"

print("\n[1] report form options")
ok("barangay options list all 31", len(public_report_service.barangay_options()) == 31)
ok("puroks list only those with properties",
   set(public_report_service.purok_options(B1)) == {"Purok 1", "Purok 2"})
ok("properties are scoped to the barangay",
   {p["id"] for p in public_report_service.property_options(B1)} == {p1["id"], p2["id"]})
ok("purok narrows the list",
   [p["id"] for p in public_report_service.property_options(B1, "Purok 2")] == [p2["id"]])
ok("search narrows the list",
   [p["id"] for p in public_report_service.property_options(B1, search="nica")] == [p1["id"]])
opts = public_report_service.property_options(B1)
ok("options expose only name, type and purok -- no tags or notes",
   all(set(o) == {"id", "owner_name", "type", "purok"} for o in opts))

print("\n[2] filing a report")
r1 = public_report_service.submit(Form({"barangay_id": B1, "property_id": p1["id"],
                                        "status_reported": "Not Collected",
                                        "comment": "Nobody came"}), IP_A)
ok("report stored", r1["status_reported"] == "Not Collected")
ok("report is not flagged disputed when nothing to conflict with",
   r1["disputed"] is False)
ok("an entry was created from the report",
   collection_service.status_for(p1["id"]) == "Not Collected")
entry = collection_service.entry_for(p1["id"])
ok("the entry is marked as coming from a resident",
   entry["source"] == collection_service.SOURCE_PUBLIC
   and entry["reason"] == "Reported by resident")
ok("no collector is attributed", entry["collector_id"] is None)

fails("a property from another barangay is refused",
      lambda: public_report_service.submit(
          Form({"barangay_id": B1, "property_id": p3["id"],
                "status_reported": "Collected"}), IP_A), "property_id")
fails("an unknown property is refused",
      lambda: public_report_service.submit(
          Form({"barangay_id": B1, "property_id": "prop-9999",
                "status_reported": "Collected"}), IP_A), "property_id")
fails("an invented status is refused",
      lambda: public_report_service.submit(
          Form({"barangay_id": B1, "property_id": p2["id"],
                "status_reported": "Maybe"}), IP_A), "status_reported")

print("\n[3] rate limiting")
ok("remaining count drops as reports are filed",
   public_report_service.remaining_today(IP_A) == Config.PUBLIC_REPORT_DAILY_LIMIT - 1)
for i in range(Config.PUBLIC_REPORT_DAILY_LIMIT - 1):
    public_report_service.submit(Form({"barangay_id": B1, "property_id": p2["id"],
                                       "status_reported": "Collected"}), IP_A)
ok("limit is reached", public_report_service.remaining_today(IP_A) == 0)
fails("the next report is refused",
      lambda: public_report_service.submit(
          Form({"barangay_id": B1, "property_id": p2["id"],
                "status_reported": "Collected"}), IP_A),
      "form", "already sent")
ok("a different device is unaffected",
   public_report_service.remaining_today(IP_B) == Config.PUBLIC_REPORT_DAILY_LIMIT)
r_b = public_report_service.submit(Form({"barangay_id": B1, "property_id": p2["id"],
                                         "status_reported": "Collected"}), IP_B)
ok("and can still report", r_b is not None)

print("\n[4] the IP is never stored")
raw = (Config.DATA_DIR / "public_reports.json").read_text(encoding="utf-8")
ok("no raw IP address appears in the stored data",
   IP_A not in raw and IP_B not in raw)
ok("the device key is a hash, not the address",
   all(len(r["device_key"]) == 32 and IP_A not in r["device_key"]
       for r in storage.read("public_reports")))
ok("the same device hashes consistently",
   public_report_service._device_key(IP_A) == public_report_service._device_key(IP_A))
ok("different devices hash differently",
   public_report_service._device_key(IP_A) != public_report_service._device_key(IP_B))
ok("the device key never reaches an admin view",
   all(r["device_key"] is None for r in public_report_service.listing()))

print("\n[5] a resident never overwrites a collector -- Disputed")
storage.write("public_reports", [])
storage.write("collections", [])
saved = collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "12", "unit_0": "Sack",
          "note": "Collected at the gate"}), None, p1, COL)
before_status = saved["status"]
before_waste = list(saved["waste"])

report = public_report_service.submit(
    Form({"barangay_id": B1, "property_id": p1["id"],
          "status_reported": "Not Collected", "comment": "It was still there"}),
    IP_B)
entry = collection_service.entry_for(p1["id"])

ok("the report is flagged as disputed", report["disputed"] is True)
ok("the collector's status is UNCHANGED", entry["status"] == before_status)
ok("the collector's load is UNCHANGED", entry["waste"] == before_waste)
ok("the collector's own note survives", entry["note"] == "Collected at the gate")
ok("the entry is flagged Disputed", entry["disputed"] is True)
ok("the dispute note records both accounts",
   "Not Collected" in entry["dispute_note"] and "Collected" in entry["dispute_note"])
ok("both records still exist",
   storage.count("public_reports") == 1 and storage.count("collections") == 1)
ok("the barangay sees one open dispute",
   public_report_service.dispute_count(B1) == 1)

print("\n[6] agreeing reports raise no dispute")
storage.write("public_reports", [])
storage.update("collections", entry["id"], {"disputed": False, "dispute_note": None})
agree = public_report_service.submit(
    Form({"barangay_id": B1, "property_id": p1["id"],
          "status_reported": "Collected"}), IP_B)
ok("no dispute when the resident agrees", agree["disputed"] is False)
ok("the entry is not flagged",
   collection_service.entry_for(p1["id"])["disputed"] is False)
ok("but it is noted that a resident confirmed it",
   collection_service.entry_for(p1["id"])["reported_by_resident"] is True)

print("\n[7] a resident-created entry is not 'disputed' by a second report")
storage.write("collections", [])
storage.write("public_reports", [])
public_report_service.submit(Form({"barangay_id": B1, "property_id": p2["id"],
                                   "status_reported": "Not Collected"}), IP_A)
second = public_report_service.submit(Form({"barangay_id": B1, "property_id": p2["id"],
                                            "status_reported": "Collected"}), IP_B)
ok("two residents disagreeing does not create a collector dispute",
   second["disputed"] is False)

print("\n[8] resolving a dispute")
storage.write("collections", [])
storage.write("public_reports", [])
saved = collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "5", "unit_0": "Sack"}), None, p1, COL)
public_report_service.submit(Form({"barangay_id": B1, "property_id": p1["id"],
                                   "status_reported": "Not Collected"}), IP_B)
entry_id = collection_service.entry_for(p1["id"])["id"]

resolved = public_report_service.resolve_dispute(entry_id, "collector", ADMIN)
ok("upholding the collector clears the flag and keeps the status",
   resolved["disputed"] is False and resolved["status"] == "Collected")
ok("the resolution is recorded", "upheld" in resolved["dispute_resolution"])
fails("an already-resolved record cannot be resolved again",
      lambda: public_report_service.resolve_dispute(entry_id, "resident", ADMIN),
      "form", "not disputed")

public_report_service.submit(Form({"barangay_id": B1, "property_id": p1["id"],
                                   "status_reported": "Not Collected"}), IP_A)
entry_id = collection_service.entry_for(p1["id"])["id"]
resolved = public_report_service.resolve_dispute(entry_id, "resident", ADMIN)
ok("upholding the resident flips the status",
   resolved["status"] == "Not Collected" and resolved["disputed"] is False)
ok("and clears the load that is no longer claimed", resolved["waste"] == [])
fails("an invalid choice is refused",
      lambda: public_report_service.resolve_dispute(entry_id, "nobody", ADMIN))

print("\n[9] counters see resident reports")
storage.write("collections", [])
storage.write("public_reports", [])
public_report_service.submit(Form({"barangay_id": B1, "property_id": p1["id"],
                                   "status_reported": "Not Collected"}), IP_A)
props = property_service.listing(barangay_id=B1)
counts = collection_service.counts(props)
ok("a resident report counts toward Not Collected", counts["not_collected"] == 1)
ok("and reduces Pending", counts["pending"] == len(props) - 1)
ok("listing filters by barangay",
   len(public_report_service.listing(barangay_id=B2)) == 0)
ok("listing filters to disputes only",
   public_report_service.listing(disputed_only=True) == [])

print("\n[10] public schedule pages read the real schedule")
cal = schedule_service.month_calendar()
ok("calendar has weeks of seven days",
   all(len(w) == 7 for w in cal["weeks"]) and len(cal["weeks"]) >= 4)
ok("today is marked once",
   sum(1 for w in cal["weeks"] for c in w if c["is_today"]) == 1)
ok("upcoming days are labelled",
   schedule_service.upcoming_days(3)[0]["label"] == "Today"
   and schedule_service.upcoming_days(3)[1]["label"] == "Tomorrow")

form = {f"{k}__{d}": v for d in schedule_service.DAYS
        for k, v in (("waste_type", "Recyclable Waste"), ("short", "Recyclable"),
                     ("tone", "blue"), ("details", "Plastic\nGlass"))}
schedule_service.save_week(form, ADMIN)
ok("editing the schedule changes the public calendar",
   all(c["short"] == "Recyclable" for w in schedule_service.month_calendar()["weeks"]
       for c in w))
ok("today's cards follow the schedule",
   schedule_service.todays_cards()[0]["items"] == ["Plastic", "Glass"])

form = {f"{k}__{d}": v for d in schedule_service.DAYS
        for k, v in (("waste_type", "No Collection"), ("short", "No Collection"),
                     ("tone", "muted"), ("details", ""))}
schedule_service.save_week(form, ADMIN)
ok("a no-collection day yields no cards", schedule_service.todays_cards() == [])
ok("and no calendar dots",
   not any(c["has_collection"] for w in schedule_service.month_calendar()["weeks"]
           for c in w))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
