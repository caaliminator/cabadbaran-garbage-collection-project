"""
Phase 9 verification: cross-role integration.

Every other phase checks one area in isolation. This one checks the joins --
that an action taken by one role actually reaches the other roles who need it,
by both routes that matter:

    LIVE   the socket fan-out, for whoever happens to have the page open
    STORED what the other role's own read path returns afterwards, which is
           what they see on their next page load whether or not they were
           online when it happened

The second is the one that must never fail. A missed socket frame costs
someone a few seconds; a figure that never lands in the store is wrong until
somebody notices by hand.
"""
import shutil, sys, tempfile
from datetime import timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)
from config import Config

tmp = Path(tempfile.mkdtemp(prefix="gcts-p9-"))
Config.DATA_DIR, Config.GEO_DIR, Config.UPLOAD_DIR = tmp, tmp / "geo", tmp / "up"

from services import (assignment_service, carryover_service, collection_service,
                      duty_service, history_service, mrf_service,
                      notification_service, property_service,
                      public_report_service, realtime, schedule_service,
                      storage, timeutil, unavailable_service, user_service)
from services.auth_service import public_view
import seed

results = []
def ok(label, cond):
    results.append(bool(cond)); print(f"  {'PASS' if cond else 'FAIL':<4} {label}"); return cond


class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]


class FakeSocket:
    """Records what would have gone over the wire, per room."""
    def __init__(self): self.sent = []
    def emit(self, event, payload=None, to=None): self.sent.append((to, event, payload))
    def events_for(self, room): return [e for r, e, _ in self.sent if r == room]
    def clear(self): self.sent.clear()


fake = FakeSocket()
realtime.init(fake)

seed.run()
ADMIN = storage.find_one("users", role="city_admin")["id"]
CITY_ROOM, PUBLIC_ROOM = "role:city_admin", "public"
B1, B2 = "brgy-01", "brgy-02"
TODAY = timeutil.today_str()
B1_ROOM, B2_ROOM = f"barangay:{B1}", f"barangay:{B2}"

if not schedule_service.waste_types_for():
    schedule_service.save_week({f"{k}__{d}": v for d in schedule_service.DAYS
                                for k, v in (("waste_type", "Biodegradable"),
                                             ("short", "Bio"), ("tone", "green"),
                                             ("details", "Kitchen Waste"))}, ADMIN)

badmin = public_view(user_service.create(
    {"full_name": "B Admin 9", "username": "b_admin9", "role": "barangay_admin",
     "assigned_barangay": B1, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
badmin2 = public_view(user_service.create(
    {"full_name": "B Admin 9b", "username": "b_admin9b", "role": "barangay_admin",
     "assigned_barangay": B2, "password": "goodpass1",
     "confirm_password": "goodpass1"}, ADMIN))
cadmin = public_view(storage.get("users", ADMIN))

col = user_service.create({"full_name": "Col 9", "username": "col_nine",
                           "role": "tricycle_collector", "assigned_barangay": B1,
                           "assigned_vehicle": "TRI-01", "password": "goodpass1",
                           "confirm_password": "goodpass1"}, ADMIN)
op = user_service.create({"full_name": "Op 9", "username": "op_nine",
                          "role": "truck_collector", "assigned_vehicle": "TRK-01",
                          "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)
op2 = user_service.create({"full_name": "Op 9b", "username": "op_nineb",
                           "role": "truck_collector", "assigned_vehicle": "TRK-02",
                           "password": "goodpass1", "confirm_password": "goodpass1"}, ADMIN)

assignment_service.save_tricycle_assignment(Form({
    "collector_id": col["id"], "barangay_id": B1, "tricycle_code": "TRI-01",
    "effective_date": TODAY, "status": "Active"}), None, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op["id"], "truck_code": "TRK-01", "covered_mrfs": [B1, B2],
    "effective_date": TODAY, "status": "Active",
    "planned_time__brgy-01": "10:00"}), None, ADMIN)
assignment_service.save_truck_assignment(Form({
    "operator_id": op2["id"], "truck_code": "TRK-02", "covered_mrfs": [B2],
    "effective_date": TODAY, "status": "Active"}), None, ADMIN)

COL = public_view(storage.get("users", col["id"]))
OP = public_view(storage.get("users", op["id"]))
OP2 = public_view(storage.get("users", op2["id"]))

house = property_service.create(Form({"owner_name": "House 9", "type": "House",
                                      "purok": "Purok 1"}), B1, ADMIN)


def city_counts():
    return collection_service.counts(property_service.listing(), TODAY)


def brgy_counts(barangay_id=B1):
    return collection_service.counts(
        property_service.listing(barangay_id=barangay_id), TODAY)


def notes_for(user, kind=None):
    rows = notification_service.for_user(user)
    return [n for n in rows if not kind or n["type"] == kind]


# ---------------------------------------------------------------------------
print("\n[1] tricycle collector records a collection")
# ---------------------------------------------------------------------------
before_city, before_brgy = city_counts(), brgy_counts()
fake.clear()
entry = collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "6", "unit_0": "Sack"}), None, house, COL)

ok("LIVE  the barangay room is told", "collection_saved" in fake.events_for(B1_ROOM))
ok("LIVE  the city admin room is told", "collection_saved" in fake.events_for(CITY_ROOM))
ok("LIVE  an unrelated barangay is not",
   "collection_saved" not in fake.events_for(B2_ROOM))
ok("STORED the city admin's own counters moved",
   city_counts()["collected"] == before_city["collected"] + 1)
ok("STORED the barangay admin's counters moved",
   brgy_counts()["collected"] == before_brgy["collected"] + 1)
ok("STORED another barangay's counters did not",
   brgy_counts(B2)["collected"] == 0)
ok("STORED the household reads Collected on the collector's own route",
   next(r["status"] for r in collection_service.route_with_status(
        property_service.listing(barangay_id=B1), TODAY)
        if r["id"] == house["id"]) == "Collected")
ok("STORED the load reached the barangay's MRF card",
   mrf_service.mrf_card(B1, TODAY)["load"]["sacks"] == 6)
ok("STORED and the truck operator sees that load on their stop",
   any(c["barangay_id"] == B1 and c["load"]["sacks"] == 6
       for c in mrf_service.cards_for_operator(OP["id"])))

# ---------------------------------------------------------------------------
print("\n[2] tricycle collector goes on duty, then moves")
# ---------------------------------------------------------------------------
fake.clear()
duty_service.set_duty(COL["id"], True)
ok("LIVE  the public map is told", "collector_status" in fake.events_for(PUBLIC_ROOM))
ok("LIVE  the city admin is told", "collector_status" in fake.events_for(CITY_ROOM))
ok("LIVE  the collector's own barangay is told",
   "collector_status" in fake.events_for(B1_ROOM))
ok("STORED the city's Active Now counter sees them",
   duty_service.active_counts()["tricycles"] == 1)
ok("STORED the barangay's own counter sees them",
   duty_service.active_counts(B1)["tricycles"] == 1)
ok("STORED another barangay's does not", duty_service.active_counts(B2)["tricycles"] == 0)

# The socket handler does both: stores the fix, then broadcasts it.
fake.clear()
duty_service.record_location(COL["id"], 9.1226, 125.5344)
realtime.location_update(COL, 9.1226, 125.5344)
pub = [p for r, e, p in fake.sent if r == PUBLIC_ROOM and e == "location_update"]
city = [p for r, e, p in fake.sent if r == CITY_ROOM and e == "location_update"]
ok("LIVE  a resident gets the position", pub and pub[0]["vehicle"] == "TRI-01")
ok("LIVE  but is told no name and no user id",
   pub and "name" not in pub[0] and "id" not in pub[0])
ok("LIVE  the city admin is told who it is", city and city[0]["name"] == "Col 9")
ok("STORED the public map query returns them",
   [v["vehicle"] for v in duty_service.active_collectors()] == ["TRI-01"])

fake.clear()
duty_service.set_duty(COL["id"], False)
ok("STORED off duty removes them from the public map",
   duty_service.active_collectors() == [])
ok("LIVE  and the public map is told to drop the marker",
   "collector_status" in fake.events_for(PUBLIC_ROOM))
duty_service.set_duty(COL["id"], True)

# ---------------------------------------------------------------------------
print("\n[3] truck operator misses an MRF -> city and barangay admin")
# ---------------------------------------------------------------------------
fake.clear()
missed = mrf_service.save_pickup(
    Form({"status": "Not Collected", "reason": "Road inaccessible",
          "note": "Bridge out"}), B1, OP)

ok("LIVE  the barangay room hears the pickup", "mrf_pickup_saved" in fake.events_for(B1_ROOM))
ok("LIVE  the city admin hears it", "mrf_pickup_saved" in fake.events_for(CITY_ROOM))
ok("LIVE  a carry-over is announced to both",
   "carry_over_created" in fake.events_for(CITY_ROOM)
   and "carry_over_created" in fake.events_for(B1_ROOM))
ok("STORED the city admin has a notification about it",
   any(notes_for(cadmin, notification_service.CARRY_OVER_CREATED)))
ok("STORED so does the barangay admin whose MRF it is",
   any(notes_for(badmin, notification_service.CARRY_OVER_CREATED)))
ok("STORED another barangay's admin does not",
   not any(notes_for(badmin2, notification_service.CARRY_OVER_CREATED)))
ok("STORED it appears on the city's Carry-Over worklist as Missed Collection",
   [r["barangay_id"] for r in carryover_service.listing(carryover_service.MISSED)] == [B1])
ok("STORED the city's MRF page shows the barangay as Not Collected",
   next(r["status"] for r in mrf_service.city_listing(TODAY, barangay_id=B1))
   == mrf_service.NOT_COLLECTED)
ok("STORED the load is still counted as waiting in the MRF",
   mrf_service.mrf_card(B1, TODAY)["load"]["sacks"] == 6)

# ---------------------------------------------------------------------------
print("\n[4] city admin reassigns and reschedules -> the operator hears")
# ---------------------------------------------------------------------------
co = carryover_service.outstanding_for(B1)
fake.clear()
carryover_service.reassign(co["id"], "TRK-02", ADMIN)
ok("LIVE  the newly assigned operator's own room is told",
   "notification_new" in fake.events_for(f"user:{op2['id']}"))
ok("STORED and it is waiting in their bell",
   any("TRK-02" in n["message"] for n in notes_for(OP2)))
ok("STORED the operator who is not assigned hears nothing new",
   not any("carry-over" in n["message"].lower() and "TRK-02" in n["message"]
           for n in notes_for(OP)))

fake.clear()
carryover_service.reschedule(co["id"], TODAY, ADMIN)
ok("LIVE  the operator is told the date moved",
   "notification_new" in fake.events_for(f"user:{op2['id']}"))
ok("STORED the reschedule is in their bell",
   any("rescheduled" in (n.get("title") or "").lower() for n in notes_for(OP2)))
ok("STORED the stop is now on that operator's MRF list",
   any(c["barangay_id"] == B1 and c.get("is_carry_over")
       for c in mrf_service.cards_for_operator(OP2["id"])))
ok("STORED and not on the original operator's",
   not any(c["barangay_id"] == B1 and c.get("is_carry_over")
           for c in mrf_service.cards_for_operator(OP["id"])))

# ---------------------------------------------------------------------------
print("\n[5] the reassigned truck collects it -> everyone downstream")
# ---------------------------------------------------------------------------
fake.clear()
closing = mrf_service.save_pickup(Form({"status": "Collected from MRF"}), B1, OP2)
ok("STORED the carry-over closed itself",
   carryover_service.outstanding_for(B1) is None)
ok("STORED it now sits in the city's Collected view",
   [r["barangay_id"] for r in carryover_service.listing(carryover_service.COLLECTED)] == [B1])
ok("STORED the city's MRF page for today shows it collected",
   next(r["status"] for r in mrf_service.city_listing(TODAY, barangay_id=B1))
   == mrf_service.COLLECTED)
ok("STORED and marks that row as a carry-over",
   mrf_service.city_listing(TODAY, barangay_id=B1)[0]["carry_over"] is not None)
ok("STORED the load moved onto the collecting truck",
   mrf_service.running_load(OP2["id"])["sacks"] == 6)
ok("LIVE  the barangay and the city both hear the pickup",
   "mrf_pickup_saved" in fake.events_for(B1_ROOM)
   and "mrf_pickup_saved" in fake.events_for(CITY_ROOM))

fake.clear()
delivery = mrf_service.deliver(OP2)
ok("LIVE  the delivery reaches the city admin", "delivery_saved" in fake.events_for(CITY_ROOM))
ok("STORED the city's delivery list has it",
   any(d["id"] == delivery["id"] for d in mrf_service.deliveries_listing(TODAY)))
ok("STORED the day's history counts that delivery",
   history_service.compute(TODAY)["deliveries"]["count"] == 1)

# ---------------------------------------------------------------------------
print("\n[6] a resident reports -> barangay admin and city admin")
# ---------------------------------------------------------------------------
house2 = property_service.create(Form({"owner_name": "House 9b", "type": "House",
                                       "purok": "Purok 1"}), B1, ADMIN)
fake.clear()
report = public_report_service.submit(
    Form({"barangay_id": B1, "property_id": house2["id"],
          "status_reported": "Not Collected", "comment": "Nobody came"}),
    ip="10.0.0.9")
ok("STORED the barangay admin is notified",
   any(notes_for(badmin, notification_service.PUBLIC_REPORT)))
ok("STORED the city admin is notified",
   any(notes_for(cadmin, notification_service.PUBLIC_REPORT)))
ok("STORED an unrelated barangay admin is not",
   not any(notes_for(badmin2, notification_service.PUBLIC_REPORT)))
ok("STORED it appears on the city's resident-reports list",
   any(r["id"] == report["id"] for r in public_report_service.listing()))
ok("STORED with no entry of its own, it creates one marked as the resident's",
   collection_service.entry_for(house2["id"], TODAY)["source"]
   == collection_service.SOURCE_PUBLIC)

# The same household, now with a collector's record that disagrees.
collection_service.save_entry(
    Form({"status": "Collected", "qty_0": "2", "unit_0": "Sack"}), None, house, COL)
fake.clear()
dispute = public_report_service.submit(
    Form({"barangay_id": B1, "property_id": house["id"],
          "status_reported": "Not Collected", "comment": "It is still here"}),
    ip="10.0.0.10")
ok("STORED a disagreement is flagged, not overwritten",
   dispute["disputed"] and collection_service.entry_for(house["id"], TODAY)["status"]
   == "Collected")
ok("STORED the barangay admin sees a dispute to settle",
   collection_service.entry_for(house["id"], TODAY)["disputed"])
ok("STORED and the city admin's dispute counter sees it",
   public_report_service.dispute_count() == 1)
ok("STORED the collector's own route shows the entry as disputed",
   next(r["disputed"] for r in collection_service.route_with_status(
        property_service.listing(barangay_id=B1), TODAY)
        if r["id"] == house["id"]))

public_report_service.resolve_dispute(
    collection_service.entry_for(house["id"], TODAY)["id"], "resident", badmin["id"])
ok("STORED the barangay admin's ruling rewrites the record",
   collection_service.entry_for(house["id"], TODAY)["status"] == "Not Collected")
ok("STORED and the city's counters follow it",
   city_counts()["not_collected"] >= 1 and public_report_service.dispute_count() == 0)

# ---------------------------------------------------------------------------
print("\n[7] a collector asks for a day off -> city admin decides")
# ---------------------------------------------------------------------------
tomorrow = timeutil.date_str(timeutil.today() + timedelta(days=1))
fake.clear()
request_row = unavailable_service.create(
    Form({"reason": "Sick / medical leave", "affected_date": tomorrow,
          "unavailable_until": tomorrow, "notes": "Fever"}), COL)
ok("LIVE  the city admin room hears it", "notification_new" in fake.events_for(CITY_ROOM))
ok("STORED it is in the city admin's bell",
   any(notes_for(cadmin, notification_service.UNAVAILABLE_REQUEST)))
ok("STORED and on their pending list",
   any(r["id"] == request_row["id"] for r in unavailable_service.pending()))

fake.clear()
unavailable_service.decide(request_row["id"], "Approved", ADMIN)
ok("LIVE  the collector's own room is told the answer",
   "notification_new" in fake.events_for(f"user:{col['id']}"))
ok("STORED the collector can read the decision",
   any("approved" in n["message"].lower() for n in notes_for(COL)))

# ---------------------------------------------------------------------------
print("\n[8] city admin changes the schedule -> everyone, residents included")
# ---------------------------------------------------------------------------
fake.clear()
schedule_service.save_week({f"{k}__{d}": v for d in schedule_service.DAYS
                            for k, v in (("waste_type", "Recyclable"),
                                         ("short", "Recyclable"), ("tone", "blue"),
                                         ("details", "Bottles"))}, ADMIN)
ok("LIVE  the public room is told the schedule moved",
   "schedule_updated" in fake.events_for(PUBLIC_ROOM))
ok("STORED a resident reading the schedule gets the new one",
   schedule_service.for_date(TODAY)["short"] == "Recyclable")

# ---------------------------------------------------------------------------
print("\n[9] a room only ever hears what belongs to it")
# ---------------------------------------------------------------------------
ok("a barangay admin's rooms are public, their own, and their barangay",
   set(realtime.rooms_for(badmin)) == {"public", f"user:{badmin['id']}", B1_ROOM})
ok("a city admin's rooms include the city room and every barangay",
   CITY_ROOM in realtime.rooms_for(cadmin)
   and sum(1 for r in realtime.rooms_for(cadmin) if r.startswith("barangay:")) == 31)
ok("a truck operator's rooms are only the barangays they cover",
   {r for r in realtime.rooms_for(OP) if r.startswith("barangay:")} == {B1_ROOM, B2_ROOM})
ok("an anonymous resident gets the public room and nothing else",
   realtime.rooms_for(None) == ["public"])

shutil.rmtree(tmp, ignore_errors=True)
passed = sum(results)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
