# Cabadbaran City — Garbage Collection Tracking System

A rebuild of the GCTS design reference as a responsive, accessible Flask
application. Four role-based portals share one app shell, plus a public page:

| Portal | Prefix | Screens |
| --- | --- | --- |
| City Hall Admin | `/city` | Dashboard, User Management, Waste Schedule, Tricycle, Truck, Live Tracking, Property, MRF, Carry-Over, History & Reports, Profile |
| Barangay Admin | `/barangay` | Dashboard, Property List, Waste Schedule, Live Tracking, Collections, History, Profile |
| Tricycle Collector | `/collector/tricycle` | Property route, Record stop, Unavailable for Duty, History, Profile |
| Truck Collector | `/collector/truck` | MRF route, Record pickup, Unavailable for Duty, History, Profile |
| Public Viewer | `/public` | Collection calendar, today's accepted waste, live tracking, reminders — **no login** |

## Run it

```bash
pip install -r requirements.txt
python seed.py --demo                    # barangays, vehicles, schedule, accounts
python tools/make_demo_geo.py            # illustrative map geography (see note)
python tools/make_demo_day.py --days 5   # a working week of activity
python app.py
```

Run them in that order. `make_demo_geo.py` has to come before
`make_demo_day.py`, because the GPS positions that put vehicles on the live map
are derived from the barangay coordinates — without it the map has nothing to
draw and the vehicles have nowhere to stand.

Then open <http://127.0.0.1:5000> — or <http://127.0.0.1:5000/public/> for the
citizen-facing page, which needs no login.

**Logins:** `city_admin`, `brgy_admin`, `tri_collector`, `truck_collector` —
all with the password `Cabadbaran2026`. Full table and how to change it:
[docs/CREDENTIALS.md](docs/CREDENTIALS.md).

### Why the demo-data step

A freshly seeded system is correct but empty: every counter reads 0, every
table shows its empty state, and the chart has nothing to draw. That is the
right behaviour for a new install and the wrong thing to present.
`tools/make_demo_day.py` writes the records a working week would have produced
— properties, collectors, routes, collection entries, MRF pickups, landfill
deliveries, a carry-over, resident reports, vehicles on the live map — through
the same storage module the app uses, so none of it is a special case the real
code knows about. It is additive and idempotent per date, and
`--reset` removes every trace of it.

Skip it for a clean install; run it before a presentation.

## First run, without the demo data

```bash
pip install -r requirements.txt
python seed.py            # 31 barangays, vehicles, schedule, first admin
python app.py
```

`seed.py` writes the geo files as **empty stubs** — 31 barangays with no
geometry — so the maps report "boundaries have not been loaded yet" rather than
inventing a city. `python tools/make_demo_geo.py` fills them with clearly
labelled approximations so the maps work while you collect the real thing;
`python tools/geo_from_csv.py` imports the real thing when you have it. See
[docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md).

`seed.py` gives every account it creates the same known password, so a fresh
local store and a fresh deploy have **identical logins** — see `seed_password()`
in `seed.py`, and `docs/CREDENTIALS.md` for the table. Set
`SEED_ADMIN_PASSWORD` or `SEED_DEMO_PASSWORD` to override it per environment.
Re-running the script is safe: it never duplicates a record and never
overwrites an edit made through the UI.

Nobody is forced to change their password — seeded accounts are created with
`must_change_password: False`, and the Profile page offers a change to anyone
who wants one. (An admin-initiated reset in User Management does prompt that
user at their next login.)

Every other account is created by the City Hall Admin in User Management. To
walk through the barangay and collector portals before that page exists, run
`python seed.py --demo` for one account per role.

There is a single `/login` for all roles; the account's role decides where it
lands. Public viewer pages need no account.

> **Design folder naming.** The two collector folders in `designs/` are
> swapped: `tricycle-garbage-collector/` contains the **truck** screens (MRF
> pickup, "Deliver to Landfill", TRK-01) and `truck-garbage-collector/`
> contains the **tricycle** screens (household collection, purok, TRI-01).
> The build follows each screen's actual content, not the folder name.

## Tech

- **Backend:** Python + Flask (app factory, blueprints per role), Flask-SocketIO
- **Frontend:** HTML5, CSS3, Jinja2, vanilla JavaScript — no frameworks, no build step
- **Data:** JSON files under `data/`, all access through `services/storage.py`
  (atomic writes, per-file locks). No route opens a data file directly.
- **Geospatial:** OpenStreetMap tiles and Leaflet — free, no API key, no paid
  map service anywhere. Every coordinate loads at runtime from `data/geo/`
  through `services/geo_service.py` and is served by `/api/geo/*`, so real data
  drops in with no code change. Ships with clearly-labelled placeholder
  geography for all 31 barangays so the maps work today. To supply the real
  boundaries and MRF locations, fill in a spreadsheet and run
  `python tools/geo_from_csv.py` —
  [docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md) has the whole
  procedure and says exactly what to collect.
- **Auth:** `werkzeug` password hashing, single `/login`, CSRF on every form,
  server-side role and scope checks on every route.
- **Real-time:** Socket.IO rooms joined from the verified session. Leaflet and
  Chart.js by CDN with subresource-integrity hashes. Every one of these is an
  enhancement — the app works fully without them, falling back to polling.

## Tests

```bash
python tests/run_all.py
```

464 checks across nine suites, each a standalone script with no test
dependency. The phase 1 suite runs against the live store — it has to, since
what it verifies is the real login flow — but it now restores the passwords it
found, so a test run no longer changes your logins.

Build status and decisions: [docs/PROGRESS.md](docs/PROGRESS.md).
Deploying to Render: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — read the first
two sections before you deploy, they cover a data-loss trap and a setting that
must not be changed.

## Layout

```
app.py                  App factory, filters, error handlers, Socket.IO
config.py               All tunables: map centre, hotspot flags, limits
seed.py                 Idempotent seed script (--demo for sample accounts)
requirements.txt
tests/run_all.py        Every phase's checks
services/
  storage.py            JSON store: atomic writes, per-file locks
  auth_service.py       Password hashing and account rules
  validation.py         Shared server-side form validation
  geo_service.py        Single source of truth for coordinates
  timeutil.py           Asia/Manila time helpers
  user_service.py       Accounts     vehicle_service.py   Vehicle registry
  assignment_service.py Routes       schedule_service.py  Weekly schedule
  property_service.py   Households   collection_service.py Stop records
  mrf_service.py        MRF pickups  carryover_service.py Carry-over lifecycle
  duty_service.py       On/off duty  history_service.py   Daily summaries
  report_service.py     Reports      notification_service.py The bell
  public_report_service.py Anonymous reports + disputes
  realtime.py           Socket rooms and events
  triggers.py           The eight automatic notifications
blueprints/
  api_geo.py            /api/geo/* endpoints
  api_live.py           /api/live/vehicles
  sockets.py            Socket.IO handlers
  auth.py               Login, logout, CSRF, @role_required
  city.py               City Hall routes + sidebar model
  brgy.py               Barangay routes + sidebar model
  collector.py          Both field-collector portals
  public.py             Public viewer (no auth)
static/
  css/
    tokens.css          Colour, type, space, elevation, motion + dark theme
    base.css            Reset, typography, a11y primitives, print
    layout.css          App shell, rail, topbar, grids, responsive drawer
    components.css      Cards, buttons, forms, tables, modals, toasts…
    pages.css           Login, live map, donut, schedule, profile, errors
    roles.css           Collector route/record UI, public calendar
  js/
    app.js              Theme, drawer, popovers, modals, toasts, forms,
                        reveal panels, steppers, geolocation, file preview
    tables.js           Declarative search / filter / sort
  img/logo.jpg
templates/
  base.html             <head>, flash toasts, script tags
  shell.html            Authenticated layout (rail + topbar + page slot)
  error.html
  auth/login.html
  partials/             icons, macros, rail, topbar, schedule_table,
                        record_form, unavailable_form, collector_profile
  city-hall-admin/      11 page templates
  brgy-admin/           7 page templates
  tricycle-collector/   5 page templates
  truck-collector/      5 page templates
  public-viewer/        1 page template
```

### Shared partials

The two collector portals are structurally identical — a work list, a
per-stop record form, an unavailability request, history, and a profile —
so they share one blueprint and three partials (`record_form.html`,
`unavailable_form.html`, `collector_profile.html`), switched by a `kind`
variable. Only the unit of work differs: a household for the tricycle, a
barangay MRF for the truck.

## Design system

**Brand palette** — sampled from the official city seal in `designs/logo.jpg`:

| Role | Hex | Source |
| --- | --- | --- |
| Primary | `#0e6ba8` | Seal ring blue |
| Secondary | `#2e9e52` | Relief map green |
| Accent | `#e0ae3a` | Rope border gold |
| Danger | `#ce3f3f` | Seal stars red |
| Rail | `#071d2e` | Deepened seal navy |

Every token lives in `static/css/tokens.css`. All text/background pairings meet
WCAG 2.1 AA (≥ 4.5:1).

**Typography** — a system-native stack (Segoe UI Variable / -apple-system /
Inter / Roboto). No web-font request, so there is no FOUT and no external
dependency. Headings use the display face with tight tracking; all numeric data
uses `tabular-nums` so columns align.

## Responsive behaviour

- **> 1024px** — fixed 264px sidebar, fluid content, multi-column grids.
- **≤ 1024px** — sidebar becomes an off-canvas drawer with a scrim, focus trap,
  and Escape-to-close. A hamburger appears in the topbar.
- **≤ 860px** — the weekly schedule stacks into day blocks.
- **≤ 760px** — every data table converts to stacked cards, each cell labelled
  from its `data-label`. Date/time and the user's name drop out of the topbar.
- **≤ 560px** — modals present as bottom sheets; toasts span the viewport.

**The collector portals were designed the other way round.** Their reference
mockups are phone-only, so those screens start as a single mobile column and
*gain* structure with width: the route list and its progress/load summary sit
side by side from 1180px, and the record screen splits into identity + capture
on the left and the result form on the right. The save bar is sticky on a
phone and inline at a desk. The public viewer is single-column on mobile and
two-column from 960px.

Verified: **93 page loads** across 390px, 768px, and 1440px — no horizontal
scrolling, no HTTP errors, no JavaScript errors on any page.

## Accessibility

- Skip link, landmark regions, and one consistent `:focus-visible` ring.
- `aria-current="page"` on the active nav item; every icon-only control is labelled.
- Tables use `<caption>`, `scope`, and `aria-sort` on sortable headers.
- Flash messages are announced via `role="status"` / `aria-live="polite"`.
- Progress bars expose `role="progressbar"` with value/min/max.
- Status is never colour-only — every badge pairs a dot with a text label.
- `prefers-reduced-motion` disables transforms and animation.
- `prefers-color-scheme` drives the theme; the toggle persists an override.
- Forms use native constraint validation, enhanced with inline `role="alert"`
  messages and cross-field password matching.

Additions for the collector screens:

- The mockups' "Collect ▾" dropdown became two large radio targets. The choice
  is binary, consequential, and made outdoors wearing gloves — it should not
  cost two taps.
- Quantity entry uses a −/+ stepper around a number input, so a value can be
  set without opening a keyboard.
- **Location capture is real**: the "Capture current location" button uses
  `navigator.geolocation` and reports accuracy, with named messages for
  permission-denied, unavailable, and timeout.
- Hidden branches of the record form have their fields `disabled`, so a
  `required` control in the collapsed branch can never block submission.

## Notes

- The **live map** is Leaflet with OpenStreetMap tiles, loaded from a CDN in
  `templates/partials/map_assets.html`. Both are free and need no API key — but
  they are a runtime dependency on `unpkg.com`, so the map needs internet.
- **Photo proof is stored**, deliberately outside `static/`, and served by the
  permission-checked `/collector/proofs/<path>` route so that only an admin or
  the owning collector can open one.
- The **donut chart** is a pure CSS `conic-gradient` — no charting library.
- The store is **JSON files on disk** — not a database, and not in memory.
  Reads scan the file; writes take an OS file lock. Comfortable at city scale;
  `docs/DEPLOYMENT.md` covers when to move to SQLite.
- Passwords exist **only as `werkzeug` hashes**. There is no plaintext password
  anywhere in the tree, the JSON store included.
- `SECRET_KEY` is **required** in production — the app refuses to start without
  it rather than generate a new one per restart. See `config.py`.
