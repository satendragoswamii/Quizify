"""Admin panel tests.

Run with:  python tests/test_admin.py

Everything runs against a throwaway SQLite file, so no existing database is
touched and no network calls are made.
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the admin store at a temp file before the app imports.
_TMP = tempfile.mkdtemp(prefix="quizify-test-")
os.environ["QUIZ_DB_PATH"] = os.path.join(_TMP, "test.db")
for _key in list(os.environ):
    if _key.startswith(("OPENROUTER", "GROQ", "GEMINI", "ANTHROPIC", "QUIZ_API")):
        del os.environ[_key]

import app as flask_app  # noqa: E402
from quizify import config  # noqa: E402
from quizify.admin import db, repo  # noqa: E402

PASSED, FAILED = [], []


def expect(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


flask_app.app.config["TESTING"] = True
flask_app.app.config["WTF_CSRF_ENABLED"] = False


def fresh_client():
    """A client with an empty database — each block of tests starts clean."""
    db.close()
    if os.path.exists(os.environ["QUIZ_DB_PATH"]):
        os.remove(os.environ["QUIZ_DB_PATH"])
    db.init()
    repo.sync_providers()
    repo.apply_settings_to_config()
    return flask_app.app.test_client()


def csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""


def get_csrf(client, path):
    return csrf_from(client.get(path).get_data(as_text=True))


ADMIN = {"email": "admin@example.com", "password": "adminpass123"}


def bootstrap(client):
    """Complete first-run setup and sign in as the administrator."""
    token = get_csrf(client, "/admin/setup")
    client.post("/admin/setup", data={
        "csrf_token": token, "email": ADMIN["email"], "name": "Root",
        "password": ADMIN["password"], "confirm": ADMIN["password"]})
    token = get_csrf(client, "/admin/login")
    return client.post("/admin/login", data={
        "csrf_token": token, "email": ADMIN["email"], "password": ADMIN["password"]})


# =======================================================================================
# First-run setup and authentication
# =======================================================================================

client = fresh_client()

response = client.get("/admin/", follow_redirects=False)
expect("admin root redirects when signed out", response.status_code == 302,
       f"status {response.status_code}")

response = client.get("/admin/login", follow_redirects=False)
expect("login redirects to setup with no users",
       response.status_code == 302 and "/admin/setup" in response.headers.get("Location", ""),
       f"{response.status_code} {response.headers.get('Location')}")

response = client.get("/admin/setup")
expect("setup page renders", response.status_code == 200
       and b"Create your administrator" in response.data)

token = get_csrf(client, "/admin/setup")
response = client.post("/admin/setup", data={
    "csrf_token": token, "email": "admin@example.com",
    "password": "short", "confirm": "short"})
expect("setup rejects a short password",
       response.status_code == 400 and repo.user_count() == 0,
       f"status {response.status_code} users {repo.user_count()}")

token = get_csrf(client, "/admin/setup")
response = client.post("/admin/setup", data={
    "csrf_token": token, "email": "admin@example.com",
    "password": "goodpassword1", "confirm": "different1"})
expect("setup rejects mismatched passwords",
       response.status_code == 400 and repo.user_count() == 0)

token = get_csrf(client, "/admin/setup")
response = client.post("/admin/setup", data={
    "csrf_token": token, "email": "not-an-email",
    "password": "goodpassword1", "confirm": "goodpassword1"})
expect("setup rejects an invalid email", repo.user_count() == 0)

response = client.post("/admin/setup", data={
    "csrf_token": "forged", "email": "a@b.com",
    "password": "goodpassword1", "confirm": "goodpassword1"})
expect("setup rejects a bad CSRF token",
       response.status_code == 400 and repo.user_count() == 0)

# Valid setup
token = get_csrf(client, "/admin/setup")
response = client.post("/admin/setup", data={
    "csrf_token": token, "email": ADMIN["email"], "name": "Root",
    "password": ADMIN["password"], "confirm": ADMIN["password"]},
    follow_redirects=False)
expect("setup creates the first administrator",
       repo.user_count() == 1 and repo.get_user_by_email(ADMIN["email"])["role"] == "admin")

response = client.get("/admin/setup", follow_redirects=False)
expect("setup closes after the first admin exists",
       response.status_code == 302 and "/admin/login" in response.headers.get("Location", ""))

token = get_csrf(client, "/admin/login")
response = client.post("/admin/login", data={
    "csrf_token": token, "email": ADMIN["email"], "password": "wrongpassword"})
expect("login rejects a wrong password", response.status_code == 401)

token = get_csrf(client, "/admin/login")
response = client.post("/admin/login", data={
    "csrf_token": token, "email": "nobody@example.com", "password": "whatever12"})
body = response.get_data(as_text=True)
expect("login does not reveal whether an account exists",
       "Incorrect email or password" in body,
       "message leaks account existence")

token = get_csrf(client, "/admin/login")
response = client.post("/admin/login", data={
    "csrf_token": token, "email": ADMIN["email"], "password": ADMIN["password"]},
    follow_redirects=False)
expect("login succeeds with correct credentials", response.status_code == 302,
       f"status {response.status_code}")

response = client.get("/admin/")
expect("dashboard renders once signed in",
       response.status_code == 200 and b"Conversions" in response.data)


# =======================================================================================
# Account lockout
# =======================================================================================

lock_client = fresh_client()
bootstrap(lock_client)
lock_client.post("/admin/logout", data={"csrf_token": get_csrf(lock_client, "/admin/")})

for _ in range(5):
    token = get_csrf(lock_client, "/admin/login")
    lock_client.post("/admin/login", data={
        "csrf_token": token, "email": ADMIN["email"], "password": "badpassword"})

token = get_csrf(lock_client, "/admin/login")
response = lock_client.post("/admin/login", data={
    "csrf_token": token, "email": ADMIN["email"], "password": ADMIN["password"]})
expect("account locks after repeated failures",
       response.status_code == 401 and b"Too many failed attempts" in response.data,
       f"status {response.status_code}")

repo.unlock_user(repo.get_user_by_email(ADMIN["email"])["id"])
token = get_csrf(lock_client, "/admin/login")
response = lock_client.post("/admin/login", data={
    "csrf_token": token, "email": ADMIN["email"], "password": ADMIN["password"]},
    follow_redirects=False)
expect("unlocking restores access", response.status_code == 302)


# =======================================================================================
# Users
# =======================================================================================

client = fresh_client()
bootstrap(client)

token = get_csrf(client, "/admin/users/new")
response = client.post("/admin/users/new", data={
    "csrf_token": token, "email": "editor@example.com", "name": "Ed",
    "role": "editor", "is_active": "1", "daily_quota": "5",
    "password": "editorpass1", "confirm": "editorpass1"}, follow_redirects=True)
editor = repo.get_user_by_email("editor@example.com")
expect("admin can create a user",
       editor is not None and editor["role"] == "editor" and editor["daily_quota"] == 5,
       str(editor))

token = get_csrf(client, "/admin/users/new")
response = client.post("/admin/users/new", data={
    "csrf_token": token, "email": "editor@example.com", "role": "user",
    "password": "anotherpass1", "confirm": "anotherpass1"})
expect("duplicate emails are rejected", response.status_code == 400)

token = get_csrf(client, f"/admin/users/{editor['id']}")
client.post(f"/admin/users/{editor['id']}", data={
    "csrf_token": token, "action": "save", "email": "editor@example.com",
    "name": "Edited", "role": "user", "daily_quota": "9"}, follow_redirects=True)
editor = repo.get_user_by_email("editor@example.com")
expect("admin can edit a user",
       editor["name"] == "Edited" and editor["role"] == "user" and editor["daily_quota"] == 9,
       str(editor))

# The only admin must not be able to demote or delete themselves.
root = repo.get_user_by_email(ADMIN["email"])
token = get_csrf(client, f"/admin/users/{root['id']}")
client.post(f"/admin/users/{root['id']}", data={
    "csrf_token": token, "action": "save", "email": ADMIN["email"],
    "role": "user", "is_active": "1"}, follow_redirects=True)
expect("the last administrator cannot be demoted",
       repo.get_user_by_email(ADMIN["email"])["role"] == "admin")

token = get_csrf(client, f"/admin/users/{root['id']}")
client.post(f"/admin/users/{root['id']}/delete", data={"csrf_token": token},
            follow_redirects=True)
expect("you cannot delete your own account", repo.get_user(root["id"]) is not None)

token = get_csrf(client, f"/admin/users/{editor['id']}")
client.post(f"/admin/users/{editor['id']}/delete", data={"csrf_token": token},
            follow_redirects=True)
expect("admin can delete another user", repo.get_user(editor["id"]) is None)


# =======================================================================================
# Role enforcement
# =======================================================================================

client = fresh_client()
bootstrap(client)
repo.create_user("plain@example.com", "plainpass123", role="user")
repo.create_user("ed@example.com", "edpass12345", role="editor")

plain = flask_app.app.test_client()
token = get_csrf(plain, "/admin/login")
plain.post("/admin/login", data={"csrf_token": token,
                                 "email": "plain@example.com", "password": "plainpass123"})

expect("a plain user reaches their dashboard", plain.get("/admin/").status_code == 200)
expect("a plain user cannot list users", plain.get("/admin/users").status_code == 403)
expect("a plain user cannot open settings", plain.get("/admin/settings").status_code == 403)
expect("a plain user cannot open providers", plain.get("/admin/providers").status_code == 403)
expect("a plain user cannot open the audit log", plain.get("/admin/audit").status_code == 403)
expect("a plain user cannot open conversions", plain.get("/admin/jobs").status_code == 403)
expect("a plain user can open their profile", plain.get("/admin/profile").status_code == 200)

editor_client = flask_app.app.test_client()
token = get_csrf(editor_client, "/admin/login")
editor_client.post("/admin/login", data={"csrf_token": token,
                                         "email": "ed@example.com", "password": "edpass12345"})
expect("an editor can open conversions", editor_client.get("/admin/jobs").status_code == 200)
expect("an editor cannot manage users", editor_client.get("/admin/users").status_code == 403)

anon = flask_app.app.test_client()
for path in ("/admin/users", "/admin/settings", "/admin/providers", "/admin/audit"):
    expect(f"signed-out access to {path} redirects to login",
           anon.get(path).status_code == 302)

# A user disabled mid-session must lose access immediately.
plain_user = repo.get_user_by_email("plain@example.com")
repo.update_user(plain_user["id"], is_active=False)
expect("disabling a user ends their session",
       plain.get("/admin/").status_code == 302)


# =======================================================================================
# Settings
# =======================================================================================

client = fresh_client()
bootstrap(client)

token = get_csrf(client, "/admin/settings")
form = {"csrf_token": token, "APP_NAME": "My Quiz Tool",
        "QUIZ_DEFAULT_MAX_OPTIONS": "6", "QUIZ_DEFAULT_AI_MODE": "off",
        "QUIZ_DEFAULT_FORMAT": "csv", "QUIZ_AI_TIMEOUT": "45"}
client.post("/admin/settings", data=form, follow_redirects=True)
expect("settings persist to the database",
       repo.get_setting("APP_NAME") == "My Quiz Tool"
       and repo.get_setting("QUIZ_DEFAULT_MAX_OPTIONS") == "6")
expect("saved settings reach the runtime config",
       config.get("APP_NAME") == "My Quiz Tool" and config.get_int("QUIZ_AI_TIMEOUT", 0) == 45,
       f"{config.get('APP_NAME')} / {config.get('QUIZ_AI_TIMEOUT')}")

response = client.get("/")
expect("converter uses the saved default option count",
       b'id="max_options"' in response.data and b'value="6"' in response.data)

# Booleans come from checkbox presence, not a submitted value.
token = get_csrf(client, "/admin/settings")
client.post("/admin/settings", data={"csrf_token": token, "QUIZ_REQUIRE_LOGIN": "1"},
            follow_redirects=True)
expect("a boolean setting turns on", config.get_bool("QUIZ_REQUIRE_LOGIN", False))

token = get_csrf(client, "/admin/settings")
client.post("/admin/settings", data={"csrf_token": token}, follow_redirects=True)
expect("an unchecked boolean turns off", not config.get_bool("QUIZ_REQUIRE_LOGIN", True))


# =======================================================================================
# Login gate on the converter
# =======================================================================================

client = fresh_client()
bootstrap(client)
repo.set_setting("QUIZ_REQUIRE_LOGIN", "true")
repo.apply_settings_to_config()

visitor = flask_app.app.test_client()
expect("gated converter redirects anonymous visitors",
       visitor.get("/").status_code == 302)
response = visitor.post("/", data={"subject": "S", "topic": "T", "quiz_text": "Q1. x?\nA. a\nB. b"})
expect("gated converter refuses anonymous submissions", response.status_code == 302)
expect("gated converter still serves signed-in users",
       client.get("/").status_code == 200)

response = visitor.post("/api/parse", data={"quiz_text": "Q1. x?\nA. a\nB. b"})
expect("gated API returns 401 rather than a redirect", response.status_code == 401)

repo.set_setting("QUIZ_REQUIRE_LOGIN", "false")
repo.apply_settings_to_config()
# Login is now mandatory app-wide: even with QUIZ_REQUIRE_LOGIN off, an anonymous
# visitor never sees the converter — the whole app is behind sign-in.
expect("converter always requires login for anonymous visitors",
       flask_app.app.test_client().get("/").status_code == 302)
expect("signed-in users still reach the converter",
       client.get("/").status_code == 200)


# =======================================================================================
# Job recording and quotas
# =======================================================================================

client = fresh_client()
bootstrap(client)

QUIZ = "Q1. Capital of Peru?\nA. Lima\nB. Quito\nAnswer: A"
before = repo.job_count()
response = client.post("/", data={"subject": "Geo", "topic": "SA", "format": "csv",
                                  "max_options": "4", "ai_mode": "off", "quiz_text": QUIZ})
expect("a successful conversion downloads", response.status_code == 200)
expect("a successful conversion is recorded", repo.job_count() == before + 1)

job = repo.list_jobs(limit=1)[0]
expect("the job records question counts and the user",
       job["questions"] == 1 and job["answered"] == 1 and job["status"] == "ok"
       and job["user_email"] == ADMIN["email"],
       str(job))

client.post("/", data={"subject": "S", "topic": "T", "ai_mode": "off",
                       "quiz_text": "just some prose with no questions at all"})
job = repo.list_jobs(limit=1)[0]
expect("a run that finds nothing is recorded as empty", job["status"] == "empty",
       job["status"])

# Quota
root = repo.get_user_by_email(ADMIN["email"])
repo.update_user(root["id"], daily_quota=1)
jobs_before = repo.job_count()
response = client.post("/", data={"subject": "S", "topic": "T", "ai_mode": "off",
                                  "quiz_text": QUIZ})
expect("a user over quota is blocked",
       response.status_code == 429 and b"daily limit" in response.data,
       f"status {response.status_code}")
expect("a quota rejection is not recorded as a conversion",
       repo.job_count() == jobs_before,
       f"{repo.job_count()} != {jobs_before}")
repo.update_user(root["id"], daily_quota=0)

expect("conversions export as CSV",
       client.get("/admin/jobs/export").status_code == 200)

removed = repo.purge_jobs(0)
expect("purge with 0 days is a no-op", removed == 0)


# =======================================================================================
# Providers
# =======================================================================================

client = fresh_client()
bootstrap(client)

token = get_csrf(client, "/admin/providers")
client.post("/admin/providers", data={
    "csrf_token": token, "name": "openrouter", "enabled": "1",
    "api_key": "sk-or-test-key-value", "model": "some/model:free",
    "base_url": "", "priority": "1"}, follow_redirects=True)

expect("a provider key saved in the panel reaches the runtime config",
       config.get("OPENROUTER_API_KEY") == "sk-or-test-key-value",
       str(config.get("OPENROUTER_API_KEY")))
expect("a provider model saved in the panel reaches the runtime config",
       config.get("QUIZ_MODEL_OPENROUTER") == "some/model:free")

page = client.get("/admin/providers").get_data(as_text=True)
expect("the providers page never renders a stored key",
       "sk-or-test-key-value" not in page)
expect("the providers page shows a masked hint", "sk-o••••alue" in page,
       "mask not rendered")

health = client.get("/api/health").get_json()
expect("a panel-configured provider becomes active",
       "openrouter" in health["ai_chain"], str(health["ai_chain"]))

# Saving with a blank key must not wipe the stored one.
token = get_csrf(client, "/admin/providers")
client.post("/admin/providers", data={
    "csrf_token": token, "name": "openrouter", "enabled": "1",
    "api_key": "", "model": "some/model:free", "priority": "1"},
    follow_redirects=True)
expect("a blank key field keeps the stored key",
       config.get("OPENROUTER_API_KEY") == "sk-or-test-key-value")

# Disabling must take the provider out of the chain.
token = get_csrf(client, "/admin/providers")
client.post("/admin/providers", data={
    "csrf_token": token, "name": "openrouter", "api_key": "",
    "model": "", "priority": "1"}, follow_redirects=True)
health = client.get("/api/health").get_json()
expect("disabling a provider removes it from the chain",
       "openrouter" not in health["ai_chain"], str(health["ai_chain"]))

# Clearing the key
token = get_csrf(client, "/admin/providers")
client.post("/admin/providers", data={
    "csrf_token": token, "name": "openrouter", "enabled": "1",
    "action": "clear_key"}, follow_redirects=True)
expect("clearing removes the stored key", not config.get("OPENROUTER_API_KEY"))

response = client.post("/admin/providers", data={
    "csrf_token": get_csrf(client, "/admin/providers"), "name": "not-a-provider"})
expect("an unknown provider name 404s", response.status_code == 404)


# =======================================================================================
# Audit log
# =======================================================================================

client = fresh_client()
bootstrap(client)
token = get_csrf(client, "/admin/settings")
client.post("/admin/settings", data={"csrf_token": token, "APP_NAME": "Audited"},
            follow_redirects=True)

entries = repo.list_audit(limit=50)
actions = {e["action"] for e in entries}
expect("sign-ins are audited", "login.success" in actions, str(actions))
expect("setting changes are audited", "settings.updated" in actions, str(actions))
expect("audit entries record the actor",
       any(e["actor_email"] == ADMIN["email"] for e in entries))

lock_client = flask_app.app.test_client()
token = get_csrf(lock_client, "/admin/login")
lock_client.post("/admin/login", data={"csrf_token": token,
                                       "email": ADMIN["email"], "password": "nope"})
expect("failed sign-ins are audited",
       "login.failed" in {e["action"] for e in repo.list_audit(limit=50)})

expect("the audit page renders", client.get("/admin/audit").status_code == 200)


# =======================================================================================
# CSRF on every state-changing route
# =======================================================================================

client = fresh_client()
bootstrap(client)
user_id = repo.create_user("victim@example.com", "victimpass1", role="user")

for path, data in [
    ("/admin/settings", {"APP_NAME": "hacked"}),
    ("/admin/providers", {"name": "openrouter", "enabled": "1"}),
    ("/admin/users/new", {"email": "x@y.com", "password": "abcdefgh1",
                          "confirm": "abcdefgh1"}),
    (f"/admin/users/{user_id}/delete", {}),
    ("/admin/jobs/purge", {"days": "1"}),
    ("/admin/logout", {}),
]:
    response = client.post(path, data={**data, "csrf_token": "forged"})
    expect(f"CSRF is enforced on {path}", response.status_code == 400,
           f"status {response.status_code}")

expect("no CSRF-protected route caused a side effect",
       repo.get_setting("APP_NAME") != "hacked"
       and repo.get_user(user_id) is not None
       and repo.get_user_by_email("x@y.com") is None)


# =======================================================================================
# Secrets never leak
# =======================================================================================

client = fresh_client()
bootstrap(client)
repo.set_setting("_secret_key", "super-secret-signing-key")
token = get_csrf(client, "/admin/providers")
client.post("/admin/providers", data={
    "csrf_token": token, "name": "groq", "enabled": "1",
    "api_key": "gsk-super-secret-value", "model": "", "priority": "1"},
    follow_redirects=True)

for path in ("/admin/settings", "/admin/providers", "/admin/", "/api/health",
             "/api/providers"):
    body = client.get(path).get_data(as_text=True)
    expect(f"no secrets leak on {path}",
           "gsk-super-secret-value" not in body
           and "super-secret-signing-key" not in body)

expect("the settings page does not expose the signing key",
       "_secret_key" not in client.get("/admin/settings").get_data(as_text=True))


# =======================================================================================
# Report
# =======================================================================================

db.close()
print(f"\n{'=' * 72}")
print(f"PASSED: {len(PASSED)}    FAILED: {len(FAILED)}")
print("=" * 72)
for entry in PASSED:
    print(f"  ok    {entry}")
if FAILED:
    print()
    for entry in FAILED:
        print(f"  FAIL  {entry}")
sys.exit(1 if FAILED else 0)
