# Logins for the demo

Every account below uses the same password:

```
Cabadbaran2026
```

| Username | Role | Scope | Lands on |
|---|---|---|---|
| `city_admin` | City Hall Admin | Everything | `/city/` |
| `brgy_admin` | Barangay Admin | Poblacion 1 | `/barangay/` |
| `tri_collector` | Tricycle Collector | Poblacion 1, Puroks 1–3, TRI-16 | `/collector/tricycle/` |
| `truck_collector` | Truck Collector | 4 barangay MRFs, TRK-01 | `/collector/truck/` |

One `/login` for all four — the role decides where you land. The public viewer
at `/public/` needs no account at all.

Show them on a phone-sized window: the two collector portals and the public
pages are built mobile-first, and that is how they are meant to be seen.

---

## Offline and local runs

The same four logins work with no network and no deploy. `data/` is gitignored
— it holds password hashes — so a fresh clone starts with no accounts at all,
and the login page rejects every password because there is nothing to match
against. `python app.py` handles that itself: when the user collection is
empty it runs the seed first, then serves the app. Nothing to remember, and it
is the same `seed_password()` the deploy uses, so the credentials are the same
in both places.

It only fires on an empty store, so it can never overwrite records you have
entered. To seed by hand instead, or to see the full output when something
goes wrong:

```bash
python seed.py --demo
```

The same startup pass also writes the illustrative barangay geometry when
`data/geo/` is still placeholders, because a map with no polygons cannot zoom
to a barangay — the filter works by fitting a polygon's bounds. Real GeoJSON
already in `data/geo/` is never touched.

`AUTO_SEED=0` switches the automatic path off; `AUTO_SEED=1` forces it on in
production, where it is off by default because the host seeds in its start
command.

---

## The rest of the cast

`tools/make_demo_day.py` creates a working set of accounts as well, all on the
same password:

| Username | Role |
|---|---|
| `brgy01_admin` … `brgy10_admin` | Barangay Admin, one per barangay |
| `tri01` … `tri10` | Tricycle Collector, one per barangay |
| `trk01` … `trk04` | Truck Collector, covering the barangay MRFs |

Poblacion 1 keeps `brgy_admin` and `tri_collector` rather than getting a
generated pair, so the four accounts above are the ones with a full day of
activity behind them.

---

## Setting or changing the password

```bash
python tools/set_demo_passwords.py                  # sets Cabadbaran2026
python tools/set_demo_passwords.py YourOwnPassword  # or choose your own
```

Passwords are stored only as `werkzeug` hashes — never in plaintext, not even
inside the JSON files. This script writes a new hash; it cannot read an
existing one back.

Every seeding path — `seed.py`, `tools/make_demo_day.py` and the script above —
reads the password from `seed_password()` in `seed.py`, so they cannot disagree
about what it is. That is what makes the local and deployed credentials
identical.

**If the deployment URL is public, override it.** The default is readable by
anyone who can read this repo. Set `SEED_ADMIN_PASSWORD` (covers `city_admin`)
and `SEED_DEMO_PASSWORD` (covers every other seeded account) in the host's
environment; both take priority over the default and are never echoed to the
build log. For real use, accounts are created by the City Hall Admin in User
Management and each user sets their own password from Profile.

---

## Two things that used to bite

**The test suite no longer changes your passwords.** `tests/phase1_check.py`
runs against the live store — it has to, since what it verifies is the real
login flow — so it puts the four accounts on a test password. It now records
what it found and restores it on exit, including when a check fails early. A
test run and your demo logins no longer interfere.

**A deploy that resets the filesystem no longer changes the logins.** `seed.py`
used to generate a fresh password on every boot, which locked you out of your
own demo on a host with no persistent disk — and worse, `make_demo_day.py`
fell back to a *different*, hardcoded one, so a single deploy ended up with two
different passwords across its accounts. Both now read the one definition, so
the credentials survive every restart and match your local store. Pinning the
env vars is optional. See [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Forgot password

The login page's link is deliberately a static message — "Contact the City Hall
Admin to reset your password". There is no email flow in scope. The City Hall
Admin resets any account from **User Management → the lock icon**, and every
signed-in user can change their own from **Profile → Change Password** (current
password required, minimum 8 characters).
