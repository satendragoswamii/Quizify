"""Deployment diagnostic — run this on the server that is failing.

    python diagnose.py                # check everything
    python diagnose.py /admin/users   # also replay one page and show its traceback

It reports the things that differ between a working local checkout and a broken
deployment: the Python and package versions actually in use, where the database was
opened from and whether it can be written, which tables and schema version are present,
and whether every endpoint the templates link to exists in the running code.

Given a path, it then replays that request with authentication bypassed and exception
propagation on, so the real traceback is printed instead of Flask's generic 500 page.
Nothing is modified — the bypass lives only inside this process.
"""

import os
import sys
import traceback

ok_marks = {True: "ok", False: "FAIL"}


def line(label, value, good=None):
    mark = "" if good is None else f"  [{ok_marks[bool(good)]}]"
    print(f"  {label:<26} {value}{mark}")


def section(title):
    print(f"\n{title}\n" + "-" * 74)


def main() -> int:
    problems = []

    section("Environment")
    line("python", sys.version.split()[0])
    line("executable", sys.executable)
    line("working directory", os.getcwd())
    line("this file", os.path.abspath(__file__))

    try:
        import flask  # noqa: F401  — imported to prove it is installed
    except Exception as exc:
        line("flask", f"NOT IMPORTABLE: {exc}", False)
        problems.append("Flask is not installed for this interpreter. On PythonAnywhere, "
                        "check that the Web tab's virtualenv is the one you installed into.")
        return report(problems)

    # Read versions from package metadata: Flask deprecated __version__ and Werkzeug
    # removed it, so touching the attributes would itself raise here.
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    for package in ("flask", "werkzeug", "jinja2", "openpyxl", "python-docx"):
        try:
            line(package, pkg_version(package))
        except PackageNotFoundError:
            line(package, "not installed", False)
            if package in ("flask", "werkzeug", "jinja2"):
                problems.append(f"{package} is missing from this environment.")

    section("Importing the application")
    try:
        from app import app
        line("import app", "succeeded", True)
    except Exception:
        line("import app", "FAILED", False)
        traceback.print_exc(file=sys.stdout)
        problems.append("The app cannot be imported. The traceback above is the cause.")
        return report(problems)

    # ---- database ---------------------------------------------------------------------
    section("Database")
    from quizify.admin import db

    path = db.path()
    directory = os.path.dirname(os.path.abspath(path)) or "."
    exists = os.path.exists(path)
    line("QUIZ_DB_PATH env", os.environ.get("QUIZ_DB_PATH") or "(not set)")
    line("resolved path", path)
    line("file exists", exists, exists)
    if exists:
        line("file size", f"{os.path.getsize(path):,} bytes")
        line("file writable", os.access(path, os.W_OK), os.access(path, os.W_OK))
        if not os.access(path, os.W_OK):
            problems.append(f"The database file is not writable: {path}")
    line("directory writable", os.access(directory, os.W_OK), os.access(directory, os.W_OK))
    if not os.access(directory, os.W_OK):
        problems.append(
            f"The directory holding the database is not writable: {directory}. "
            "SQLite needs to create -wal and -shm files beside the database.")

    try:
        mode = db.query("PRAGMA journal_mode")[0][0]
        line("journal mode", mode)
        if str(mode).lower() != "wal":
            print("      note: WAL is unavailable here (common on networked storage "
                  "such as PythonAnywhere). That is handled, just slower under load.")
    except Exception as exc:
        line("journal mode", f"could not read: {exc}", False)

    try:
        version = db.scalar("SELECT version FROM schema_info WHERE id = 1", default=0)
        line("schema version", f"{version} (code expects {db.SCHEMA_VERSION})",
             int(version) == db.SCHEMA_VERSION)
        if int(version) != db.SCHEMA_VERSION:
            problems.append(
                f"Schema is v{version} but this code expects v{db.SCHEMA_VERSION}. "
                "The migration runs at import; if it has not, the database is probably "
                "not writable.")
    except Exception as exc:
        line("schema version", f"could not read: {exc}", False)
        problems.append("schema_info is unreadable — the database may be empty or corrupt.")

    expected = {"users", "settings", "providers", "jobs", "audit",
                "corrections", "questions", "question_uses", "user_settings"}
    try:
        present = {r["name"] for r in
                   db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(expected - present)
        line("tables present", len(present & expected), not missing)
        if missing:
            line("tables MISSING", ", ".join(missing), False)
            problems.append(f"Missing tables: {', '.join(missing)}. "
                            "The schema could not be created — check writability.")
    except Exception as exc:
        line("tables", f"could not list: {exc}", False)

    try:
        count = db.scalar("SELECT COUNT(*) FROM users", default=0)
        admins = db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' "
                           "AND is_active=1", default=0)
        line("users / active admins", f"{count} / {admins}", count and admins)
        if not count:
            problems.append("There are no users at all. quizify.db is in .gitignore, so a "
                            "git deploy starts empty — visit /admin to create the first "
                            "administrator.")
    except Exception as exc:
        line("users", f"could not count: {exc}", False)

    # ---- routes -----------------------------------------------------------------------
    section("Routes the templates link to")
    # A template referring to an endpoint the deployed code does not define raises
    # BuildError at render time — a 500 on every page that extends that layout. This is
    # the usual symptom of templates and Python files being deployed from different
    # versions, so the check reads the endpoints out of the templates themselves rather
    # than from a list here that could drift out of date.
    import re

    template_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    referenced = {}
    pattern = re.compile(r"""url_for\(\s*['"]([A-Za-z_][A-Za-z0-9_.]*)['"]""")
    for folder, _, files in os.walk(template_root):
        for name in files:
            if not name.endswith(".html"):
                continue
            full = os.path.join(folder, name)
            try:
                with open(full, encoding="utf-8") as handle:
                    for endpoint in pattern.findall(handle.read()):
                        referenced.setdefault(endpoint, set()).add(
                            os.path.relpath(full, template_root))
            except OSError:
                continue

    known = {rule.endpoint for rule in app.url_map.iter_rules()}
    missing = sorted(set(referenced) - known)
    line("templates scanned", sum(1 for _ in referenced) and len(
        {f for files in referenced.values() for f in files}))
    line("endpoints referenced", len(referenced), not missing)
    for endpoint in missing:
        line(f"  {endpoint}", "MISSING — used by " + ", ".join(sorted(referenced[endpoint])),
             False)
    if missing:
        problems.append(
            "Templates link to routes this code does not define: "
            f"{', '.join(missing)}. The templates and the Python files are from "
            "different versions — redeploy the whole project together.")

    # ---- replay a request -------------------------------------------------------------
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target:
        section(f"Replaying GET {target}")
        import quizify.admin as admin_pkg
        from quizify.admin import auth, routes

        fake_admin = {"id": 0, "email": "diagnostic@local", "name": "diagnostic",
                      "role": "admin", "is_active": 1, "daily_quota": 0}

        # Each module imported `current_user` by value, so patching only auth would
        # leave the templates rendering as a signed-out visitor — which skips the very
        # admin-only markup the page is failing on.
        targets = [(auth, "current_user"), (routes, "current_user"),
                   (admin_pkg, "current_user")]
        originals = [(module, name, getattr(module, name, None)) for module, name in targets]
        for module, name in targets:
            if hasattr(module, name):
                setattr(module, name, lambda: fake_admin)

        app.config["PROPAGATE_EXCEPTIONS"] = True
        try:
            response = app.test_client().get(target)
            line("status", response.status_code, response.status_code < 400)
            if response.status_code >= 400:
                problems.append(f"{target} returned {response.status_code}.")
            elif response.status_code == 200:
                line("rendered", f"{len(response.data):,} bytes", True)
        except Exception:
            print("\n  The real traceback behind the 500:\n")
            traceback.print_exc(file=sys.stdout)
            problems.append(f"{target} raised the exception above — that is your cause.")
        finally:
            for module, name, value in originals:
                if value is not None:
                    setattr(module, name, value)
    else:
        print("\nTip: pass a path to replay it, e.g.  python diagnose.py /admin/users")

    return report(problems)


def report(problems) -> int:
    section("Result")
    if not problems:
        print("  No problems found by these checks.")
        print("  If a page still fails, replay it:  python diagnose.py /admin/users")
        return 0
    for i, problem in enumerate(problems, 1):
        print(f"  {i}. {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
