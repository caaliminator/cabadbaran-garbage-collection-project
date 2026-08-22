"""Phase 0 verification against a throwaway data dir (never touches data/)."""
import json, shutil, sys, tempfile, threading
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)

from config import Config
tmp = Path(tempfile.mkdtemp(prefix="gcts-"))
Config.DATA_DIR = tmp
Config.GEO_DIR = tmp / "geo"
Config.UPLOAD_DIR = tmp / "uploads"

from services import geo_service, storage, timeutil
import seed

ok = lambda label, cond: print(f"  {'PASS' if cond else 'FAIL':<4} {label}") or cond
results = []

print("\n[1] seed into empty dir")
seed.run()
results.append(ok("31 barangays", storage.count("barangays") == 31))
results.append(ok("31 tricycles", storage.count("vehicles", type="tricycle") == 31))
results.append(ok("8 trucks (city ceiling)", storage.count("vehicles", type="truck") == 8))
results.append(ok("7 schedule days", storage.count("waste_schedule") == 7))
admin = storage.find_one("users", role="city_admin")
results.append(ok("admin password is hashed, not plaintext",
                  admin["password_hash"].startswith(("pbkdf2:", "scrypt:"))
                  and "password" not in admin))
results.append(ok("zone groups span 1-31",
                  {geo_service.zone_group_for(n)["key"] for n in range(1, 32)} ==
                  {"zone-a", "zone-b", "zone-c", "zone-d"}))

print("\n[2] graceful degradation")
geo_service.clear_cache()
(Config.GEO_DIR / geo_service.ZONES_FILE).unlink()
z = geo_service.barangay_zones()
results.append(ok("missing zones file -> empty, no crash",
                  z["features"] == [] and z["meta"]["problem"] == "missing"))

(Config.GEO_DIR / geo_service.MRFS_FILE).write_text("{ this is not json", encoding="utf-8")
geo_service.clear_cache()
m = geo_service.mrf_locations()
results.append(ok("malformed mrf file -> empty + reason",
                  m["mrfs"] == [] and "invalid JSON" in (m["meta"]["problem"] or "")))

(Config.GEO_DIR / geo_service.HOTSPOTS_FILE).write_text("", encoding="utf-8")
geo_service.clear_cache()
Config.HOTSPOT_LAYER_ENABLED = True
Config.HOTSPOT_SOURCE = "file"
h = geo_service.hotspots()
results.append(ok("empty hotspot file -> empty state", h["features"] == [] and h["meta"]["problem"]))

print("\n[3] derived hotspots with real coordinates")
# Supply coordinates for two barangays only -- the third stays unplaced.
(Config.GEO_DIR / geo_service.MRFS_FILE).write_text(json.dumps([
    {"barangay_id": "brgy-01", "name": "Antonio Luna MRF", "lat": 9.13, "lng": 125.54},
    {"barangay_id": "brgy-02", "name": "Bay-ang MRF", "lat": 9.15, "lng": 125.56},
]), encoding="utf-8")
geo_service.clear_cache()
Config.HOTSPOT_SOURCE = "derived"
today = timeutil.today_str()
for i in range(7):
    storage.insert("collections", {"property_id": f"prop-{i}", "barangay_id": "brgy-01",
                                   "purok": "Purok 2", "date": today, "status": "Not Collected"})
for i in range(2):
    storage.insert("collections", {"property_id": f"prop-x{i}", "barangay_id": "brgy-02",
                                   "purok": "Purok 1", "date": today, "status": "Not Collected"})
storage.insert("public_reports", {"barangay_id": "brgy-03", "purok": "Purok 1",
                                  "date": today, "status_reported": "Not Collected"})
h = geo_service.hotspots()
sev = {f["properties"]["barangay_id"]: f["properties"]["severity"] for f in h["features"]}
results.append(ok("7 hits -> high severity", sev.get("brgy-01") == "high"))
results.append(ok("2 hits -> low severity", sev.get("brgy-02") == "low"))
results.append(ok("uncoordinated barangay counted as unplaced, not dropped silently",
                  h["meta"]["unplaced"] == 1))
results.append(ok("severity filter works",
                  len(geo_service.hotspots(severity="high")["features"]) == 1))
results.append(ok("barangay filter works",
                  len(geo_service.hotspots(barangay="brgy-02")["features"]) == 1))
results.append(ok("layer flag off -> empty regardless of data",
                  (setattr(Config, "HOTSPOT_LAYER_ENABLED", False),
                   geo_service.hotspots()["features"] == [])[1]))
Config.HOTSPOT_LAYER_ENABLED = True

print("\n[4] storage concurrency + atomicity")
storage.write("notifications", [])
def hammer(n):
    for i in range(40):
        storage.insert("notifications", {"who": n, "i": i})
threads = [threading.Thread(target=hammer, args=(t,)) for t in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
rows = storage.read("notifications")
ids = [r["id"] for r in rows]
results.append(ok(f"320 concurrent inserts all survived (got {len(rows)})", len(rows) == 320))
results.append(ok("no duplicate ids under contention", len(set(ids)) == len(ids)))
results.append(ok("audit fields stamped", all(r.get("created_at") for r in rows)))

storage.write("history", [{"id": "hist-0001", "a": 1}])
try:
    with storage.transaction("history") as rows:
        rows.append({"id": "hist-0002"})
        raise RuntimeError("boom")
except RuntimeError:
    pass
results.append(ok("failed transaction rolls back", len(storage.read("history")) == 1))
results.append(ok("no temp files left behind",
                  not list(Path(Config.DATA_DIR).glob(".*tmp"))))

print("\n[5] idempotency of a second seed over live data")
storage.insert("users", {"username": "someone", "role": "barangay_admin"})
seed.run()
results.append(ok("no duplicate barangays", storage.count("barangays") == 31))
results.append(ok("hand-added user untouched", storage.count("users", username="someone") == 1))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(1 for r in results if r)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
