# Improved Prompt — Cabadbaran Garbage Collection Tracking System

Copy the block below into your coding agent (Claude Code, Cursor, etc.) from the project root.

---

## MAIN PROMPT

You are working on the **Cabadbaran City Garbage Collection Tracking System**. The full specification lives in the `instructions/` folder at the project root (`FUNCTIONALITIES.md` = tech stack, data model, and logic; `project_summary_script.txt` = UI/role spec from the mockups). Treat both as the source of truth — `FUNCTIONALITIES.md` wins wherever they conflict.

### TECH STACK — HARD CONSTRAINT

The entire system is:

- **Backend:** Python 3 + Flask (blueprints per role), Jinja2 templates
- **Database:** JSON files only — no SQL, no ORM, no external DB service
- **Frontend:** plain HTML + CSS + vanilla JavaScript. No React, Vue, Tailwind, Bootstrap, TypeScript, npm, or any build step
- **Mobile:** the collector and public-viewer pages are **responsive web pages opened in a phone browser**. There is no native app, no React Native, no Flutter, no app store build. GPS comes from the browser Geolocation API, the camera from `<input type="file" accept="image/*" capture>`

Permitted browser-side libraries, loaded by CDN `<script>` tag only (no npm, no bundler): **Leaflet.js** for the map and **Chart.js** for the one dashboard chart. Permitted Python packages: **Flask**, **Werkzeug** (password hashing), and **Flask-SocketIO** for real time.

Do not introduce any dependency outside this list. If you believe something genuinely cannot be built within it, stop and ask me first.

Work in four steps. **Do not skip ahead, and do not write any code until Step 3 is approved.**

### STEP 1 — Read the spec

Read every file in `instructions/` completely. Then produce a compact inventory of:

- The required tech stack and any dependency not yet installed
- Every JSON collection and its fields
- Every page, grouped by role (Public Viewer, Barangay Admin, City Hall Admin, Tricycle Collector, Truck Collector)
- Every Socket.IO event and notification trigger
- Every cross-role business rule in the "CORE SYSTEM LOGIC" section
- Every item under "OPEN ITEMS / TO FINALIZE" and every "MISSING LOGIC ADDED" note

### STEP 2 — Audit what already exists

Scan the whole repository (ignore `node_modules/`, `venv/`, `.git/`, build output) and report:

- Current folder structure, entry point, and how to run it
- Which spec features are **done**, **partially done**, or **missing**
- Any existing code that contradicts the spec (wrong stack, hardcoded data, missing RBAC, direct file reads that bypass a storage module)
- Anything that is currently broken or won't boot

Output this as a gap table: `Feature | Spec reference | Status | File(s) | Notes`.

### STEP 3 — Plan, then stop

Give me:

1. A **phased build order** with small, independently testable phases. Suggested sequence unless the audit says otherwise:
   - Phase 0 — project skeleton, config, JSON storage module (atomic writes + per-file locks), seed script
   - Phase 1 — auth, RBAC decorators, session handling, change password
   - Phase 2 — City Hall Admin: user management, vehicle registry, waste schedule, assignments
   - Phase 3 — Tricycle Collector app (mobile) + collection entry + image proof
   - Phase 4 — Truck Collector app + MRF pickups + deliveries + carry-overs
   - Phase 5 — Barangay Admin pages
   - Phase 6 — Public Viewer pages + anonymous reports + rate limiting
   - Phase 7 — Socket.IO real-time layer, notification triggers, APScheduler end-of-day job
   - Phase 8 — History, reports (printable HTML + CSV), polish, offline queue
2. Every ambiguity you hit, as a numbered question with your recommended answer. **Do not silently decide the "OPEN ITEMS" for me** (tricycle count 23 vs 4-per-barangay, TRI/TRK naming, "Food Waste" vs "Kitchen Waste", truck app SAVE button and image proof, etc.).

Then **stop and wait for my approval.** After I approve, implement one phase per turn and ask before starting the next.

### STEP 4 — Implementation ground rules

- Stay inside the tech stack constraint above. No frontend framework, no CSS framework, no build step, no extra pip packages without asking.
- The end-of-day summary job runs **lazily**, not on a background scheduler: on the first request of a new day, if `history.json` has no frozen summary for the previous day, compute and write it then. This avoids a scheduler dependency and survives the server being off overnight.
- Every read/write goes through the storage module — never `open()` a JSON collection directly from a route.
- Enforce RBAC server-side on every route **and** every socket event; a barangay admin or collector may only touch their own scope. Never trust the client.
- Hash passwords with `werkzeug.security`. No plaintext anywhere, including seed data.
- All timestamps in Asia/Manila. Audit fields (`created_at`, `created_by`, `updated_at`) on every record.
- Mobile-first for collector and public pages; desktop-first for admin pages.
- Client-side **and** server-side validation on every form.
- Do not delete or refactor working code without flagging it first.
- Every phase must end with the app still booting and running.

---

## MAPPING & HOTSPOT LAYER — SPECIAL HANDLING

I do **not** have the real barangay boundary data, MRF coordinates, or hotspot data yet. Build the map so that real data can be dropped in later **without editing a single route, template, or JS file.**

**1. Single source of truth.** All geospatial data loads at runtime from `data/geo/` through one module (`services/geo_service.py`):

| File | Contents |
|---|---|
| `barangay_zones.geojson` | 31 polygons; each feature has `barangay_id`, `name`, `zone_group` (the 4 colour groups: Brgy 1–6, 7–14, 15–22, 23–31) |
| `mrf_locations.json` | `{ barangay_id, name, lat, lng }` per barangay |
| `hotspots.geojson` | points or polygons with `hotspot_id`, `barangay_id`, `purok`, `severity`, `type`, `last_reported`, `notes` |

**2. Nothing hardcoded.** No coordinates, polygon arrays, or zone colours inside templates or JS. The frontend fetches everything from REST endpoints:
`GET /api/geo/barangays`, `GET /api/geo/mrfs`, `GET /api/geo/hotspots?barangay=&from=&to=&severity=`

**3. Config-driven, in `config.py`:**
- `HOTSPOT_LAYER_ENABLED` — default `False`
- `MAP_DEFAULT_CENTER` and `MAP_DEFAULT_ZOOM` — default to Cabadbaran City centre
- `HOTSPOT_SOURCE` — `"file"` (reads `hotspots.geojson`) or `"derived"` (computes hotspots from existing data: frequency of Not Collected entries + public reports per purok over a date range). Implement both; default to `"derived"` so the layer shows something real before external data arrives.

**4. Placeholder data + graceful degradation.** Ship the three files with valid, clearly-marked placeholder content (`"_placeholder": true`). If a file is missing or empty: the map still renders tiles, zones, and live vehicle markers, the hotspot toggle shows a "No hotspot data loaded yet" empty state, and nothing crashes.

**5. Layer isolation.** The hotspot layer is its own toggleable Leaflet layer (via LayerControl), stacked above the zone polygons and below the live vehicle markers, so it never interferes with live tracking.

**6. Write `docs/DATA_REQUIREMENTS.md`** telling me exactly what data to collect, in what format, with one fully filled-in example barangay, and the exact steps to swap the real files in.

---

## DEFINITION OF DONE (every phase)

- `python app.py` boots with no errors; the seed script is idempotent and safe to re-run
- Every page in the phase renders with real data from the JSON store
- Every validation and permission rule listed for that phase actually triggers when violated
- No hardcoded coordinates, credentials, or sample data left in the code
- `docs/PROGRESS.md` updated with: what is complete, what is stubbed, what is next, and any decision you made on my behalf

---

## SHORT VERSION (if you want a lighter first message)

> Read every file in `instructions/` — that's the full spec for this Cabadbaran garbage collection tracking system. Then scan the existing project and give me: (1) a gap table of spec features vs what's already built, (2) a phased build order of small testable chunks, and (3) a numbered list of ambiguities with your recommended answers, including everything under "OPEN ITEMS / TO FINALIZE". Don't write any code yet — stop after the plan and wait for my approval.
>
> Stack is fixed: Python Flask + Jinja templates + JSON files for storage + plain CSS and vanilla JS. No React, no CSS framework, no build step, no SQL. The collector and public pages are responsive web pages opened in a phone browser — there is no native app. Only Leaflet and Chart.js by CDN, and Flask-SocketIO on the Python side.
>
> One constraint to design in from the start: I don't have the real barangay boundaries, MRF coordinates, or hotspot data yet. Load all geospatial data at runtime from `data/geo/` (`barangay_zones.geojson`, `mrf_locations.json`, `hotspots.geojson`) through a single `geo_service` module, serve it via `/api/geo/*` endpoints, gate the hotspot layer behind a `HOTSPOT_LAYER_ENABLED` config flag, ship marked placeholder files so nothing crashes when data is missing, and write `docs/DATA_REQUIREMENTS.md` telling me exactly what data to collect and how to drop it in.

---

## RESUME PROMPT (for later sessions)

> Read `instructions/` and `docs/PROGRESS.md`, then continue with Phase N. Same rules as before: one phase per turn, stop and ask before starting the next, no hardcoded geo data, update `PROGRESS.md` when done.
