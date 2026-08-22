# Build Progress

Cabadbaran City Garbage Collection Tracking System.
Spec: `instruction/FUNCTIONALITIES.md` (authoritative) + `instruction/project_summary_script.txt`.

**Current state: all eight phases complete, plus a presentation pass.**
**464 automated checks passing** — run them with `python tests/run_all.py`.

---

## Phase 9 — presentation pass ✅

The system was functionally complete and verifiably empty. This phase fixed
what that looked like, plus four defects the move to a new folder exposed.

| Item | File | Notes |
|---|---|---|
| Portable tests | `tests/phase*_check.py` | Absolute paths from a previous folder replaced with `Path(__file__)`; the suite had stopped running at all |
| Tests restore passwords | `tests/phase1_check.py` | Records the hashes it finds and puts them back on exit, including on early failure |
| Windows write retry | `services/storage.py` | `os.replace` retried on a sharing violation — a scanner or sync client holding the file for milliseconds was failing valid saves |
| Readable MRF markers | `static/js/map.js`, `components.css` | Small dots with zoom-gated labels, replacing 31 labelled pills that stacked into one unreadable clot |
| Barangay-scoped maps | `static/js/map.js` | A page pinned to one barangay now opens on it, with neighbours faded and its own MRF highlighted. `data-map-scope` had been read and never used |
| Zone legend | `live_map.html`, `map.js` | The four colour groups, printed from `/api/geo/config` so it cannot drift from the polygons |
| One row per waste type | `by_waste_type` filter | "Bag — 31 sacks · 4 kg" instead of two rows four apart |
| Demo data | `tools/make_demo_day.py` | A working week, written through the normal storage layer; `--reset` removes it |
| Spreadsheet geo import | `tools/geo_from_csv.py` | Fill in a CSV, get valid GeoJSON, with the lat/lng flip and ring closure handled |
| Real tracking counters | `city-hall-admin/tracking.html` | "Active Trucks 4 / Active Tricycles 23" were the mockup's sample figures; `counts` was already passed to the template and never read |
| Known logins | `tools/set_demo_passwords.py`, `docs/CREDENTIALS.md` | — |

### The map was the weakest thing on the screen

Thirty-one MRFs inside a 17 km city sit within a couple of hundred pixels of
each other at city zoom. They were drawn with the same labelled-pill icon as
the vehicles, so the middle of the city was a black clot with no readable label
and the barangay zones invisible underneath. Now: a small dot always, its name
only once the zoom leaves room for it, and the vehicles keep the pills — they
are what a viewer is actually looking for.

The barangay-scoped maps were worse than cluttered, they were wrong: a barangay
admin opened their dashboard to the whole province with all 31 MRFs on it.
`data-map-scope` was being read into map state and then never used. Their map
now opens on their own barangay.

### Sacks and kilos, still separate, no longer twice

Loads are keyed by (type, unit) because the chart needs sacks and kilos as
separate series. Every summary card rendered that key directly, so "Bag —
357 sacks" and "Bag — 227 kg" appeared as two unrelated rows. A
`by_waste_type` Jinja filter groups them at render time — deliberately a filter
and not a field, because pickups and deliveries store a *snapshot* of their
load and records written before the change have no grouped field. Sacks and
kilos are still never added together.

### The four defects were all real

The suite was not failing, it was not running: nine `sys.path` inserts pointed
at a folder that no longer exists. Once running, phase 1 turned out to leave
the four demo accounts on its own test password — run the tests, lose your
logins. And `os.replace` was failing intermittently with a Windows sharing
violation, which is a lost save for a reason that has nothing to do with the
app.

---

## Phase 8 — Reports, chart, offline queue, cleanup ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Reports | `services/report_service.py` | Five types, printable HTML + CSV from one build |
| City dashboard | `blueprints/city.py` + template | Live counters, merged activity feed, Chart.js card |
| Property monitoring | `city-hall-admin/property.html` | All six filters, any date |
| Printable report | `city-hall-admin/report_print.html` | Own print stylesheet, repeating headers |
| Chart | `static/js/chart.js` | Sacks and kilos as separate series |
| Offline queue | `static/js/offline.js` | Held in localStorage, retried on reconnect |
| Cleanup | — | **`data_store.py` deleted** |

### The reports have a concrete output

The mockup only said "Generate", so this defines it: **a printable HTML page
and a CSV of the same rows**, both from one `build()` call, so the printout and
the spreadsheet cannot disagree. Five types — Property Collection, MRF
Collection, Deliveries, Carry-Overs, Collector Performance — over any range up
to a year, optionally scoped to one barangay.

The printable page carries the scope, range, and generation time on the page
itself: a printout gets detached from the screen that made it and has to stand
alone as a record. Column headers repeat on every printed sheet.

The CSV is written UTF-8 **with a BOM** — Excel opens a plain UTF-8 CSV as the
system codepage and mangles accented barangay names.

Figures for completed days come from the frozen summaries, so running the same
report twice over the same past range gives the same numbers. A test proves it
by deleting a property and re-running.

### Offline queue, with its limits stated

An entry submitted with no connection is held in localStorage and sent on
reconnect. Two deliberate limits, both surfaced to the collector rather than
hidden:

- **Photos are not queued.** A 5 MB image would exhaust the localStorage budget
  on one entry. A not-collected record needs its photo, so those need a live
  connection — and the collector is told that plainly instead of being left to
  believe it saved.
- **Queued entries expire after 24 hours.** Re-submitting a stop from days ago
  would file it under today's date and corrupt both days' totals.

A 4xx response drops the entry with a message rather than retrying forever;
only a 5xx is retried.

### `data_store.py` is gone

The prototype's in-memory data — the fake `1,248 / 1,496` counters, the sample
people, the static activity feed — is deleted. Every page reads the JSON store.
Tests assert the file is absent, that no module imports it, and that no sample
name or invented figure survives anywhere in the Python or the templates.

### Verified

47 new checks. **All nine suites green: 447 checks.** Plus an HTTP pass through
report generation, the printable page, and a CSV that parses back cleanly with
its BOM intact.

Every CDN asset carries a subresource-integrity hash, all five verified against
the real published files. **One was wrong** — the Chart.js hash I wrote from
memory did not match, and would have silently blocked the chart from loading.
Caught by checking rather than trusting.

---

## Phase 7 — Socket.IO real-time layer ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Socket layer | `services/realtime.py` | Rooms, emit helpers, `notify()` (store then push) |
| Triggers | `services/triggers.py` | All eight automatic notifications |
| Handlers | `blueprints/sockets.py` | connect/rejoin, location, duty, mark-read |
| App wiring | `app.py` | `socketio.run` replaces `app.run`; threading async mode |
| Client | `static/js/realtime.js` | Connect, listen, stream position, live bell |
| Emits | 8 services | Wired at the points marked in earlier phases |

### The one new dependency

`pip install flask-socketio` (5.6.1), pulling `python-socketio`,
`python-engineio`, `bidict`, `simple-websocket`, `wsproto`. **No eventlet or
gevent**: threading async mode works with the built-in server, which is the
LAN / small-VPS deployment this is for.

**The app now starts with `socketio.run(app)`.** `python app.py` is unchanged
for you — it just goes through Socket.IO now.

### Rooms are decided by the server

The client never names a room. On connect the server reads the session, works
out what that account is entitled to, and joins it:

- anonymous → `public` only
- barangay admin → `public`, their own `user:` room, their one barangay
- truck operator → only the barangays their assignment covers
- city admin → the city room plus all 31 barangays

Verified against a **real running server** with a real Socket.IO client: an
anonymous connection gets `['public']`, and an authenticated barangay admin
gets three rooms with the city-admin room withheld.

### Positions are public; identities are not

A resident needs to see where the truck is — that is the point of the system.
Who is driving it is a different question. The `public` payload carries the
vehicle code, kind, and coordinates; the name and user id go only to admin
rooms. A location update is accepted **only from the session it belongs to**,
so no collector can move another's marker.

### The eight triggers

Truck approaching (haversine < 500 m, once per truck per MRF per day) ·
T-2h scheduled arrival · unavailability request → city · assignment changed →
that collector · missed pickup → carry-over → city + barangay · carry-over
reassigned → the new operator · resident report → barangay + city · delivery →
city · schedule updated → everyone.

The two that fire on repeating conditions are deduped by key, so a truck idling
near an MRF raises one alert, not one per GPS ping. The T-2h reminder rides on
the same lazy per-request check as the end-of-day job — no scheduler.

### It degrades, it does not break

Every emit is best-effort. With the socket layer down, `emit_to()` returns
False and the app carries on: notifications are still **stored** (the record is
the source of truth, the push is on top), saves still work, and the map falls
back to its 30-second poll. Tests assert this by running the whole suite with
the socket layer set to `None`.

Both CDN scripts carry subresource-integrity hashes, and all three (socket.io
plus the two Leaflet files) were **verified against the real published files**
rather than trusted from memory.

### Verified

48 new checks, plus a live-server test with a real socket client. All suites
green: **400 checks across Phases 0–7**. All 34 pages render.

---

## Phase 6 — Public Viewer ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Anonymous reports | `services/public_report_service.py` | Rate limit, conflict handling, dispute resolution |
| Public pages | `blueprints/public.py` | Live map, schedule, today's waste, report |
| Calendar + cards | `services/schedule_service.py` | Month grid, upcoming days, today's cards |
| Templates | 5 new | Shared shell with bottom tabs, four pages |
| Dispute resolution | `blueprints/brgy.py` + `brgy-admin/reports.html` | Resident Reports page with the collector's photo alongside |

### Four tabs, thumb-first

Bottom tabs on a phone — this page is used one-handed at the kerb, and the
bottom of the screen is what a thumb reaches. They move back to the top from
720px, where a fixed bottom bar would look out of place. Each tab is a real
URL a resident can bookmark or share, not a JS panel.

The report flow works **with JavaScript switched off entirely**: each choice is
a normal form submit that reloads with the next set of options. Where JS is
available the selects update in place instead. A resident on a weak signal
should still be able to report.

### A resident never overwrites a collector

This is the conflict rule from spec §7, and it is the part worth being careful
about. When an anonymous report disagrees with a collector's record:

- the collector's status, load, note, and **photo proof are left exactly as
  they were**
- the report is stored in full
- the collection entry is flagged **Disputed**, with a note stating both accounts
- the barangay admin decides, on a Resident Reports page that shows them the
  collector's photo next to the resident's comment

Overwriting on the word of an anonymous form would destroy a collector's
evidence. Tests assert the status, the load, and the note all survive intact.

Where there is no conflict the report simply creates the entry, marked
`source: public_report` and reasoned "Reported by resident", so an admin can
always tell it apart from a collector's own entry.

### Rate limiting without storing IPs

Three reports per device per day (`Config.PUBLIC_REPORT_DAILY_LIMIT`). The key
is a **salted SHA-256 hash** of the IP, never the address itself — a raw IP
stored beside a household's name and purok is identifying information the
system has no reason to keep, and the hash enforces the limit just as well.
Salted with `SECRET_KEY`, so the hashes cannot be matched against a
precomputed table of the IPv4 space. A test asserts no raw address appears
anywhere in the stored data, and the key never reaches an admin view.

### Verified

53 new checks plus an HTTP pass through the real form: the 4th report in a day
is refused with a clear message, and the barangay admin sees the first three.
All suites green: **352 checks across Phases 0–6**.

### Stubbed, by design

- Notification triggers on a new report are Phase 7 (the marked point is in
  `public.report`).
- The City Hall dashboard, Property page, and Reports page still read
  `data_store.py` — Phase 8.

---

## Phase 5 — Barangay Admin pages ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Notifications | `services/notification_service.py` | Audience-addressed, per-person read state, repeat suppression |
| Daily summaries | `services/history_service.py` | Compute, freeze, and the lazy end-of-day job |
| Live vehicle API | `blueprints/api_live.py` | `/api/live/vehicles`, scoped by session |
| **Leaflet map** | `static/js/map.js`, `partials/live_map.html` | Replaces the hardcoded inline-SVG map |
| Barangay routes | `blueprints/brgy.py` | All seven pages on real data, scoped |
| Templates | 6 rewritten + 3 new partials | Dashboard, properties, collections, tracking, history, schedule |

### The map is now real

The prototype drew a stylised inline SVG with **77 lines of hardcoded polygon
coordinates** — precisely what the mapping constraint forbids. That macro is
deleted. In its place, `partials/live_map.html` renders a Leaflet map whose
every coordinate, boundary, zone colour, and marker comes from `/api/geo/*` and
`/api/live/vehicles` at runtime. A test now asserts that **no template and no
JS file contains a coordinate**, so this cannot regress.

Layer order is deliberate: zones underneath, hotspots above them, live vehicles
on top — switching the hotspot layer on never hides a moving truck. Leaflet
loads by CDN `<script>` with subresource-integrity hashes, so a compromised CDN
fails to load rather than running altered code on pages showing household
addresses.

Until the real boundary data arrives the map shows tiles and an honest note
("Barangay boundaries have not been loaded yet") rather than a blank frame.

### Scope, enforced server-side

`_scope()` is the single place a barangay admin's barangay comes from, and it
reads the session. Property create/update/delete take the barangay as an
argument from that scope, never from the form — so a tampered hidden field
cannot move a property into another barangay, and cross-barangay edits and
deletes both fail.

### Notifications

Addressed to an **audience** (`public`, `role:city_admin`, `barangay:<id>`,
`user:<id>`) rather than to a person, so alerts survive staff changes. Read
state is per person: one admin marking an alert read does not hide it from a
colleague in the same barangay.

### The lazy end-of-day job is live

Wired into the app context processor: on the first request of a new day, any
day not yet summarised is auto-missed for untouched MRFs and then frozen into
`history.json`. No scheduler, no dependency, and it walks back over days the
server was switched off for. Frozen days do **not** change afterwards — a test
confirms that deleting a property leaves last week's figures alone.

### Verified

40 new checks, and all earlier suites re-run green: **299 checks across
Phases 0–5**. All 30 pages render.

### Stubbed, by design

- The public viewer's anonymous report flow is Phase 6.
- Notification *triggers* (truck approaching, T-2h reminder) are Phase 7; the
  engine and the bell they feed are done.
- The City Hall dashboard, Property, and Reports pages still read
  `data_store.py` — Phases 6 and 8.

---

## Phase 4 — Truck Collector app, MRF pickups, deliveries, carry-overs ✅

### Complete

| Item | File | Notes |
|---|---|---|
| MRF pickups + deliveries | `services/mrf_service.py` | Derived loads, running load, delivery reset, auto-miss, city views |
| Carry-over lifecycle | `services/carryover_service.py` | Open on miss, reassign, reschedule, auto-close |
| Truck routes | `blueprints/collector.py` | MRF list, pickup entry, deliver, history, profile |
| City pages | `blueprints/city.py` | MRF page and Carry-Over page on real data |
| Templates | 5 rewritten | Truck route/record/history, city MRF, city carry-over |

### The aggregation chain, working end to end

    tricycle entries → barangay Overall Collected Load
                     → that barangay's MRF card, shown to the truck
                     → the truck's running Overall Collected Load
                     → the delivery totals
                     → the city MRF page

The truck operator **records no quantities**. An MRF card's load is computed
from that barangay's collection entries since its last successful pickup —
which is also what makes carry-overs work with no special casing: a missed
pickup leaves the load in place, and it is still there, larger, next visit.

### Carry-over lifecycle

Truck marks Not Collected → carry-over opens (Pending, no truck) → admin
Reassigns and/or Reschedules → the assigned truck sees it as an extra stop on
its route → collecting that MRF **closes it automatically**. Repeated misses
update one carry-over with a miss count rather than stacking duplicate rows —
three misses are one overdue load that has grown, not three loads.

`auto_mark_missed(date)` closes off a past day: any MRF holding waste with no
status recorded is marked Missed and enters carry-over, flagged `auto_missed`
so it is distinguishable from an operator-reported miss. Without it, inaction
would silently lose the load.

### Two real bugs found and fixed by the tests

1. **Same-day collections after a pickup were invisible.** The "what is waiting
   in the MRF" cut-off compared dates only, so a household recorded after the
   truck had already emptied the MRF that morning was swallowed and never
   appeared again. It now compares `(date, timestamp)` — and `timeutil.stamp()`
   moved from second to millisecond resolution, since several records can be
   written inside one second.
2. **Re-recording a missed MRF as Collected gave it an empty load.** The card
   returned the missed record's (empty) snapshot instead of the live figure.
   A completed pickup keeps its snapshot; a pending or missed one shows what is
   actually still sitting there.

### Verified

61 service-level checks plus HTTP-level checks of the miss → carry-over →
reassign → reschedule flow, past-date rejection, and out-of-route 403.
All earlier suites re-run green: **259 checks across Phases 0–4**.

### Stubbed, by design

- The truck's SAVE button on the Collected frame is **added** (OPEN ITEM 5); a
  consequential record with no submit is a mockup omission.
- `mrf_pickup_saved`, `carry_over_created`, and `delivery_saved` are not
  broadcast yet — Phase 7.
- `auto_mark_missed` exists and is tested but is not yet *called* on a schedule;
  Phase 8 wires it into the lazy end-of-day job.

---

## Phase 3 — Tricycle Collector app ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Properties | `services/property_service.py` | Scoped CRUD. A property carries no status — status belongs to a date |
| Collection entries | `services/collection_service.py` | The status model, load totals, image proofs, same-day correction, history |
| Duty + position | `services/duty_service.py` | On/Off duty, last known position, active-collector counts |
| Unavailability | `services/unavailable_service.py` | Requests, overlap checks, expiry |
| Routes | `blueprints/collector.py` | Route list, entry, history, unavailable, duty toggle, `/location`, `/proofs/<path>` |
| Templates | 5 rewritten + 1 new partial | Route list, record form, history with real date filter, duty toggle |
| Client behaviour | `static/js/app.js` | `watchPosition` streaming while on duty, throttled |

### The status model, working as specced

A property is **Pending** for a date because no record exists for that date.
Saving makes it Collected or Not Collected. Nothing resets at midnight — every
"today" view queries today's Manila date, so yesterday keeps its own answer and
history comes out for free.

### Rules that now fire

- Collected needs at least one quantity above zero; quantities must be whole,
  non-negative, and ≤ 999
- Not Collected needs a reason from the seeded list **and** an image proof
- Waste types come from **that day's schedule row**, so a Tuesday entry offers
  recyclable categories rather than Kitchen Waste
- A collector may correct their **own** entry on the **current day only**
- The route is the collector's barangay narrowed to their purok coverage; a
  property outside it returns 403 even by direct URL
- GPS, collector, and vehicle are stamped from the session, never the form
- **Sacks and kilos are totalled separately** — "12 sacks and 4 kg", never "16"

### Image proofs

Stored **outside `static/`** (a deviation from the spec's stated path, noted
below) and served by `/collector/proofs/<path>`, which checks that the viewer is
an admin or the collector who filed it. Filenames are random rather than derived
from what the phone sent, and path traversal is refused.

### Verified

72 service-level checks plus HTTP-level checks of route scoping (403 on a
property outside the coverage), cross-role access, the duty toggle, the location
endpoint, a real multipart upload, and proof access for owner / other collector /
anonymous.

### Stubbed, by design

- The truck side still runs on `data_store.py` — Phase 4.
- `collection_saved` and the unavailable-request notification are not broadcast
  yet; Phase 7 adds the emits at the marked points.
- `python seed.py --demo` now also creates 8 demo properties and routes for the
  two demo collectors, so the app is walkable before Phase 5's Property List.

---

## Phase 2 — City Hall Admin management ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Form validation | `services/validation.py` | Collects every failure at once instead of one per submit; PH phone normalising, username/time/date rules |
| User accounts | `services/user_service.py` | Create, edit, reset password, delete, status. Role-dependent required fields |
| Vehicle registry | `services/vehicle_service.py` | Register/withdraw/delete units; status derived from live assignments |
| Assignments | `services/assignment_service.py` | Both kinds, with clash errors and coverage warnings |
| Waste schedule | `services/schedule_service.py` | Editable week; drives which waste types a collector can record that day |
| Routes | `blueprints/city.py` | `/city/users`, `/city/vehicles`, `/city/schedule`, `/city/tricycle`, `/city/truck` |
| Templates | 4 rewritten + 1 new partial | View/Edit/Reset modals, schedule editor, MRF picker, registry |
| Client behaviour | `static/js/app.js` | Role-dependent fields, purok pills rebuilt per barangay, modal prefill |

### Rules that now actually fire

- Usernames unique case-insensitively; role decides which fields are required
- A vehicle belongs to at most one account **and** at most one active assignment
- Truck registry capped at 8 (`Config.MAX_TRUCKS`); withdrawing one frees a slot
- Purok coverage must belong to the chosen barangay
- One active assignment per collector and per unit — hard errors, naming the
  current holder rather than "not a valid choice"
- Overlapping purok or double-covered MRF — **warnings**, saved anyway
- The city can never lose its last active City Hall Admin, and nobody can
  delete or deactivate themselves
- Deleting an account removes its route assignment, so no route points at a
  collector who no longer exists

### The coverage gap, made visible

8 trucks × 4 barangays = 32 against 31 barangays. Rather than encode a rule
that cannot hold, the Truck page shows **MRFs Covered: n/31**, names every
uncovered barangay in a banner, and repeats it as a warning after each save.
7×4 + 1×3 = 31 assigns cleanly.

### Verified

68 service-level checks plus an end-to-end pass through the real HTTP forms
(create, duplicate rejection, schedule save, vehicle registration, truck cap).

### Stubbed, by design

- Dashboard, Property, MRF, Carry-Over, Live Tracking, and Reports still read
  `data_store.py`. They are rebuilt in Phases 4–8.
- The Availability column reads `unavailable_requests`, which nothing writes
  until Phase 3.
- `schedule_updated` is not broadcast yet — Phase 7 adds the socket emit at the
  marked point in `city.schedule`.

---

## Phase 1 — Auth, RBAC, sessions, change password ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Account rules | `services/auth_service.py` | `werkzeug` hashing, authenticate, change/reset password, role registry. The only module that touches a password |
| Login / logout / CSRF / guards | `blueprints/auth.py` | Single `/login`, CSRF hook, idle timeout, `@role_required`, scope helpers |
| Login page | `templates/auth/login.html` | Role switcher and demo-credential block removed; Forgot Password shows "Contact the City Hall Admin" |
| CSRF tokens | 20 forms across 13 templates | `{{ csrf_field() }}` in every POST form |
| Profile pages | 3 templates | Account details now read-only and bound to the real account; Change Password posts to the shared endpoint |
| Error pages | `app.py` | 400 and 403 handlers added alongside 404/500 |
| Demo accounts | `seed.py --demo` | One per non-admin role, generated passwords, opt-in |

### What changed in behaviour

- **`/login/<role>` is gone.** One `/login` for everyone; the account's role
  decides where it lands. The old URL now 404s.
- **The four hardcoded `admin123` accounts are gone.** Every login is checked
  against a `werkzeug` hash in `users.json`.
- **Every POST is CSRF-checked** in a `before_app_request` hook, so a form that
  forgets its token fails closed rather than posting silently.
- **Sessions resolve to a live user record on every request**, so deactivating
  or deleting an account ends that person's session on their very next click
  rather than whenever they log out.
- **Idle timeout applies to admin roles only** (60 min, `SESSION_IDLE_MINUTES`).
  Collectors are exempt — a timeout mid-route in the field would mean lost work.
- **Barangay scope comes from the session, never the request.** `assigned_barangay()`
  reads the signed-in admin's own barangay; `require_barangay_scope()` and
  `require_owner()` are ready for the data routes in Phases 2–5.

### Verified

36 automated checks, all passing: valid/invalid login, identical message for
unknown user vs wrong password (no username enumeration), case-insensitive
usernames, inactive accounts blocked, a 10-combination role access matrix,
anonymous redirect vs signed-in 403, CSRF missing/forged/valid, remember-me vs
browser-session cookies, admin idle timeout, collector timeout exemption, live
deactivation killing a session, four open-redirect payloads rejected, all six
change-password rules, no plaintext or hash anywhere in the store or rendered
HTML, and all 26 authenticated pages still returning 200.

### Stubbed, by design

- Page **content** is still the Phase 0 prototype data (`data_store.py`).
  Phase 1 changed who can reach a page and how they authenticate, not yet what
  the page shows.
- `must_change_password` is set and cleared correctly, but is not yet *forced* —
  a user who should change their password is prompted, not blocked. The forced
  redirect lands with Phase 2's admin password reset.
- Collector `purok`/`vehicle` in the profile come from the account record;
  they move to the assignment records in Phase 2.

---

## Phase 0 — Skeleton, storage, geo service, seed ✅

### Complete

| Item | File | Notes |
|---|---|---|
| Configuration | `config.py` | Map centre/zoom, hotspot flags, zone groups, limits, upload rules. No credentials — `SECRET_KEY` comes from the env or a generated `instance/.secret_key` |
| JSON storage | `services/storage.py` | Atomic writes (temp file + `os.replace` in the same directory), one reentrant lock per collection, auto-create, `transaction()` for safe read-modify-write, audit fields on every insert/update |
| All 15 collections | `data/*.json` | Registered in `storage.COLLECTIONS`; per spec §2 |
| Manila time | `services/timeutil.py` | Every timestamp is Asia/Manila. Falls back to fixed UTC+8 where `tzdata` is absent (Windows), which is exactly correct for the Philippines |
| Geo service | `services/geo_service.py` | Single source of truth for every coordinate. mtime-cached loaders, graceful degradation, derived-hotspot engine, haversine |
| Geo REST API | `blueprints/api_geo.py` | `/api/geo/config`, `/barangays`, `/mrfs`, `/hotspots`, `/status` |
| Geo placeholders | `data/geo/*` | Valid, flagged `_placeholder: true`, pre-filled with all 31 barangay ids |
| Seed script | `seed.py` | Idempotent — verified byte-identical output on re-run |
| Data requirements | `docs/DATA_REQUIREMENTS.md` | What to collect, in what format, worked example, swap steps, validation snippets |

### Seeded data

- **31 barangays**, numbered 1–31, each with a zone group and Purok 1–7
- **31 tricycle units** (TRI-01…TRI-31) — a one-per-barangay floor, extensible
  through the vehicle registry in Phase 2
- **8 truck units** (TRK-01…TRK-08) — the city-wide ceiling, enforced by
  `Config.MAX_TRUCKS`
- **7-day waste schedule** — Mon/Wed/Fri Biodegradable + Residual, Tue
  Recyclable, Thu Residual, Sat Special Waste, Sun no collection
- **1 City Hall Admin** — password generated, printed once, stored only as a
  `werkzeug` hash

### Verified

22 automated checks, all passing: seed counts, hashed password, zone-group
coverage, missing/malformed/empty geo files degrading to empty states, derived
hotspot severities and filters, 320 concurrent inserts with no lost writes or
duplicate ids, transaction rollback, no stray temp files, and idempotency of a
second seed over live data.

`python app.py` boots; all 26 prototype routes still return 200.

### Stubbed, by design

- `data/geo/*` hold placeholders. Maps will show an empty state until real data
  arrives — see `docs/DATA_REQUIREMENTS.md`.
- `HOTSPOT_LAYER_ENABLED = False` by default. Flip it in `config.py` to turn the
  layer on; `HOTSPOT_SOURCE = "derived"` then computes hotspots from real data.
- Purok lists are a uniform Purok 1–7 placeholder for all 31 barangays.

### Not touched

`data_store.py` and the existing blueprints still drive every page. Phase 0 is
purely additive — nothing that worked before was changed or removed. The
prototype's in-memory store gets retired progressively across Phases 1–8, and
the file is deleted only in Phase 8.

---

## Decisions made on your behalf

Flagged here because they were judgement calls, and each is cheap to reverse.

1. **Barangay numbering is alphabetical** (Poblacion 1–12 in numeric, not
   lexical, order). Numbers 1–31 drive the four map zone colour groups. If the
   city has an official numbering, replace `BARANGAY_NAMES` in `seed.py`.
2. **31 tricycle units seeded**, not 23 and not 124. You said each barangay can
   have multiple collectors with no fixed count, so the code assumes no count
   anywhere — one unit per barangay is a floor, and the registry adds more.
   Live counters ("23 Tricycles Active") derive from who is on duty, so no
   number is hardcoded.
3. **Truck coverage is a free multi-select**, not a hardcoded 4 barangays each.
   8 trucks × 4 barangays = 32, one more than the 31 that exist, so a fixed rule
   cannot hold. The Phase 2 assign form will let you pick any set, warn when a
   barangay is covered by two active trucks, and show "MRFs Covered: n/31" in
   amber while any barangay is uncovered. 7×4 + 1×3 = 31 assigns cleanly.
4. **No fake polygons shipped.** Placeholder zone features carry
   `geometry: null` rather than invented boundaries — an imaginary boundary
   drawn over a real satellite map reads as authoritative when it is not.
5. **Fixed UTC+8 instead of a `tzdata` dependency.** Correct for the
   Philippines, which has had no daylight saving since 1978; uses `zoneinfo`
   automatically where the tz database is available.
6. **Derived hotspots anchor to the barangay MRF**, since no purok geometry
   exists. Reported honestly via `meta.anchored_to`, and upgradeable to true
   per-purok placement with data alone.
7. **`SECRET_KEY` is generated and persisted** to `instance/.secret_key` when
   the env var is absent, rather than shipping a hardcoded dev secret.
8. **Admin Profile pages are read-only** (Phase 1). Spec §6.2 says "Profile
   (view own info) + Change Password", and §3 says only the City Hall Admin
   edits accounts — so the prototype's "Save changes" form was removed rather
   than wired up. Say the word if admins should be able to edit their own name
   and contact number.
9. **Idle timeout excludes collectors.** Spec §3 says "session timeout on
   inactivity for admin roles"; timing out a collector mid-route would lose
   field work.
10. **Demo accounts are opt-in** via `python seed.py --demo`, since User
    Management does not exist until Phase 2 and there would otherwise be no way
    to view the barangay or collector portals.
11. **Image proofs live outside `static/`.** Spec §8 names
    `/static/uploads/proofs/`, but it also requires that only admins and the
    owning collector can view a proof — and anything under `static/` is served
    to anyone who guesses the URL. They are stored in `uploads/proofs/` and
    served by a permission-checked route instead. The two requirements cannot
    both hold; the access rule is the one that matters.
12. **The truck's not-collected entry takes no image proof**, while the
    tricycle's requires one (OPEN ITEM 4, as agreed). A missed MRF pickup is
    verified by the barangay; a disputed household refusal is not.
13. **Barangay Admin History moved to a "Reports" group** in the sidebar,
    mirroring the City Hall portal, rather than sitting under Settings
    (OPEN ITEM 6).
14. **Anonymous reports store a salted hash of the IP, not the IP.** The spec
    says "device_fingerprint/ip"; a raw address next to a household name is
    identifying data the system does not need, and the hash rate-limits just
    as well.
15. **The public property list is a real disclosure.** The report flow needs a
    resident to pick their own household, so owner names, types, and puroks
    for a barangay are readable by anyone. That is what the spec asks for, and
    the list is trimmed to those three fields — no tags, notes, or addresses.
    Flagging it because it is a policy decision, not a technical one: say the
    word and I can put the flow behind a purok + partial-name match instead.
16. **Idle timeout still excludes collectors**, and the duty toggle does not
    expire either — a collector who forgets to go off duty stops appearing on
    the map after 10 minutes of no position (`STALE_AFTER_MINUTES`) rather than
    being forcibly signed out mid-route.

---

## What is left for you

The build is complete against the spec. Three things need **your data or your
decision**, none of which need code:

1. **The real geospatial data.** `data/geo/` still holds the marked
   placeholders. Follow `docs/DATA_REQUIREMENTS.md` — drop the files in and the
   maps pick them up without a restart. Until then the maps render tiles and
   say so honestly.
2. **Barangay numbering and purok lists.** Numbering is alphabetical and drives
   the four map zone colours; puroks are a uniform Purok 1–7. Both are data.
3. **The public property list** (decision 15 below) — whether owner names for a
   barangay should be publicly enumerable, as the report flow currently needs.

Deployment notes: set `SECRET_KEY` in the environment, run `python seed.py`,
and put a real WSGI server in front for anything beyond a LAN pilot — the
built-in server is fine for the deployment this was scoped for, but is not
hardened for the public internet.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Skeleton, config, storage, geo service, seed | ✅ Complete |
| 1 | Auth, RBAC, sessions, change password | ✅ Complete |
| 2 | City Hall: users, vehicles, schedule, assignments | ✅ Complete |
| 3 | Tricycle collector app + collection entry + image proof | ✅ Complete |
| 4 | Truck collector app + MRF pickups + deliveries + carry-overs | ✅ Complete |
| 5 | Barangay admin pages | ✅ Complete |
| 6 | Public viewer + anonymous reports + rate limiting | ✅ Complete |
| 7 | Socket.IO real-time, notification triggers | ✅ Complete |
| 8 | History, reports, Chart.js, offline queue, legacy cleanup | ✅ Complete |
