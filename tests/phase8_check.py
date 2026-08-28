"""Phase 8 verification: reports, CSV, dashboard data, hotspots, cleanup."""
import shutil, sys, tempfile
from datetime import timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

ROOT = Path(_ROOT)
tmp = Path(tempfile.mkdtemp(prefix="gcts-p8-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, carryover_service, collection_service,
                      geo_service, history_service, mrf_service,
                      property_service, public_report_service, report_service,
                      schedule_service, storage, timeutil, user_service)
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
YESTERDAY = timeutil.date_str(timeutil.today() - timedelta(days=1))

if not schedule_service.waste_types_for():
    form = {f"{k}__{d}": v for d in schedule_service.DAYS
            for k, v in (("waste_type", "Biodegradable and Net Residual Waste"),
                         ("short", "Biodegradable + Residual"), ("tone", "green"),
                         ("details", "Kitchen Waste\nYard Waste"))}
    schedule_service.save_week(form, ADMIN)

col = user_service.create({"full_name": "Col 8", "username": "col_eight",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1,
    "tricycle_code": "TRI-01", "effective_date": timeutil.today_str(),
    "status": "Active"}), None, ADMIN)
COL = public_view(storage.get("users", col["id"]))

op = user_service.create({"full_name": "Op 8", "username": "op_eight",
                          "role": "truck_collector", "assigned_vehicle": "TRK-01",
                          "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op["id"], "truck_code": "TRK-01", "covered_mrfs": [B1, B2],
    "effective_date": timeutil.today_str(),
    "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
OP = public_view(storage.get("users", op["id"]))

p1 = property_service.create(Form({"owner_name": "Alpha House", "type": "House",
                                   "purok": "Purok 1"}), B1, ADMIN)
p2 = property_service.create(Form({"owner_name": "Beta Store", "type": "Establishment",
                                   "purok": "Purok 1"}), B1, ADMIN)
collection_service.save_entry(Form({"status": "Collected", "qty_0": "10",
                                    "unit_0": "Sack", "qty_1": "4",
                                    "unit_1": "Kilo"}), None, p1, COL)
collection_service.save_entry(Form({"status": "Collected", "qty_0": "5",
                                    "unit_0": "Sack"}), None, p2, COL)
mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B1, OP)
mrf_service.deliver(OP)
mrf_service.save_pickup(Form({"status": "Not Collected",
                              "reason": "Road inaccessible"}), B2, OP)

print("\n[1] report validation")
fails("an unknown report type is refused",
      lambda: report_service.build("nonsense", TODAY, TODAY), "report_type")
fails("a missing date is refused",
      lambda: report_service.build(report_service.PROPERTY_COLLECTION, "", TODAY),
      "start_date")
fails("end before start is refused",
      lambda: report_service.build(report_service.PROPERTY_COLLECTION, TODAY, "2020-01-01"),
      "end_date")
fails("an absurd range is refused",
      lambda: report_service.build(report_service.PROPERTY_COLLECTION,
                                   "2000-01-01", TODAY), "end_date")

print("\n[2] all five report types build")
for key, label in report_service.TYPES:
    r = report_service.build(key, YESTERDAY, TODAY)
    ok(f"{label} builds with columns and meta",
       r["columns"] and r["title"] == label and r["meta"]["days"] == 2)

print("\n[3] property collection figures")
r = report_service.build(report_service.PROPERTY_COLLECTION, TODAY, TODAY)
ok("one row per day", len(r["rows"]) == 1)
ok("collected count matches the entries", r["totals"]["collected"] == 2)
ok("sacks total is right", r["totals"]["sacks"] == 15)
ok("kilos total is right", r["totals"]["kilos"] == 4)
ok("the summary keeps the units apart",
   any("15 sacks and 4 kg" in str(v) for _, v in r["summary"]))
scoped = report_service.build(report_service.PROPERTY_COLLECTION, TODAY, TODAY, B2)
ok("a barangay scope narrows the figures", scoped["totals"]["collected"] == 0)
ok("and names the scope", scoped["meta"]["barangay"] != "All barangays")

print("\n[4] the other four report types")
r = report_service.build(report_service.MRF_COLLECTION, TODAY, TODAY)
ok("MRF report counts collected and missed",
   r["totals"]["collected"] == 1 and r["totals"]["missed"] == 1)
r = report_service.build(report_service.DELIVERIES, TODAY, TODAY)
ok("delivery report has the trip", r["totals"]["trips"] == 1)
ok("with the load", r["totals"]["sacks"] == 15)
r = report_service.build(report_service.CARRY_OVERS, TODAY, TODAY)
ok("carry-over report lists the missed pickup", r["totals"]["pending"] == 1)
r = report_service.build(report_service.COLLECTOR_PERFORMANCE, TODAY, TODAY)
ok("collector performance lists the collector", r["totals"]["collectors"] == 1)
ok("with their entry count", r["rows"][0][5] == 2)

print("\n[5] CSV output")
r = report_service.build(report_service.PROPERTY_COLLECTION, TODAY, TODAY)
csv_text = report_service.to_csv(r)
ok("CSV carries a header block", "Cabadbaran City" in csv_text)
ok("CSV names the date range", r["meta"]["start_display"] in csv_text)
ok("CSV has the column row", ",".join(str(c) for c in r["columns"][:3]) in csv_text)
ok("CSV uses CRLF line endings", "\r\n" in csv_text)
ok("CSV includes the summary", "Summary" in csv_text)
import csv as _csv, io as _io
parsed = list(_csv.reader(_io.StringIO(csv_text)))
ok("CSV parses back cleanly", len(parsed) > 8)
ok("the data row matches the report row",
   any(row[:2] == [str(r["rows"][0][0]), str(r["rows"][0][1])] for row in parsed))
ok("filename carries the type and range",
   report_service.csv_filename(r) ==
   f"property_collection_{TODAY}_to_{TODAY}.csv")

print("\n[6] frozen days keep reports stable")
e = storage.find_one("collections", property_id=p1["id"])
storage.update("collections", e["id"], {"date": YESTERDAY})
history_service.freeze(YESTERDAY, ADMIN)
before = report_service.build(report_service.PROPERTY_COLLECTION,
                              YESTERDAY, YESTERDAY)["totals"]["sacks"]
property_service.delete(p1["id"], B1, ADMIN)
after = report_service.build(report_service.PROPERTY_COLLECTION,
                             YESTERDAY, YESTERDAY)["totals"]["sacks"]
ok("deleting a property does not change a frozen day's report", before == after)

print("\n[7] hotspots in derived mode")
Config.HOTSPOT_LAYER_ENABLED = True
Config.HOTSPOT_SOURCE = "derived"
(Config.GEO_DIR / geo_service.MRFS_FILE).write_text(
    '[{"barangay_id": "brgy-01", "name": "MRF 1", "lat": 9.1226, "lng": 125.5344}]',
    encoding="utf-8")
geo_service.clear_cache()
storage.write("collections", [])
p3 = property_service.create(Form({"owner_name": "Gamma", "type": "House",
                                   "purok": "Purok 1"}), B1, ADMIN)
for _ in range(7):
    storage.insert("collections", {"property_id": p3["id"], "barangay_id": B1,
                                   "purok": "Purok 1", "date": TODAY,
                                   "status": "Not Collected"})
spots = geo_service.hotspots()
ok("the layer is on", spots["meta"]["enabled"] is True)
ok("a hotspot is derived from real data", len(spots["features"]) == 1)
ok("severity reflects the count",
   spots["features"][0]["properties"]["severity"] == "high")
ok("it is honest about how it is placed",
   spots["features"][0]["properties"]["anchored_to"] == "barangay")
ok("geometry is valid GeoJSON lng-first",
   abs(spots["features"][0]["geometry"]["coordinates"][0] - 125.5344) < 0.01)

print("\n[8] the prototype store is gone")
ok("data_store.py has been deleted", not (ROOT / "data_store.py").exists())
offenders = []
for f in list((ROOT / "blueprints").glob("*.py")) + list((ROOT / "services").glob("*.py")) + [ROOT / "app.py"]:
    if "import data_store" in f.read_text(encoding="utf-8"):
        offenders.append(f.name)
ok("no module imports it", not offenders) or print("      ", offenders)

import re
sample_names = ["Nica Abayon", "Iliana Dwane", "Lagrio Mendoza", "Rogelio Ramos",
                "1,496", "1,248"]
hits = []
for f in list(ROOT.glob("*.py")) + list((ROOT / "blueprints").glob("*.py")) + \
         list((ROOT / "services").glob("*.py")):
    text = f.read_text(encoding="utf-8")
    for name in sample_names:
        if name in text and f.name != "seed.py":
            hits.append(f"{f.name}: {name}")
ok("no hardcoded sample data left in the Python", not hits) or print("      ", hits)

tpl_hits = []
for f in (ROOT / "templates").rglob("*.html"):
    text = f.read_text(encoding="utf-8")
    for name in sample_names:
        if name in text:
            tpl_hits.append(f"{f.name}: {name}")
ok("nor in the templates", not tpl_hits) or print("      ", tpl_hits)

print("\n[9] offline queue behaviour is declared honestly")
offline = (ROOT / "static" / "js" / "offline.js").read_text(encoding="utf-8")
ok("the queue expires stale entries", "MAX_AGE_MS" in offline)
ok("photos are explicitly not queued", "cannot be saved offline" in offline)
ok("a 4xx is dropped rather than retried forever", "res.status >= 500" in offline)

print("\n[10] all CDN integrity hashes are present")
base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
assets = (ROOT / "templates" / "partials" / "map_assets.html").read_text(encoding="utf-8")
dash = (ROOT / "templates" / "city-hall-admin" / "dashboard.html").read_text(encoding="utf-8")
ok("socket.io has an integrity hash",
   "socket.io" in base and re.search(r"socket\.io[^>]*integrity=", base, re.S))
ok("leaflet css and js both have one",
   assets.count("integrity=") == 2)
ok("chart.js has one", "chart.umd.min.js" in dash and "integrity=" in dash)
ok("every CDN script is same-version pinned, not @latest",
   "@latest" not in base + assets + dash)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
