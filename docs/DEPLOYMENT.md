# Deploying to Render

Configured for a **capstone demo on the free plan**. The short version:

```
plan            free
start command   python seed.py --demo && gunicorn app:app                   --worker-class gthread --workers 1 --threads 8                   --bind 0.0.0.0:$PORT
env vars        FLASK_ENV=production
                SECRET_KEY=<generate one>
                SEED_ADMIN_PASSWORD=<your choice>
                SEED_DEMO_PASSWORD=<your choice>
                PYTHON_VERSION=3.13.4
```

`render.yaml` sets all of this. Push the repo to GitHub, then in Render choose
**New → Blueprint**, point it at the repo, and fill in the two passwords when
prompted.

---

## Why a wipe does not matter here

Render's free plan has no persistent disk: the filesystem is rebuilt on every
deploy, every restart, and every time the service wakes from sleep. Anything
you create through the app — accounts, properties, collection records, photos
— is gone after that.

For a demo that is fine, **because `seed.py --demo` runs on every boot** and
rebuilds the whole thing: 31 barangays, 39 vehicles, the weekly schedule, four
working accounts, eight properties, and the collector assignments. So the app
is never empty, and it never looks broken.

The one thing that would break it is the logins changing. That is what
`SEED_ADMIN_PASSWORD` and `SEED_DEMO_PASSWORD` are for: with them set, the same
four accounts work no matter how many times the instance resets. Without them
`seed.py` generates fresh passwords on each boot and you are locked out of your
own demo.

**Set both before your first deploy.**

### The accounts you get

| Username | Role | Password |
|---|---|---|
| `city_admin` | City Hall Admin | your `SEED_ADMIN_PASSWORD` |
| `brgy_admin` | Barangay Admin (Poblacion 1) | your `SEED_DEMO_PASSWORD` |
| `tri_collector` | Tricycle Collector | your `SEED_DEMO_PASSWORD` |
| `truck_collector` | Truck Collector | your `SEED_DEMO_PASSWORD` |

---

## The one free-plan gotcha: cold starts

A free service **sleeps after 15 minutes of no traffic**, and waking it takes
roughly 50 seconds. If your panel opens the link cold, they watch a blank
loading screen for the better part of a minute before anything appears.

Two ways around it, neither costing anything:

1. **Open the site yourself 2–3 minutes before you present.** Simplest, and
   enough on its own.
2. **Point a free uptime monitor at it** (UptimeRobot and similar have free
   tiers) hitting `/login` every 10 minutes for the week of your defense. The
   service then never sleeps.

Do the first one regardless.

---

## Running it for real

If this is ever used with actual city data rather than demo data, three
changes — no code involved:

1. **Plan → Starter**, and attach a **disk** mounted at `/var/data`
   (~1 GB is plenty for the records; the photos are what grow).
2. Add **`DATA_ROOT=/var/data`** so the store and uploads live on that disk.
3. Drop **`--demo`** from the start command, so sample properties are not mixed
   in with real ones.

`render.yaml` has all three commented at the bottom, ready to uncomment.

Then read the backup section further down — Starter disks are not backed up,
and those JSON files are the entire database.

---

## Run exactly one worker


```
gunicorn app:app --worker-class gthread --workers 1 --threads 8
```

`--workers 1` is a correctness requirement, not a performance setting.

Socket.IO rooms live in the process's memory. With two workers, a browser
connected to worker A never receives an event emitted by worker B — the live
map would freeze for roughly half your users, intermittently, in a way that
looks like a network problem rather than a configuration one.

(The JSON store itself is safe either way: writes take an OS file lock, so
concurrent processes cannot corrupt it. Verified with six processes writing
240 records concurrently — no lost writes, no duplicate ids. The socket rooms
are the reason for the single worker.)

Concurrency comes from `--threads 8` instead, which is ample for a city-scale
deployment. If you later outgrow one process, the fix is a Redis message queue
for Socket.IO plus SQLite or Postgres for the store — at which point the
`services/storage.py` swap the module was designed for becomes worth doing.

### Websockets vs polling

The `gthread` worker does not support the websocket upgrade, so Socket.IO
falls back to HTTP long-polling. **Everything still works** — live map,
counters, notifications — just with more requests.

If you want true websockets, add `gevent` and `gevent-websocket` to
`requirements.txt` and change the worker class:

```
gunicorn app:app -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1
```

That is a real improvement but two more dependencies. Polling is the honest
default for a pilot.

---

## Deploying by hand (instead of the blueprint)

If you would rather not use `render.yaml`: **New → Web Service**, connect the
repo, then:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Region | Singapore (closest to the Philippines) |
| Plan | Free (Starter if you need the data to persist) |
| Build command | `pip install -r requirements.txt` |
| Start command | `python seed.py --demo && gunicorn ...` — see the single-worker section |
| Health check path | `/login` |

On the free plan there is no disk to add. For real use, add one: name
`gcts-data`, mount path `/var/data`, 1 GB.

Add these **environment variables**:

| Key | Value | Why |
|---|---|---|
| `DATA_ROOT` | `/var/data` | Real use only — must match the disk mount path |
| `FLASK_ENV` | `production` | Secure cookies, no debug, no template auto-reload |
| `SECRET_KEY` | *generate a long random value* | See below |
| `PYTHON_VERSION` | `3.13.4` | |
| `SEED_ADMIN_USERNAME` | `city_admin` | |
| `SEED_ADMIN_PASSWORD` | *your choice* | Otherwise generated fresh on each boot |
| `SEED_DEMO_PASSWORD` | *your choice* | Same, for the three demo accounts |

### About `SECRET_KEY`

The app **refuses to start** in production without it. That is deliberate:
without a fixed key, a new one is generated on every restart, which signs out
every user and invalidates every CSRF token — an intermittent bug that is
miserable to diagnose. Failing loudly at boot is better.

Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Changing it later signs everyone out once. That is all.

---

## First boot

`seed.py --demo` runs on every start and is idempotent. On an empty filesystem
it creates everything the app needs to be demonstrable:

- the 31 barangays, with puroks and zone groups
- 31 tricycle units and 8 truck units
- the default weekly waste schedule
- the City Hall Admin, plus one barangay admin and two collectors
- 8 sample properties in Poblacion 1, and the collector assignments

Sign in as `city_admin` and everything is already navigable. Nothing further is
required before a demo.

If you drop `--demo` for real use, only the barangays, vehicles, schedule, and
the single admin are created — you then build the rest through the UI: create
accounts in **User Management**, set the **Waste Schedule**, assign collectors
on the **Tricycle** and **Truck** pages, and have barangay admins add their
properties.

---

## HTTPS matters more than you might expect

Render gives you HTTPS automatically, and the collector app **requires** it.

Browsers refuse the Geolocation API on plain `http://` for anything but
`localhost`. So GPS capture and live tracking silently do nothing if you serve
this over plain HTTP on a LAN — a genuinely confusing failure, because the
button appears to work and simply never returns a position. On Render this is
handled for you. It is worth knowing if you also run it on a local network for
testing.

The same applies to the camera capture on the image-proof upload.

---

## Backups (only relevant for real use)

Render's disks are **not backed up on the Starter plan**. This is a public
records system; losing a month of collection data would be a real problem.

The simplest safeguard is a periodic copy of `/var/data/data/*.json` — those
files are the entire database. Options, cheapest first:

- Render Shell: `tar czf - /var/data/data | base64` and save the output
- A scheduled job that copies the directory to object storage
- Upgrade to a plan with disk snapshots

Whatever you choose, test restoring it once before you rely on it.

---

## What to check after deploying

- [ ] `/login` loads and you can sign in
- [ ] `/public/` shows the map with tiles (zones stay empty until you supply
      the geo data — see `docs/DATA_REQUIREMENTS.md`)
- [ ] The live dot in the top bar goes green, confirming Socket.IO connected
- [ ] Sign in as all four accounts with the passwords you set
- [ ] Deploy once more and sign in again — on the free plan the data resets,
      but the four logins must still work. If they do not,
      `SEED_DEMO_PASSWORD` is not set.
- [ ] On a phone, go On Duty as a collector and confirm the marker appears

That last one is the important one. Your data disappearing after a deploy is
expected on the free plan; being unable to log in afterwards is not.

---

## Things this deployment is not

Being straight about the limits, since this is going somewhere real:

- **One process.** Fine for a city pilot; not built to scale horizontally
  without the Redis + SQL swap described in the single-worker section.
- **JSON files, not a database.** No transactions across collections, and no
  query planner. Reads scan the file. At 31 barangays and a few thousand
  properties that is comfortably fast; at ten times that, move to SQLite.
- **No email.** Password resets go through the City Hall Admin, by design
  (spec §3).
- **No rate limit on login.** The public report form is rate limited; the login
  form is not. If this is exposed to the open internet rather than run for a
  city office, add one.
