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

**On a real deployment, do not use these.** `seed.py` generates a strong
password for the first admin and prints it once; every other account is then
created by the City Hall Admin in User Management, and each user changes their
own password from their Profile page. The script above exists so a capstone
demo has logins you can hand to a panel.

---

## Two things that used to bite

**The test suite no longer changes your passwords.** `tests/phase1_check.py`
runs against the live store — it has to, since what it verifies is the real
login flow — so it puts the four accounts on a test password. It now records
what it found and restores it on exit, including when a check fails early. A
test run and your demo logins no longer interfere.

**A deploy that resets the filesystem needs the passwords pinned.** On a free
host with no persistent disk, `seed.py` runs again on every boot and would
generate *new* passwords each time, locking you out of your own demo. Set
`SEED_ADMIN_PASSWORD` and `SEED_DEMO_PASSWORD` in the host's environment before
the first deploy. See [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Forgot password

The login page's link is deliberately a static message — "Contact the City Hall
Admin to reset your password". There is no email flow in scope. The City Hall
Admin resets any account from **User Management → the lock icon**, and every
signed-in user can change their own from **Profile → Change Password** (current
password required, minimum 8 characters).
