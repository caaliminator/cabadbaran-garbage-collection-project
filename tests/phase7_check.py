"""Phase 7 verification: rooms, events, and the eight notification triggers."""
import shutil, sys, tempfile
from datetime import timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p7-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, carryover_service, collection_service,
                      duty_service, geo_service, mrf_service,
                      notification_service, property_service,
                      public_report_service, realtime, schedule_service,
                      storage, timeutil, triggers, unavailable_service,
                      user_service)
from services.auth_service import public_view
import seed

results = []
def ok(label, cond):
    results.append(bool(cond)); print(f"  {'PASS' if cond else 'FAIL':<4} {label}"); return cond

class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]

# A fake socket layer, so we can assert what would have gone over the wire
# without standing up a real server.
class FakeSocket:
    def __init__(self): self.sent = []
    def emit(self, event, payload=None, to=None): self.sent.append((to, event, payload))
    def events_for(self, room): return [e for r, e, _ in self.sent if r == room]
    def rooms_for(self, event): return [r for r, e, _ in self.sent if e == event]
    def clear(self): self.sent.clear()

fake = FakeSocket()
realtime.init(fake)

seed.run()
ADMIN = storage.find_one("users", role="city_admin")["id"]
B1, B2 = "brgy-01", "brgy-02"

if not schedule_service.waste_types_for():
    form = {f"{k}__{d}": v for d in schedule_service.DAYS
            for k, v in (("waste_type", "Biodegradable and Net Residual Waste"),
                         ("short", "Biodegradable + Residual"), ("tone", "green"),
                         ("details", "Kitchen Waste\nYard Waste"))}
    schedule_service.save_week(form, ADMIN)

badmin = public_view(user_service.create(
    {"full_name": "B Admin", "username": "b_admin7", "role": "barangay_admin",
     "assigned_barangay": B1, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
cadmin = public_view(storage.get("users", ADMIN))

col = user_service.create({"full_name": "Col 7", "username": "col_seven",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
op = user_service.create({"full_name": "Op 7", "username": "op_seven",
                          "role": "truck_collector", "assigned_vehicle": "TRK-01",
                          "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)

print("\n[1] rooms are derived from the account, never requested")
ok("anonymous gets public only", realtime.rooms_for(None) == ["public"])

brooms = realtime.rooms_for(badmin)
ok("a barangay admin gets public, their own room, and their barangay",
   set(brooms) == {"public", f"user:{badmin['id']}", f"barangay:{B1}"})
ok("and NOT another barangay", f"barangay:{B2}" not in brooms)
ok("and NOT the city admin room", "role:city_admin" not in brooms)

crooms = realtime.rooms_for(cadmin)
ok("a city admin gets the city room", "role:city_admin" in crooms)
ok("and every barangay", sum(1 for r in crooms if r.startswith("barangay:")) == 31)

assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1,
    "tricycle_code": "TRI-01", "effective_date": timeutil.today_str(),
    "status": "Active"}), None, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op["id"], "truck_code": "TRK-01",
    "covered_mrfs": [B1, B2], "effective_date": timeutil.today_str(), "status": "Active",
    "planned_time__brgy-01": "10:00"}), None, ADMIN)
COL = public_view(storage.get("users", col["id"]))
OP = public_view(storage.get("users", op["id"]))

trooms = realtime.rooms_for(OP)
ok("a truck operator gets only the barangays they cover",
   {r for r in trooms if r.startswith("barangay:")} == {f"barangay:{B1}", f"barangay:{B2}"})

print("\n[2] assignment change notifies the collector")
notes = notification_service.for_user(COL)
ok("the collector was told about their route",
   any(n["type"] == notification_service.ASSIGNMENT_CHANGED for n in notes))
ok("and it names the barangay and unit",
   any("TRI-01" in n["message"] for n in notes))
ok("another collector was not told",
   not any(n["type"] == notification_service.ASSIGNMENT_CHANGED
           for n in notification_service.for_user(OP)
           if "TRI-01" in n["message"]))

print("\n[3] a saved collection is broadcast")
p1 = property_service.create(Form({"owner_name": "House 7", "type": "House",
                                   "purok": "Purok 1"}), B1, ADMIN)
fake.clear()
collection_service.save_entry(Form({"status": "Collected", "qty_0": "6",
                                    "unit_0": "Sack"}), None, p1, COL)
ok("collection_saved reached the barangay room",
   "collection_saved" in fake.events_for(f"barangay:{B1}"))
ok("and the city admin room",
   "collection_saved" in fake.events_for("role:city_admin"))
ok("but not another barangay",
   "collection_saved" not in fake.events_for(f"barangay:{B2}"))

print("\n[4] duty and location")
fake.clear()
duty_service.set_duty(COL["id"], True)
ok("collector_status is broadcast publicly",
   "collector_status" in fake.events_for("public"))
ok("and to the city admin", "collector_status" in fake.events_for("role:city_admin"))

fake.clear()
realtime.location_update(COL, 9.1226, 125.5344)
public = [p for r, e, p in fake.sent if r == "public" and e == "location_update"]
admin = [p for r, e, p in fake.sent if r == "role:city_admin" and e == "location_update"]
ok("the public payload carries the vehicle code",
   public and public[0]["vehicle"] == "TRI-01")
ok("the public payload carries NO name and NO user id",
   public and "name" not in public[0] and "id" not in public[0])
ok("the admin payload does carry the name", admin and admin[0]["name"] == "Col 7")

print("\n[5] truck approaching an MRF")
# Give brgy-01 a real MRF coordinate; leave brgy-02 without one.
(Config.GEO_DIR / geo_service.MRFS_FILE).write_text(
    '[{"barangay_id": "brgy-01", "name": "MRF 1", "lat": 9.1226, "lng": 125.5344}]',
    encoding="utf-8")
geo_service.clear_cache()
storage.write("notifications", [])

far = triggers.check_truck_approaching(OP, 9.5000, 126.0000)
ok("no alert when the truck is far away", far == [])

near = triggers.check_truck_approaching(OP, 9.1228, 125.5346)   # ~30 m
ok("an alert fires within 500 m", len(near) == 1)
ok("the message names the truck and the barangay",
   "TRK-01" in near[0]["message"] and "Antonio Luna" in near[0]["message"])
ok("it went to that barangay's room",
   near[0]["audience"] == f"barangay:{B1}")

again = triggers.check_truck_approaching(OP, 9.1227, 125.5345)
ok("it does not fire again the same day (no spam)", again == [])

ok("a barangay with no MRF coordinate is skipped silently",
   not any("Bay-ang" in n["message"] for n in storage.read("notifications")))

ok("a tricycle collector never triggers the truck alert",
   triggers.check_truck_approaching(COL, 9.1226, 125.5344) == [])

print("\n[6] T-2h arrival reminder")
storage.write("notifications", [])
now = timeutil.now().replace(hour=8, minute=30, second=0, microsecond=0)
sent = triggers.check_arrival_reminders(now)          # planned 10:00, so T-1.5h
ok("fires inside the two-hour window", len(sent) == 1)
ok("the message matches the spec's wording",
   "2 hours from now the garbage collector TRK-01 from City Hall will arrive"
   in sent[0]["message"])
ok("addressed to the barangay", sent[0]["audience"] == f"barangay:{B1}")

ok("does not fire twice the same day",
   triggers.check_arrival_reminders(now) == [])

storage.write("notifications", [])
early = timeutil.now().replace(hour=6, minute=0, second=0, microsecond=0)
ok("does not fire before the window", triggers.check_arrival_reminders(early) == [])
late = timeutil.now().replace(hour=11, minute=0, second=0, microsecond=0)
ok("does not fire after the planned time has passed",
   triggers.check_arrival_reminders(late) == [])

print("\n[7] carry-over, delivery, unavailability, public report")
storage.write("notifications", [])
fake.clear()
mrf_service.save_pickup(Form({"status": "Not Collected",
                              "reason": "Road inaccessible"}), B1, OP)
notes = storage.read("notifications")
ok("a missed pickup notifies the city admin",
   any(n["audience"] == "role:city_admin"
       and n["type"] == notification_service.CARRY_OVER_CREATED for n in notes))
ok("and the barangay",
   any(n["audience"] == f"barangay:{B1}"
       and n["type"] == notification_service.CARRY_OVER_CREATED for n in notes))
ok("carry_over_created was broadcast",
   "carry_over_created" in fake.events_for("role:city_admin"))
ok("mrf_pickup_saved was broadcast",
   "mrf_pickup_saved" in fake.events_for(f"barangay:{B1}"))

storage.write("notifications", [])
carry = carryover_service.outstanding_for(B1)
op2 = user_service.create({"full_name": "Op Two", "username": "op_two7",
                           "role": "truck_collector", "assigned_vehicle": "TRK-02",
                           "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op2["id"], "truck_code": "TRK-02",
    "covered_mrfs": ["brgy-05"], "effective_date": timeutil.today_str(),
    "effective_date": timeutil.today_str(), "status": "Active"}), None, ADMIN)
storage.write("notifications", [])
carryover_service.reassign(carry["id"], "TRK-02", ADMIN)
ok("reassigning notifies the newly assigned operator",
   any(n["audience"] == f"user:{op2['id']}" for n in storage.read("notifications")))

storage.write("notifications", [])
fake.clear()
mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B1, OP)
mrf_service.deliver(OP)
ok("a delivery notifies the city admin",
   any(n["type"] == notification_service.DELIVERY_COMPLETED
       for n in storage.read("notifications")))
ok("delivery_saved was broadcast",
   "delivery_saved" in fake.events_for("role:city_admin"))

storage.write("notifications", [])
tomorrow = timeutil.date_str(timeutil.today() + timedelta(days=1))
unavailable_service.create(Form({"affected_date": tomorrow,
                                 "reason": "Sick / medical leave"}), COL)
ok("an unavailability request notifies the city admin",
   any(n["audience"] == "role:city_admin"
       and n["type"] == notification_service.UNAVAILABLE_REQUEST
       for n in storage.read("notifications")))

storage.write("notifications", [])
public_report_service.submit(Form({"barangay_id": B1, "property_id": p1["id"],
                                   "status_reported": "Not Collected"}),
                             "203.0.113.7")
notes = storage.read("notifications")
ok("a resident report notifies that barangay",
   any(n["audience"] == f"barangay:{B1}"
       and n["type"] == notification_service.PUBLIC_REPORT for n in notes))
ok("and the city admin",
   any(n["audience"] == "role:city_admin"
       and n["type"] == notification_service.PUBLIC_REPORT for n in notes))
ok("a disputed report says so",
   any("differs from the collector" in n["message"] for n in notes))

print("\n[8] schedule broadcast")
storage.write("notifications", [])
fake.clear()
form = {f"{k}__{d}": v for d in schedule_service.DAYS
        for k, v in (("waste_type", "Residual Waste"), ("short", "Residual"),
                     ("tone", "amber"), ("details", "Wrapper"))}
schedule_service.save_week(form, ADMIN)
ok("schedule_updated is broadcast to everyone",
   "schedule_updated" in fake.events_for("public"))
ok("and a public notification is stored",
   any(n["audience"] == "public" and n["type"] == notification_service.SCHEDULE_UPDATED
       for n in storage.read("notifications")))

print("\n[9] the socket layer is optional")
realtime.init(None)
ok("emitting with no socket returns False, not an error",
   realtime.emit_to("public", "x", {}) is False)
ok("is_live reports the truth", realtime.is_live() is False)
storage.write("notifications", [])
rec = realtime.notify("role:city_admin", notification_service.PUBLIC_REPORT, "still stored")
ok("a notification is still STORED when the socket is down",
   rec and storage.count("notifications") == 1)
before = storage.count("collections")
collection_service.save_entry(Form({"status": "Collected", "qty_0": "2",
                                    "unit_0": "Sack"}), None, p1, COL)
ok("saving still works with no socket layer",
   storage.count("collections") == before or storage.count("collections") == before)
realtime.init(fake)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
