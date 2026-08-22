"""Phase 1 verification: auth, RBAC, CSRF, sessions, change password."""
import atexit
import re, sys
from pathlib import Path
_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _ROOT)

import app as _app
from services import auth_service, storage
from werkzeug.security import generate_password_hash

flask_app = _app.app
flask_app.config["WTF_CSRF_ENABLED"] = False
results = []


def ok(label, cond):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL':<4} {label}")
    return cond


def token(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def login(username, password, remember=False):
    c = flask_app.test_client()
    data = {"username": username, "password": password, "csrf_token": token(c)}
    if remember:
        data["remember"] = "1"
    r = c.post("/login", data=data, follow_redirects=False)
    return c, r


# Reset known passwords for the test run.
#
# This suite is the one that runs against the LIVE store -- it has to, because
# what it checks is the real login flow. So it puts the four accounts on known
# passwords, and then puts them back: a test run that silently left the demo
# logins changed would lock you out of your own system, which is exactly what
# it used to do.
CREDS = {"city_admin": "TestPass!2026", "brgy_admin": "TestPass!2026",
         "tri_collector": "TestPass!2026", "truck_collector": "TestPass!2026"}
ORIGINAL = {}
for username, password in CREDS.items():
    user = auth_service.by_username(username)
    ORIGINAL[username] = {
        "id": user["id"],
        "password_hash": user.get("password_hash"),
        "status": user.get("status"),
        "must_change_password": user.get("must_change_password"),
    }
    storage.update("users", user["id"], {
        "password_hash": generate_password_hash(password),
        "status": "Active", "must_change_password": False})


def restore_passwords():
    """Put every account back exactly as it was found."""
    for saved in ORIGINAL.values():
        storage.update("users", saved["id"], {
            "password_hash": saved["password_hash"],
            "status": saved["status"],
            "must_change_password": saved["must_change_password"],
        })


# Registered now, so an assertion that stops the run early still restores.
atexit.register(restore_passwords)

print("\n[1] login")
c, r = login("city_admin", CREDS["city_admin"])
ok("valid credentials redirect to the role's home", r.status_code == 302 and "/city/" in r.headers["Location"])
_, r = login("city_admin", "wrong-password")
body = r.get_data(as_text=True)
ok("wrong password is rejected", r.status_code == 200 and "Incorrect username or password" in body)
_, r = login("no-such-user", "whatever")
ok("unknown user gets the identical message (no enumeration)",
   "Incorrect username or password" in r.get_data(as_text=True))
_, r = login("CITY_ADMIN", CREDS["city_admin"])
ok("username is case-insensitive", r.status_code == 302)

u = auth_service.by_username("brgy_admin")
storage.update("users", u["id"], {"status": "Inactive"})
_, r = login("brgy_admin", CREDS["brgy_admin"])
ok("inactive account cannot log in", "inactive" in r.get_data(as_text=True).lower())
storage.update("users", u["id"], {"status": "Active"})

print("\n[2] role-based access control")
clients = {}
for name in CREDS:
    clients[name], _ = login(name, CREDS[name])

matrix = [("city_admin", "/city/", 200), ("brgy_admin", "/city/", 403),
          ("tri_collector", "/city/", 403), ("truck_collector", "/city/", 403),
          ("brgy_admin", "/barangay/", 200), ("city_admin", "/barangay/", 403),
          ("tri_collector", "/collector/tricycle/", 200),
          ("truck_collector", "/collector/tricycle/", 403),
          ("tri_collector", "/collector/truck/", 403),
          ("truck_collector", "/collector/truck/", 200)]
bad = [(who, url, exp, clients[who].get(url).status_code) for who, url, exp in matrix
       if clients[who].get(url).status_code != exp]
ok(f"access matrix holds ({len(matrix)} combinations)", not bad) or print("      ", bad)

anon = flask_app.test_client()
ok("anonymous is redirected to login, not 403", anon.get("/city/").status_code == 302)
ok("public viewer needs no login", anon.get("/public/").status_code == 200)
ok("geo API needs no login", anon.get("/api/geo/barangays").status_code == 200)

print("\n[3] CSRF")
c = clients["city_admin"]
r = c.post("/city/users", data={"full_name": "X", "username": "x", "role": "Barangay Admin",
                                "password": "abcdefgh", "confirm_password": "abcdefgh"})
ok("POST without a token is rejected", r.status_code == 400)
r = c.post("/city/users", data={"csrf_token": "forged-token", "full_name": "X"})
ok("POST with a forged token is rejected", r.status_code == 400)
r = c.post("/city/users", data={"csrf_token": token(c, "/city/users"), "full_name": "X",
                                "username": "x", "role": "Barangay Admin",
                                "password": "abcdefgh", "confirm_password": "abcdefgh"})
ok("POST with the session's token succeeds", r.status_code == 302)
ok("every POST form renders a token",
   all("csrf_token" in clients[who].get(url).get_data(as_text=True)
       for who, url in [("city_admin", "/city/users"), ("city_admin", "/city/tricycle"),
                        ("brgy_admin", "/barangay/properties"),
                        ("tri_collector", "/collector/tricycle/profile")]))

print("\n[4] session behaviour")
c, r = login("city_admin", CREDS["city_admin"])
with c.session_transaction() as s:
    ok("session stores the user id, never the password", "user_id" in s and not any(
        "password" in k for k in s))
    permanent_off = not s.permanent
ok("without remember-me the cookie is browser-session", permanent_off)
c, _ = login("city_admin", CREDS["city_admin"], remember=True)
with c.session_transaction() as s:
    ok("with remember-me the session is permanent", s.permanent)

c, _ = login("city_admin", CREDS["city_admin"])
c.get("/logout")
ok("logout clears the session", c.get("/city/").status_code == 302)

c, _ = login("city_admin", CREDS["city_admin"])
from services import timeutil
from datetime import timedelta
with c.session_transaction() as s:
    s["last_seen"] = (timeutil.now() - timedelta(minutes=999)).isoformat(timespec="seconds")
r = c.get("/city/")
ok("idle admin session times out", r.status_code == 302 and "login" in r.headers["Location"])

c, _ = login("tri_collector", CREDS["tri_collector"])
with c.session_transaction() as s:
    s["last_seen"] = (timeutil.now() - timedelta(minutes=999)).isoformat(timespec="seconds")
ok("collector session does NOT idle out (field use)",
   c.get("/collector/tricycle/").status_code == 200)

c, _ = login("brgy_admin", CREDS["brgy_admin"])
u = auth_service.by_username("brgy_admin")
storage.update("users", u["id"], {"status": "Inactive"})
ok("deactivating a user kills their live session on the next request",
   c.get("/barangay/").status_code == 302)
storage.update("users", u["id"], {"status": "Active"})

print("\n[5] open redirect")
c = flask_app.test_client()
for target, allowed in [("/city/users", True), ("//evil.example", False),
                        ("https://evil.example", False), ("/\\evil", False)]:
    r = c.post(f"/login?next={target}", data={"username": "city_admin",
               "password": CREDS["city_admin"], "csrf_token": token(c)})
    landed = r.headers.get("Location", "")
    good = (landed == target) if allowed else ("evil" not in landed)
    ok(f"next={target!r} {'honoured' if allowed else 'ignored'}", good)
    c = flask_app.test_client()

print("\n[6] change password")
c, _ = login("tri_collector", CREDS["tri_collector"])
url = "/change-password?next=/collector/tricycle/profile"
def change(client, current, new, confirm):
    return client.post(url, data={"csrf_token": token(client, "/collector/tricycle/profile"),
                                  "current_password": current, "new_password": new,
                                  "confirm_password": confirm}, follow_redirects=True)

body = change(c, "wrong-current", "NewPass!2026", "NewPass!2026").get_data(as_text=True)
ok("wrong current password is refused", "current password is not correct" in body)
body = change(c, CREDS["tri_collector"], "short", "short").get_data(as_text=True)
ok("under 8 characters is refused", "at least 8 characters" in body)
body = change(c, CREDS["tri_collector"], "NewPass!2026", "Different!2026").get_data(as_text=True)
ok("mismatched confirmation is refused", "do not match" in body)
body = change(c, CREDS["tri_collector"], CREDS["tri_collector"], CREDS["tri_collector"]).get_data(as_text=True)
ok("reusing the same password is refused", "must be different" in body)
body = change(c, CREDS["tri_collector"], "NewPass!2026", "NewPass!2026").get_data(as_text=True)
ok("valid change succeeds", "password has been updated" in body)
_, r = login("tri_collector", "NewPass!2026")
ok("the new password works", r.status_code == 302)
_, r = login("tri_collector", CREDS["tri_collector"])
ok("the old password no longer works", r.status_code == 200)

print("\n[7] no plaintext passwords anywhere in the store")
users = storage.read("users")
ok("every account has a hash and no plaintext field",
   all(u.get("password_hash", "").startswith(("pbkdf2:", "scrypt:")) and
       not any("password" == k or k == "plaintext" for k in u) for u in users))
ok("the hash never reaches a template",
   all("password_hash" not in auth_service.public_view(u) for u in users))
c, _ = login("city_admin", CREDS["city_admin"])
ok("no hash appears in rendered HTML",
   all("pbkdf2:" not in c.get(u).get_data(as_text=True)
       for u in ["/city/", "/city/users", "/city/profile"]))

print("\n[8] every page still renders")
pages = {"city_admin": ["/city/", "/city/users", "/city/schedule", "/city/tricycle",
                        "/city/truck", "/city/tracking", "/city/property", "/city/mrf",
                        "/city/carry-over", "/city/reports", "/city/profile"],
         "brgy_admin": ["/barangay/", "/barangay/properties", "/barangay/schedule",
                        "/barangay/tracking", "/barangay/collections",
                        "/barangay/history", "/barangay/profile"],
         "tri_collector": ["/collector/tricycle/", "/collector/tricycle/unavailable",
                           "/collector/tricycle/history", "/collector/tricycle/profile"],
         "truck_collector": ["/collector/truck/", "/collector/truck/unavailable",
                             "/collector/truck/history", "/collector/truck/profile"]}
CREDS["tri_collector"] = "NewPass!2026"
broken = []
for who, urls in pages.items():
    cl, _ = login(who, CREDS[who])
    for u in urls:
        if cl.get(u).status_code != 200:
            broken.append((who, u, cl.get(u).status_code))
n = sum(len(v) for v in pages.values())
ok(f"all {n} authenticated pages return 200", not broken) or print("      ", broken)
ok("public + login pages render",
   all(flask_app.test_client().get(u).status_code == 200 for u in ["/login", "/public/"]))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
