# CABADBARAN CITY GARBAGE COLLECTION TRACKING SYSTEM — FUNCTIONALITY SPEC

Companion to `project_summary_script.txt` (the UI/role spec). Build everything listed there; this file lists the functionalities, data, and logic needed to make it work. All clients are web: admins on desktop browsers, collectors and public viewers on mobile phone browsers (responsive web app, no native app).

## 1. TECH STACK
- Backend: Python 3 + Flask (blueprints per role: `public`, `brgy`, `city`, `collector_tri`, `collector_trk`, `auth`)
- Real-time: Flask-SocketIO (WebSockets) for live map, live notifications, and live dashboard counters
- Frontend: HTML5 + CSS3 + vanilla JS; mobile-first pages for collectors and public viewer, desktop layout for admins
- Map: Leaflet.js + OpenStreetMap tiles (free, no API key); barangay zones drawn as GeoJSON polygons with the 4 color groups (Brgy 1-6, 7-14, 15-22, 23-31)
- Chart: Chart.js for the Today's Waste Type chart
- Database: JSON files (one file per collection, see section 2) through a small storage module:
  - atomic writes (write temp file then `os.replace`)
  - a lock per collection file (threading/filelock) so concurrent writes never corrupt data
  - auto-create files with default structure on first run
  - all reads/writes go through this module only, so it can be swapped to SQLite later without touching routes if concurrency ever becomes a problem
- Passwords hashed with `werkzeug.security` (never stored in plaintext, including inside JSON files)
- Scheduler: APScheduler for the end-of-day summary job
- All timestamps in Asia/Manila

## 2. JSON DATABASE COLLECTIONS
- `users.json` — id, full_name, username, password_hash, role (`city_admin` / `barangay_admin` / `tricycle_collector` / `truck_collector`), assigned_barangay (or list of barangays for truck), assigned_vehicle, contact_number, status (Active/Inactive), date_created
- `barangays.json` — the 31 barangays: id, name, color zone group, puroks[], mrf {name, lat, lng}
- `vehicles.json` — registered units: id, type (tricycle/truck), code (TRI-01…, TRK-01…), status (available/assigned/inactive)
- `assignments_tricycle.json` — collector_id, barangay, purok_coverage[], tricycle_code, status (Active/Temporary Replacement), note, created_at
- `assignments_truck.json` — operator_id, covered_mrfs[] (barangay ids), truck_code, status, note, planned_pickup_times {barangay_id: "HH:MM"} (needed for the "2 hours from now TRK-01 will arrive" notification), created_at
- `properties.json` — id, owner_name, type (House/Establishment), barangay, purok, tag/note (e.g. "None Composting", "Has Special Waste"), created_by, date_created
- `collections.json` — one record per property per date: property_id, date, status (Collected/Not Collected), collector_id, tricycle_code, gps {lat,lng}, timestamp, waste [{type, unit (sack/kilo), qty}], reason, image_proof_path, note, source (collector/public_report)
- `mrf_pickups.json` — barangay, date, source_schedule_day, waste_type, load summary, truck_code, status (Pending/Collected from MRF/Not Collected), gps, timestamp, reason, note
- `deliveries.json` — truck_code, date, time, mrfs_included[], load totals per waste type, gps
- `carry_overs.json` — barangay, source_schedule_day, waste details, original_truck, current_truck (null until reassigned), status (Pending/Collected), reschedule_date
- `waste_schedule.json` — Monday–Sunday: waste_type, details[] (examples like Kitchen Waste, Yard Waste)
- `unavailable_requests.json` — user_id, role, affected_date, unavailable_until, reason, notes, status (Pending/Resolved)
- `notifications.json` — id, audience (role / barangay:<id> / user:<id> / public), type, message, created_at, read_by[]
- `public_reports.json` — anonymous feedback: barangay, purok, property_id, status_reported, comment, device_fingerprint/ip, created_at
- `history.json` — frozen per-day summaries (citywide and per barangay)

## 3. AUTHENTICATION & RBAC
- One `/login` endpoint for all roles; after login redirect by role (city admin dashboard, brgy admin dashboard, tricycle Property page, truck MRF page). Public viewer pages need no login.
- Flask session auth; "Remember me" extends the session to ~30 days, otherwise browser-session cookie.
- `@role_required(...)` decorator on every route AND on every SocketIO event handler; a barangay admin/collector can only ever read or write data of their own barangay/assignment (enforce server-side on every query, never trust the client).
- Route access matrix: Public pages → everyone; Brgy pages → barangay_admin (own barangay only); City pages → city_admin; collector pages → the matching collector role (own data only).
- Inactive accounts cannot log in.
- Forgot Password link → static message "Contact the City Hall Admin to reset your password" (the admin already has the Reset Password action; no email flow in scope).
- Change Password (all logged-in roles): requires the correct current password, new + confirm must match, min length 8.
- Only the City Hall Admin can create/edit/reset/delete users. MISSING LOGIC ADDED: a seed script must create the very first City Hall Admin account (plus barangays, vehicles, and a default waste schedule), since accounts can only be made by an admin.
- CSRF protection on all forms; server-side validation on all inputs; session timeout on inactivity for admin roles.

## 4. REAL-TIME ENGINE (Socket.IO)
Rooms: `public`, `barangay:<id>`, `role:city_admin`, `user:<id>`. Clients join rooms based on their verified session role.
- `location_update` — collector's phone sends GPS via the browser Geolocation API (`watchPosition`, throttled to every 5–10 s) while On Duty; server stores last known position and broadcasts to `public`, the collector's `barangay:<id>`, and `role:city_admin`. This drives every live map.
- `collector_status` — On Duty / Off Duty toggle; drives the "Active Now" counters (23 Tricycles, 4 Trucks) and shows/hides the marker on the maps.
- `collection_saved`, `mrf_pickup_saved`, `delivery_saved`, `carry_over_created` — update dashboard counters, recent-activity tables, load totals, and charts live on every open admin screen.
- `notification_new` — pushes to the bell icon (unread badge count) and to the notification cards in real time.
- `schedule_updated` — when the City Hall Admin edits the waste schedule, all clients refresh their schedule displays.
- Fallback: if the socket disconnects, clients poll a REST endpoint every 30 s and re-join rooms on reconnect.

## 5. NOTIFICATION TRIGGERS (auto-generated, real time)
- Truck approaching: haversine distance between each on-duty truck's GPS and the MRF coordinates of its covered barangays; when distance < 500 m, send "Truck TRK-02 approaching Brgy 14 MRF" to that barangay room + city admin. Fire once per MRF per day (flag it so it does not spam).
- Scheduled arrival reminder: from `planned_pickup_times`, at T-2 hours send "2 hours from now the garbage collector TRK-01 from City Hall will arrive" to the barangay room.
- Unavailable request submitted → notify city admin.
- Assignment created/changed (incl. Temporary Replacement) → notify the affected collector(s).
- Truck marks Not Collected → carry-over created → notify city admin (and the barangay).
- Public report submitted (resident says not collected) → notify that barangay admin + city admin.
- Delivery to landfill completed → notify city admin.
- Bell dropdown for logged-in roles: list, unread count, mark-as-read (per user in `read_by`).

## 6. FUNCTIONALITIES PER ROLE

### 6.1 Public Viewer (no login, phone browser)
- Live map: real-time markers of all on-duty tricycles and trucks, barangay zone polygons, filter tabs (All / Tricycles / Trucks), barangay dropdown, legend bar with live counts, fullscreen.
- Waste Collection Schedule page: month calendar (prev/next months, today highlighted) + list of each day with its waste type, generated from `waste_schedule.json`.
- Today's Waste page: current date, cards of today's waste types with details/examples.
- Anonymous feedback (the "Note" card): form flow — pick Barangay → pick Purok → pick the property/owner (searchable list) → mark Collected or Not Collected + optional comment → creates a `public_reports` record and, if Not Collected, sets today's collection status of that property to Not Collected with source `public_report`. MISSING LOGIC ADDED: rate limit (e.g. max 3 reports per device/IP per day) since it is anonymous, and the report is flagged in the admin log as "Reported by resident" so admins can distinguish it from collector entries.
- Simple tab navigation between the two pages.

### 6.2 Barangay Admin (web, per-barangay scope)
- Login/logout.
- Dashboard: live mini-map of own barangay tricycles; notifications card (truck schedule/approach alerts); status counters — Collected / Pending / Not Collected out of total registered properties; recent collection log (Time, Purok, TRI no., Status, Reason, View modal with the full entry incl. image proof).
- Property List: Total Property counter; add-property form (owner name, House/Establishment, barangay auto-locked to own, purok, optional tag); records table sorted newest first. MISSING LOGIC ADDED: Edit and Delete actions on own-barangay properties (with confirm), since typos will happen.
- Waste Schedule: read-only weekly table from the City Hall schedule.
- Live Tracking: full map of own barangay tricycles + City Hall truck arrival notifications.
- Collections: status counters; Overall Collected Load auto-computed per waste type (sacks and kilos totalled separately, e.g. "35 sacks and 5 kg") from today's Collected entries; collectors table (Name, TRI no., View → modal of that collector's totals); date filter (default today, can pick past dates/months).
- History: per-day summary records of own barangay.
- Profile (view own info) + Change Password.
- Every query server-side filtered to own barangay (RBAC scope guard).

### 6.3 City Hall Admin (web, full scope)
- Login/logout.
- Dashboard (all counters live via sockets): Property Collection card (collected/pending/not collected, % complete, View Household Monitoring → Property page); MRF Collection card (collected/pending/missed pickup, %, View MRF Totals → MRF page); Active Collectors card (live counts, View Live Monitoring → Live Tracking page); live map card with All/Tricycles/Trucks + barangay filters and fullscreen; Today's Schedule card (weekly types with Today/Tomorrow/In X Days badges, View Full Schedule); Recent Collection Activities table (Time, Barangay, Collector, Type, Status, Reason, View) with search + All Status + All Types filters; Today's Waste Type chart (Chart.js) with per-type sack/kilo totals.
- User Management: create-user form with validations (required fields, unique username, password match, role-dependent fields — barangay for brgy admin/tricycle, vehicle for collectors); user list with search + Roles/Barangay/Status filters; actions View modal, Edit, Reset Password (admin sets a new one), Delete with confirmation. Creating a collector account should also make the vehicle selectable/linked.
- Waste Schedule: EDITABLE weekly table (per day set Waste Type + details/examples); saving broadcasts `schedule_updated` to every client (brgy admins, collectors, public viewer). MISSING LOGIC ADDED: this page is where the schedule is managed, not just displayed.
- Tricycle page: 4 live counters (Total, Active Assignments, Unavailable Requests, Barangays Covered); assignment table with search + Barangay/Status/Availability filters and View/Edit actions; Assign form (collector dropdown = tricycle collector accounts, purok coverage multi-select, tricycle unit dropdown, Active/Temporary Replacement radio, note). Validations: a collector can hold only one active assignment, a tricycle unit can only be assigned once, warn on purok coverage overlaps.
- Truck page: same pattern — counters, table (Status/Availability filters), Assign form (operator, truck unit, covered barangay MRFs multi-select, status radio, note, optional planned pickup time per MRF for the arrival reminders).
- MISSING LOGIC ADDED (both pages): a small Vehicle registry (add/deactivate TRI-XX and TRK-XX units) — the assign dropdowns need registered units to come from somewhere; can live as a modal/section on the Tricycle and Truck pages or be seed data.
- Property (Household Monitoring): 3 counters; records table of all barangays with Date/Barangay/Purok/Property Type/Collectors/Status filters and View modal.
- MRF: 4 counters; Overall Collected Load card (auto totals across MRFs); MRF pickup table (Date/Barangay/Truck/Status filters, View); Delivered to Landfill table (View).
- Carry-Over: records table; Reassign button (pick another truck → sets current_assigned_truck, notifies that operator) and Reschedule button (pick a new date); carry-over auto-closes when the assigned truck marks it Collected.
- History & Reports: per-day summary feed; Generate Report form (Report Type dropdown — Property Collection / MRF Collection / Deliveries / Carry-Overs / Collector Performance; Start + End date) → output is a printable HTML report page plus a CSV download (concrete output format, since the mockup only says "Generate").
- Profile + Change Password.

### 6.4 Tricycle Collector (phone browser)
- Login/logout. MISSING LOGIC ADDED: an On Duty / Off Duty toggle on the main page — On Duty starts browser GPS sharing (with a graceful prompt if location permission is denied) and marks the collector Active on all live maps and counters; Off Duty stops it.
- Property page: Today's Schedule cards (day/date + today's waste type); property table of own barangay + purok coverage (Name, Property, Purok, live Status); tap an owner → collection entry page.
- Collection entry (Collected): NAME SELECTED card (owner, purok, tag) with status dropdown = Collect; auto-captured GPS, timestamp, TRI no.; Waste Quantity rows (type, Sack/Kilo unit, qty); SAVE. Validation: at least one quantity > 0. Saving updates the barangay + city totals in real time.
- Collection entry (Not Collected): status dropdown = Not Collected; required Reason dropdown (seed reasons: Not segregated properly, No garbage taken out, Road inaccessible, Has special/hazardous waste, Other); required image proof upload (jpg/png, max ~5 MB, compress client-side before upload); optional note; SAVE.
- MISSING LOGIC ADDED: the collector can re-open and correct their OWN entry for the CURRENT day only (mistakes happen in the field); past days are locked.
- Unavailable for Duty: own info card + request form (affected date, optional until date, reason, notes) → creates request, notifies city admin, sets availability to Unavailable on the affected date(s).
- History: search + date filter; per-entry record cards.
- Profile (read-only account info) + Change Password.

### 6.5 Truck Collector (phone browser)
- Login/logout + the same On Duty toggle with GPS sharing.
- MRF page: Today's Schedule cards; Overall Collected Load card — running totals of today's picked-up MRFs (sacks and kilos separate) + "Deliver to Landfill" button → creates a delivery record (current load + list of included MRFs + GPS/time), notifies city admin, and resets the running load so a second trip the same day starts clean; Assigned Barangay MRF cards (status badge, source schedule day, waste type, per-type load breakdown pulled automatically from that barangay's collection totals for the source day); tap a card → pickup entry.
- Pickup entry (Collected): SELECTED BARANGAY card with Collect dropdown; auto GPS/timestamp/TRK no.; SAVE button (MISSING IN MOCKUP — add it, same pattern as the tricycle app); no quantity inputs since the load comes from the barangay data.
- Pickup entry (Not Collected): required Reason dropdown + optional note + SAVE → marks the MRF Not Collected, counts as Missed Pickup, and AUTO-CREATES a carry-over record for the City Hall Admin to reassign/reschedule.
- Unavailable for Duty, History (MRF Pickup History + Final Disposal Delivery History sections), Profile + Change Password — same patterns as the tricycle app.

## 7. CORE SYSTEM LOGIC (cross-role rules, including added missing logic)
- Status model: a property/MRF is Pending for a date simply when no record exists for that date; saving an entry makes it Collected or Not Collected. Nothing is "reset" at midnight — every view of "today" just queries today's date (Asia/Manila), so history is automatic.
- Expected totals: the "x / 1,496" style denominators = count of registered properties (per barangay or citywide); MRF denominators = 31.
- Aggregation chain (all computed, never hand-entered twice): tricycle entries → barangay Overall Collected Load → that barangay's MRF card load shown to the truck → truck's running Overall Collected Load → delivery totals → city MRF page totals. Sacks and kilos are always totalled separately, never added to each other.
- Carry-over lifecycle: truck Not Collected → carry_over(Pending, current_truck = null) → admin Reassign and/or Reschedule → the assigned truck sees it as a pending pickup on the rescheduled date → marking it Collected closes the carry-over and its load joins that day's totals.
- Temporary Replacement: while a replacement assignment is active, the replacement collector sees that route/property list; the original collector sees none. Reverting is a manual admin action (set original back to Active).
- Unavailable requests: on the affected date(s) the collector automatically shows as Unavailable; the request is marked Resolved when the admin reassigns or the period ends.
- End-of-day job (APScheduler, runs just after midnight): writes yesterday's citywide and per-barangay summary into `history.json` (counts, totals, missed pickups, deliveries); optionally auto-marks MRFs that got no status all day as Missed so they enter carry-over.
- Public "Not Collected" reports never overwrite a collector's Collected entry silently — if a conflict happens (collector says Collected, resident says Not Collected), keep both and flag the row as Disputed for the barangay admin to review. (MISSING LOGIC ADDED — conflict handling.)

## 8. NON-FUNCTIONAL / CROSS-CUTTING
- Fully responsive: collector + public pages designed mobile-first (thumb-friendly buttons, no hover-only actions); admin pages desktop-first but usable on tablets.
- Every table: search, filters as specced, newest-first sorting, sensible empty states.
- Confirmation modals for every destructive action (delete user, delete property).
- Image proofs stored in `/static/uploads/proofs/YYYY-MM-DD/` with random filenames; only admins and the owning collector can view them.
- Client-side AND server-side validation; friendly inline error messages.
- Seed script: 31 barangays with real names, coordinates, puroks, and MRF locations; TRI/TRK vehicle units; default weekly waste schedule (Mon Biodegradable+Residual, Tue Recyclable, Wed Biodegradable+Residual, Thu Residual, Fri Biodegradable+Residual, Sat Special Waste); one City Hall Admin account.
- Socket security: room joins validated against the session role; location updates accepted only from the authenticated collector they belong to.
- Audit fields (created_at, created_by, updated_at) on every record.
- Handle flaky mobile connections: queue an unsent entry in localStorage and retry submission automatically when back online (nice-to-have but cheap to add).
- Single Flask app entrypoint, config for host/port, runs on LAN or a small VPS.
