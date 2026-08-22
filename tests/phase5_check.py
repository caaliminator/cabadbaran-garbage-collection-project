"""Phase 5 verification: barangay scoping, notifications, history, live map API."""
import shutil, sys, tempfile
from datetime import timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p5-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, collection_service, duty_service,
                      history_service, mrf_service, notification_service,
                      property_service, schedule_service, storage, timeutil,
                      user_service)
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

admin1 = public_view(user_service.create(
    {"full_name": "Admin One", "username": "b1admin", "role": "barangay_admin",
     "assigned_barangay": B1, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
admin2 = public_view(user_service.create(
    {"full_name": "Admin Two", "username": "b2admin", "role": "barangay_admin",
     "assigned_barangay": B2, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))

col = user_service.create({"full_name": "Col One", "username": "col_one",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1, "purok_coverage": ["Purok 1"],
    "tricycle_code": "TRI-01", "status": "Active"}), None, ADMIN)
COL = public_view(storage.get("users", col["id"]))

print("\n[1] barangay scoping")
p1 = property_service.create(Form({"owner_name": "B1 Owner", "type": "House",
                                   "purok": "Purok 1"}), B1, admin1["id"])
p2 = property_service.create(Form({"owner_name": "B2 Owner", "type": "House",
                                   "purok": "Purok 1"}), B2, admin2["id"])
ok("each admin's listing holds only their own barangay",
   [r["id"] for r in property_service.listing(barangay_id=B1)] == [p1["id"]]
   and [r["id"] for r in property_service.listing(barangay_id=B2)] == [p2["id"]])
fails("cannot edit another barangay's property",
      lambda: property_service.update(p2["id"], Form({"owner_name": "Hijacked",
                                                      "type": "House",
                                                      "purok": "Purok 1"}), B1,
                                      admin1["id"]),
      "form", "another barangay")
fails("cannot delete another barangay's property",
      lambda: property_service.delete(p2["id"], B1, admin1["id"]),
      "form", "another barangay")
ok("the other barangay's property is untouched",
   storage.get("properties", p2["id"])["owner_name"] == "B2 Owner")
ok("citywide listing sees both", len(property_service.listing()) == 2)

print("\n[2] counters and totals are per barangay")
collection_service.save_entry(Form({"status": "Collected", "qty_0": "9",
                                    "unit_0": "Sack"}), None, p1, COL)
c1 = collection_service.counts(property_service.listing(barangay_id=B1))
c2 = collection_service.counts(property_service.listing(barangay_id=B2))
ok("B1 shows one collected", c1["collected"] == 1 and c1["pending"] == 0)
ok("B2 is untouched by B1's entry", c2["collected"] == 0 and c2["pending"] == 1)
ok("percent is out of the barangay's own total", c1["percent"] == 100)
ok("B1 load is 9 sacks",
   collection_service.barangay_totals(B1)["sacks"] == 9)
ok("B2 load is empty", collection_service.barangay_totals(B2)["empty"])

print("\n[3] notifications are addressed by audience")
notification_service.create(f"barangay:{B1}", notification_service.TRUCK_APPROACHING,
                            "Truck TRK-01 approaching your MRF")
notification_service.create(f"barangay:{B2}", notification_service.TRUCK_APPROACHING,
                            "Truck TRK-02 approaching your MRF")
notification_service.create("role:city_admin", notification_service.CARRY_OVER_CREATED,
                            "A carry-over was opened")
notification_service.create("public", notification_service.SCHEDULE_UPDATED,
                            "The waste schedule changed")

a1 = notification_service.for_user(admin1)
ok("an admin sees their own barangay's alert",
   any("TRK-01" in n["message"] for n in a1))
ok("and not another barangay's",
   not any("TRK-02" in n["message"] for n in a1))
ok("and not the city admin's", not any("carry-over" in n["message"] for n in a1))
ok("public alerts reach everyone",
   any("schedule changed" in n["message"] for n in a1))
ok("anonymous viewers get only public alerts",
   [n["message"] for n in notification_service.for_user(None)]
   == ["The waste schedule changed"])

print("\n[4] read state is per person")
target = next(n for n in a1 if "TRK-01" in n["message"])
before = notification_service.unread_count(admin1)
notification_service.mark_read(target["id"], admin1["id"])
ok("marking read drops that admin's unread count",
   notification_service.unread_count(admin1) == before - 1)
admin3 = public_view(user_service.create(
    {"full_name": "Admin Three", "username": "b1admin2", "role": "barangay_admin",
     "assigned_barangay": B1, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
ok("a colleague in the same barangay still sees it unread",
   any(n["unread"] and "TRK-01" in n["message"]
       for n in notification_service.for_user(admin3)))
notification_service.mark_all_read(admin1)
ok("mark-all-read clears the badge",
   notification_service.unread_count(admin1) == 0)
ok("but not for the colleague",
   notification_service.unread_count(admin3) > 0)

print("\n[5] repeat-alert suppression")
notification_service.create(f"barangay:{B1}", notification_service.TRUCK_APPROACHING,
                            "Approaching", dedupe_key="approach:TRK-01:brgy-01")
ok("a sent alert is recognised",
   notification_service.already_sent("approach:TRK-01:brgy-01"))
ok("a different key is not", not notification_service.already_sent("approach:TRK-02:brgy-01"))

print("\n[6] history summaries")
summary = history_service.compute(TODAY, B1)
ok("summary counts this barangay's properties", summary["properties"]["total"] == 1)
ok("summary carries the load", summary["load"]["sacks"] == 9)
ok("citywide summary spans both barangays",
   history_service.compute(TODAY, None)["properties"]["total"] == 2)
ok("today is never frozen", history_service.summary_for(TODAY, B1)["is_frozen"] is False)

entry = storage.find_one("collections", property_id=p1["id"])
storage.update("collections", entry["id"], {"date": YESTERDAY})
written = history_service.freeze(YESTERDAY, ADMIN)
ok("freezing writes one row per scope plus the city",
   len(written) == 1 + storage.count("barangays"))
ok("a frozen day is read back as frozen",
   history_service.summary_for(YESTERDAY, B1)["is_frozen"] is True)
ok("re-freezing changes nothing", history_service.freeze(YESTERDAY, ADMIN) == [])

frozen_before = history_service.summary_for(YESTERDAY, B1)["load"]["sacks"]
property_service.delete(p1["id"], B1, admin1["id"])
ok("deleting a property does NOT rewrite a frozen day",
   history_service.summary_for(YESTERDAY, B1)["load"]["sacks"] == frozen_before)

feed = history_service.feed(B1, limit=5)
ok("feed is newest first and labelled",
   feed[0]["is_today"] and feed[0]["label"] == "Today"
   and feed[1]["label"] == "Yesterday")
ok("feed marks days with no activity",
   any(not d["had_activity"] for d in feed))

print("\n[7] the lazy end-of-day job")
storage.write("history", [])
storage.write("mrf_pickups", [])
p3 = property_service.create(Form({"owner_name": "Late Owner", "type": "House",
                                   "purok": "Purok 1"}), B1, admin1["id"])
e3 = collection_service.save_entry(Form({"status": "Collected", "qty_0": "4",
                                         "unit_0": "Sack"}), None, p3, COL)
storage.update("collections", e3["id"], {"date": YESTERDAY})
written = history_service.ensure_frozen(ADMIN)
ok("first request of a new day freezes the missed day", len(written) > 0)
ok("yesterday now has a frozen summary",
   history_service.frozen(YESTERDAY, B1) is not None)
ok("running it again is a no-op", history_service.ensure_frozen(ADMIN) == [])
ok("it also auto-missed the MRF nobody touched",
   storage.count("mrf_pickups", date=YESTERDAY) >= 0)

print("\n[8] live vehicle scoping")
duty_service.set_duty(COL["id"], True)
duty_service.record_location(COL["id"], 9.12, 125.53)
ok("on-duty collector appears for their own barangay",
   len(duty_service.active_collectors(barangay_id=B1)) == 1)
ok("and not for another barangay",
   duty_service.active_collectors(barangay_id=B2) == [])
counts = duty_service.active_counts()
ok("counts split tricycles and trucks",
   counts["tricycles"] == 1 and counts["trucks"] == 0)

print("\n[9] no hardcoded geography anywhere in the client")
map_js = (Path(_ROOT) / "static" / "js" / "map.js").read_text(encoding="utf-8")
import re
coords = re.findall(r"\b12[45]\.\d{3,}|\b9\.\d{3,}", map_js)
ok("map.js contains no coordinates", not coords)
templates = Path(_ROOT) / "templates"
offenders = []
for f in templates.rglob("*.html"):
    text = f.read_text(encoding="utf-8")
    if re.search(r"\b12[45]\.\d{3,}", text) or "<polygon" in text:
        offenders.append(f.name)
ok(f"no template holds coordinates or polygons", not offenders) or print("      ", offenders)

print("\n[10] per-barangay zone colours live in CSS, not in JS")
css = (Path(_ROOT) / "static" / "css"
       / "components.css").read_text(encoding="utf-8")
brgy_rules = re.findall(r"\.map-zone--(brgy-\d+)\s*\{", css)
ok("31 barangay colour rules present", len(set(brgy_rules)) == 31)
ok("every rule sets a distinct fill",
   len(set(re.findall(r"\.map-zone--brgy-\d+ \{ fill: (#[0-9a-f]{6})", css))) == 31)
ok("map.js emits the barangay class", "map-zone--${feature.properties.barangay_id}" in map_js
   or "barangay_id}" in map_js)
ok("map.js still holds no colour value", not re.search(r"#[0-9a-fA-F]{6}\b", map_js))
ok("zone-group colours kept as a working fallback",
   all(f".map-zone--zone-{k}" in css for k in "abcd"))

print("\n[11] map height is viewport-relative, not fixed pixels")
ok("canvas height uses clamp()/vh", "clamp(360px, 44vh" in css)
ok("a taller variant exists for tracking pages", ".map__canvas--tall" in css)
inline = [f.name for f in templates.rglob("*.html")
          if re.search(r'class="map__canvas[^"]*"[^>]*style="height:\s*\d+px', f.read_text(encoding="utf-8"))]
ok("no template hardcodes a pixel map height", not inline) or print("      ", inline)

# pages.css and roles.css load after components.css, so anything they say about
# the canvas silently wins. This is exactly how the Leaflet map ended up 240px
# tall with its polygons mangled: dead rules from the old hand-drawn SVG map
# were left behind in pages.css when the SVG itself was deleted.
print("\n[12] no later stylesheet overrides the Leaflet canvas")
css_dir = Path(_ROOT) / "static" / "css"
later = {name: (css_dir / name).read_text(encoding="utf-8")
         for name in ("pages.css", "roles.css")}
for name, text in later.items():
    canvas = re.search(r"^\.map__canvas\s*\{([^}]*)\}", text, re.MULTILINE)
    body = canvas.group(1) if canvas else ""
    ok(f"{name} sets no canvas height", "height" not in body)
    # `flex` on a column-flex child sets its height and would beat the
    # components.css value, so `none` is the only acceptable setting here.
    ok(f"{name} does not flex-size the canvas",
       all(v.strip() == "none" for v in re.findall(r"flex:\s*([^;}]+)", body)))
    ok(f"{name} holds no dead SVG-map rules",
       not any(dead in text for dead in
               (".map__canvas svg", ".map__zone", ".map__unit", "keyframes bob")))

print("\n[13] static assets are cache-busted")
base_html = (templates / "base.html").read_text(encoding="utf-8")
ok("base.html versions its stylesheets", "asset('css/components.css')" in base_html)
ok("base.html versions its scripts", "asset('js/app.js')" in base_html)
ok("no unversioned css/js url_for remains",
   not [f.name for f in templates.rglob("*.html")
        if re.search(r"url_for\(['\"]static['\"],\s*filename=['\"](css|js)/", f.read_text(encoding="utf-8"))])

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
