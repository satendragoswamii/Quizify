"""Admin blueprint: dashboard, users, providers, settings, jobs, question bank,
corrections, and audit log."""

import csv
import io
import json
import re
from typing import Any, Dict, Optional

from flask import (
    Blueprint, Response, abort, jsonify, redirect, render_template, request, url_for,
)

from .. import bank, exams, features, learning
from .. import providers as prov
from ..export import export as export_questions
from . import repo
from .auth import (
    LoginError, admin_required, check_csrf, client_ip, csrf_token, current_user,
    flash_error, flash_ok, has_role, login, login_required, logout, role_required,
    validate_password,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.app_context_processor
def inject_admin_context() -> Dict[str, Any]:
    """Make the signed-in user and CSRF token available to every template."""
    return {
        "current_user": current_user(),
        "csrf_token": csrf_token,
        "has_role": has_role,
        "app_name": repo.get_setting("APP_NAME", "Quizify"),
    }


def _form_bool(name: str) -> bool:
    return request.form.get(name, "").strip().lower() in ("1", "true", "on", "yes")


def _form_int(name: str, default: int = 0) -> int:
    try:
        return int(request.form.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------------------
# First-run setup
# --------------------------------------------------------------------------------------

@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first administrator. Unavailable once any user exists."""
    if repo.user_count() > 0:
        return redirect(url_for("admin.login"))

    if request.method == "POST":
        check_csrf()
        email = (request.form.get("email") or "").strip()
        name = (request.form.get("name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        try:
            if "@" not in email:
                raise ValueError("Enter a valid email address.")
            validate_password(password, confirm)
        except ValueError as exc:
            flash_error(str(exc))
            return render_template("admin/setup.html", email=email, name=name), 400

        # Guard against two setup requests racing to create the first admin.
        if repo.user_count() > 0:
            return redirect(url_for("admin.login"))

        repo.create_user(email, password, name=name, role="admin")
        repo.sync_providers()
        repo.apply_settings_to_config()
        repo.log("setup.admin_created", target=email, ip=client_ip())
        flash_ok("Administrator created. Please sign in.")
        return redirect(url_for("admin.login"))

    return render_template("admin/setup.html", email="", name="")


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"], endpoint="login")
def login_view():
    if repo.user_count() == 0:
        return redirect(url_for("admin.setup"))
    if current_user() is not None:
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        check_csrf()
        try:
            login(request.form.get("email", ""), request.form.get("password", ""))
        except LoginError as exc:
            flash_error(str(exc))
            return render_template("admin/login.html",
                                   email=request.form.get("email", "")), 401
        target = request.args.get("next") or url_for("admin.dashboard")
        # Only ever redirect within this site.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("admin.dashboard")
        return redirect(target)

    return render_template("admin/login.html", email="")



@bp.route("/logout", methods=["POST"])
@login_required
def logout_view():
    check_csrf()
    logout()

    # Signing out from the converter should land back on the converter, not on the admin
    # login. Only same-site relative paths are honoured, so this cannot be turned into an
    # open redirect.
    target = request.form.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        # The converter shows no flash messages, so a note left here would sit unread in
        # the session and surface later on an unrelated admin page.
        return redirect(target)

    flash_ok("Signed out.")
    return redirect(url_for("admin.login"))


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------

@bp.route("/")
@login_required
def dashboard():
    # Anyone signed in reaches this page, so a plain user sees only their own figures.
    # Without this a new account was shown the install's totals — and, through recent
    # conversions, the file names everybody else had uploaded.
    #
    # The cut is at "editor" to match the Conversions page, which that role can already
    # open in full; scoping the dashboard tighter than the page it links to would only
    # be confusing.
    user = current_user()
    everything = has_role("editor", user)
    scope_id = None if everything else (user["id"] if user else -1)

    stats = repo.dashboard_stats(user_id=scope_id)
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        recent_jobs=repo.list_jobs(limit=8, user_id=scope_id),
        providers=repo.provider_rows() if has_role("editor", user) else [],
        max_daily=max([d["count"] for d in stats["daily"]] or [1]),
        showing_everyone=everything,
    )


# --------------------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------------------

@bp.route("/users")
@admin_required
def users():
    return render_template(
        "admin/users.html",
        users=repo.list_users(search=request.args.get("q", "").strip(),
                              role=request.args.get("role", "").strip()),
        search=request.args.get("q", ""),
        role_filter=request.args.get("role", ""),
        roles=repo.ROLES,
    )


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    if request.method == "POST":
        check_csrf()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        try:
            if "@" not in email:
                raise ValueError("Enter a valid email address.")
            if repo.get_user_by_email(email):
                raise ValueError("A user with that email already exists.")
            validate_password(password, request.form.get("confirm"))
        except ValueError as exc:
            flash_error(str(exc))
            return render_template("admin/user_form.html", user=request.form,
                                   roles=repo.ROLES, is_new=True), 400

        user_id = repo.create_user(
            email, password,
            name=(request.form.get("name") or "").strip(),
            role=request.form.get("role", "user"),
            is_active=_form_bool("is_active"),
            daily_quota=_form_int("daily_quota"),
            notes=(request.form.get("notes") or "").strip(),
        )
        repo.log("user.created", actor=current_user(), target=email, ip=client_ip())
        flash_ok(f"Created {email}.")
        return redirect(url_for("admin.user_edit", user_id=user_id))

    return render_template("admin/user_form.html",
                           user={"is_active": 1, "role": "user", "daily_quota": 0},
                           roles=repo.ROLES, is_new=True)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@admin_required
def user_edit(user_id: int):
    user = repo.get_user(user_id)
    if user is None:
        abort(404)
    actor = current_user()

    if request.method == "POST":
        check_csrf()
        action = request.form.get("action", "save")

        if action == "save":
            email = (request.form.get("email") or "").strip()
            role = request.form.get("role", "user")
            is_active = _form_bool("is_active")

            existing = repo.get_user_by_email(email)
            if "@" not in email:
                flash_error("Enter a valid email address.")
                return redirect(url_for("admin.user_edit", user_id=user_id))
            if existing and existing["id"] != user_id:
                flash_error("Another user already uses that email.")
                return redirect(url_for("admin.user_edit", user_id=user_id))
            # Never allow the last active admin to be demoted or disabled.
            if user["role"] == "admin" and (role != "admin" or not is_active):
                if repo.admin_count(exclude_id=user_id) == 0:
                    flash_error("This is the only active administrator. "
                                "Promote another account first.")
                    return redirect(url_for("admin.user_edit", user_id=user_id))

            repo.update_user(
                user_id, email=email, name=(request.form.get("name") or "").strip(),
                role=role, is_active=is_active, daily_quota=_form_int("daily_quota"),
                notes=(request.form.get("notes") or "").strip(),
            )
            repo.log("user.updated", actor=actor, target=email,
                     detail={"role": role, "active": is_active}, ip=client_ip())
            flash_ok("Changes saved.")

        elif action == "password":
            password = request.form.get("password") or ""
            try:
                validate_password(password, request.form.get("confirm"))
            except ValueError as exc:
                flash_error(str(exc))
                return redirect(url_for("admin.user_edit", user_id=user_id))
            repo.set_password(user_id, password)
            repo.log("user.password_reset", actor=actor, target=user["email"],
                     ip=client_ip())
            flash_ok("Password updated.")

        elif action == "unlock":
            repo.unlock_user(user_id)
            repo.log("user.unlocked", actor=actor, target=user["email"], ip=client_ip())
            flash_ok("Account unlocked.")

        elif action == "features":
            kept = features.save_overrides(user_id, request.form.to_dict())
            repo.log("user.features_updated", actor=actor, target=user["email"],
                     detail={"overrides": kept}, ip=client_ip())
            flash_ok("Features saved." if kept else
                     "Features saved — this account now follows every global setting.")

        elif action == "features_reset":
            removed = features.clear_overrides(user_id)
            repo.log("user.features_reset", actor=actor, target=user["email"],
                     detail={"removed": removed}, ip=client_ip())
            flash_ok(f"Cleared {removed} override(s); back to the global settings.")

        return redirect(url_for("admin.user_edit", user_id=user_id))

    return render_template("admin/user_form.html", user=user, roles=repo.ROLES,
                           is_new=False, usage_today=repo.usage_today(user_id),
                           recent_jobs=repo.list_jobs(limit=10, user_id=user_id),
                           is_locked=repo.is_locked(user),
                           feature_groups=features.groups(),
                           overrides=features.overrides_for(user_id),
                           effective=features.for_user(user),
                           global_value=features.global_value)


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id: int):
    check_csrf()
    user = repo.get_user(user_id)
    if user is None:
        abort(404)
    actor = current_user()
    if actor and actor["id"] == user_id:
        flash_error("You cannot delete the account you are signed in with.")
        return redirect(url_for("admin.user_edit", user_id=user_id))
    if user["role"] == "admin" and repo.admin_count(exclude_id=user_id) == 0:
        flash_error("This is the only active administrator and cannot be deleted.")
        return redirect(url_for("admin.user_edit", user_id=user_id))

    repo.delete_user(user_id)
    repo.log("user.deleted", actor=actor, target=user["email"], ip=client_ip())
    flash_ok(f"Deleted {user['email']}.")
    return redirect(url_for("admin.users"))


# --------------------------------------------------------------------------------------
# AI providers
# --------------------------------------------------------------------------------------

@bp.route("/providers", methods=["GET", "POST"])
@admin_required
def providers_view():
    if request.method == "POST":
        check_csrf()
        name = request.form.get("name", "")
        if name not in prov.BY_NAME:
            abort(404)
        action = request.form.get("action", "save")

        if action == "clear_key":
            repo.clear_provider_key(name)
            repo.apply_settings_to_config()
            repo.log("provider.key_cleared", actor=current_user(), target=name,
                     ip=client_ip())
            flash_ok(f"Cleared the stored key for {name}.")
            return redirect(url_for("admin.providers_view"))

        # An empty key field means "leave the stored key alone".
        submitted_key: Optional[str] = request.form.get("api_key")
        if submitted_key is not None and submitted_key.strip() == "":
            submitted_key = None

        repo.save_provider(
            name,
            enabled=_form_bool("enabled"),
            api_key=submitted_key,
            model=request.form.get("model", ""),
            base_url=request.form.get("base_url", ""),
            priority=_form_int("priority", 100),
        )
        repo.apply_settings_to_config()
        repo.log("provider.updated", actor=current_user(), target=name,
                 detail={"enabled": _form_bool("enabled"),
                         "key_changed": submitted_key is not None},
                 ip=client_ip())
        flash_ok(f"Saved {name}.")
        return redirect(url_for("admin.providers_view"))

    from .. import messengerx as mx

    rows = repo.provider_rows()
    # The order requests actually fall through is the thing an operator most needs to
    # see, and it is not obvious from the cards — it comes from live availability, not
    # from the stored priority alone.
    chain = [p.name for p in prov.available()]
    by_name = {row["name"]: row for row in rows}
    return render_template(
        "admin/providers.html",
        providers=rows,
        chain=[by_name[name] for name in chain if name in by_name],
        configured=[r for r in rows if r["active"] or r["has_key"]],
        unconfigured=[r for r in rows if not (r["active"] or r["has_key"])],
        messengerx=mx.describe(),
    )


@bp.route("/providers/messengerx/companion", methods=["POST"])
@admin_required
def messengerx_companion():
    """Register or update the MessengerX persona (the /save-companion call).

    Kept separate from the chat provider on purpose: this defines who the companion
    is, and has to succeed once before the companion can answer anything.
    """
    check_csrf()
    from .. import messengerx

    try:
        result = messengerx.save_companion(
            companion_slug=request.form.get("slug", "").strip(),
            name=request.form.get("name", "").strip(),
            description=request.form.get("description", "").strip(),
            prompt=request.form.get("prompt", "").strip(),
            image_url=request.form.get("image_url", "").strip(),
            first_message_hint=request.form.get("first_message_hint", "").strip(),
            image_prompt=request.form.get("image_prompt", "").strip(),
            moderation=_form_bool("moderation"),
        )
    except messengerx.MessengerXError as exc:
        flash_error(str(exc))
        return redirect(url_for("admin.providers_view"))

    repo.log("messengerx.companion_saved", actor=current_user(),
             target=request.form.get("slug", ""), ip=client_ip())
    flash_ok(f"Companion saved. {str(result)[:120]}")
    return redirect(url_for("admin.providers_view"))


@bp.route("/providers/<name>/test", methods=["POST"])
@admin_required
def provider_test(name: str):
    """Send one tiny request so a key can be verified without leaving the panel."""
    check_csrf()
    provider = prov.BY_NAME.get(name)
    if provider is None:
        abort(404)

    if not provider.is_configured():
        flash_error(f"{provider.label} is not configured yet.")
        return redirect(url_for("admin.providers_view"))

    try:
        if provider.kind == "anthropic":
            from .. import ai
            reply, _ = ai.chat("Reply with the single word: ok.",
                               "You are a connection test.", provider=name)
        else:
            reply = prov.complete(provider, "You are a connection test.",
                                  "Reply with the single word: ok.",
                                  schema=None, max_tokens=16, json_mode=False,
                                  timeout=25)
        repo.log("provider.tested", actor=current_user(), target=name,
                 detail="ok", ip=client_ip())
        flash_ok(f"{provider.label} responded: {str(reply).strip()[:120]}")
    except Exception as exc:
        repo.log("provider.test_failed", actor=current_user(), target=name,
                 detail=str(exc)[:500], ip=client_ip())
        flash_error(f"{provider.label} failed: {exc}")

    return redirect(url_for("admin.providers_view"))


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

BOOLEAN_SETTINGS = {"QUIZ_REQUIRE_LOGIN", "QUIZ_ALLOW_SIGNUP", "QUIZ_LEARNING",
                    "QUIZ_QUESTION_BANK"}


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_view():
    if request.method == "POST":
        check_csrf()
        actor = current_user()
        changed = []
        for key in repo.SETTING_DEFAULTS:
            if key in BOOLEAN_SETTINGS:
                value = "true" if _form_bool(key) else "false"
            elif key in request.form:
                value = request.form.get(key, "").strip()
            else:
                continue
            if str(repo.get_setting(key) or "") != value:
                changed.append(key)
            repo.set_setting(key, value, actor["id"] if actor else None)

        repo.apply_settings_to_config()
        repo.log("settings.updated", actor=actor, detail={"changed": changed},
                 ip=client_ip())
        flash_ok("Settings saved." if changed else "No changes to save.")
        return redirect(url_for("admin.settings_view"))

    return render_template("admin/settings.html",
                           settings=repo.all_settings(),
                           defaults=repo.SETTING_DEFAULTS,
                           booleans=BOOLEAN_SETTINGS)


# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------

@bp.route("/jobs")
@role_required("editor")
def jobs():
    page = max(1, _query_int("page", 1))
    per_page = 50
    status = request.args.get("status", "").strip()
    return render_template(
        "admin/jobs.html",
        jobs=repo.list_jobs(limit=per_page, offset=(page - 1) * per_page, status=status),
        page=page, per_page=per_page,
        total=repo.job_count(status), status=status,
    )


@bp.route("/jobs/export")
@role_required("editor")
def jobs_export():
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "created_at", "user", "source", "format", "engine",
                     "questions", "answered", "avg_confidence", "duration_ms",
                     "status", "error"])
    for job in repo.list_jobs(limit=10_000):
        writer.writerow([job["id"], job["created_at"], job.get("user_email") or "",
                         job["source_name"], job["source_format"], job["engine"],
                         job["questions"], job["answered"], job["avg_confidence"],
                         job["duration_ms"], job["status"], job["error"]])
    repo.log("jobs.exported", actor=current_user(), ip=client_ip())
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="quizify_jobs.csv"'},
    )


@bp.route("/jobs/purge", methods=["POST"])
@admin_required
def jobs_purge():
    check_csrf()
    days = _form_int("days", 90)
    removed = repo.purge_jobs(days)
    repo.log("jobs.purged", actor=current_user(),
             detail={"older_than_days": days, "removed": removed}, ip=client_ip())
    flash_ok(f"Removed {removed} job record(s) older than {days} days.")
    return redirect(url_for("admin.jobs"))


# --------------------------------------------------------------------------------------
# Question bank
# --------------------------------------------------------------------------------------

def _bank_filters() -> Dict[str, Any]:
    return {
        "subject": request.args.get("subject", "").strip(),
        "topic": request.args.get("topic", "").strip(),
        "term": request.args.get("q", "").strip(),
        "repeats_only": request.args.get("repeats", "") in ("1", "true", "on"),
    }


def _bank_scope() -> Dict[str, Any]:
    """Whose bank this request is looking at.

    A banked question carries the file name and subject it came from, so by default
    an account only ever sees its own. Administrators can ask for the whole install
    with ?scope=all, which is an explicit act rather than the default view.
    """
    user = current_user()
    # Read from the query string or the posted form: the listing links carry it as a
    # query argument, while the clear form carries it as a hidden field.
    requested = request.args.get("scope") or request.form.get("scope")
    everyone = requested == "all" and has_role("admin", user)
    return {"user_id": user["id"] if user else None, "everyone": everyone}


@bp.route("/questions")
@role_required("editor")
def questions():
    filters = _bank_filters()
    scope = _bank_scope()
    page = max(1, _query_int("page", 1))
    per_page = 50
    return render_template(
        "admin/questions.html",
        questions=bank.search(limit=per_page, offset=(page - 1) * per_page,
                              **filters, **scope),
        total=bank.count(**filters, **scope),
        page=page, per_page=per_page,
        subjects=bank.subjects(**scope),
        topics=bank.topics(filters["subject"], **scope),
        stats=bank.stats(**scope),
        bank_enabled=bank.enabled(),
        showing_everyone=scope["everyone"],
        can_see_everyone=has_role("admin"),
        **filters,
    )


@bp.route("/questions/export")
@role_required("editor")
def questions_export():
    """Download the filtered set — this is the 'questions by subject or topic' export."""
    filters = _bank_filters()
    output_format = (request.args.get("format", "excel") or "excel").strip().lower()
    if output_format not in ("excel", "xlsx", "csv", "json"):
        output_format = "excel"

    rows = bank.search(limit=5000, **filters, **_bank_scope())
    if not rows:
        flash_error("No questions match those filters.")
        return redirect(url_for("admin.questions", **{k: v for k, v in filters.items() if v}))

    result = bank.to_result(rows)
    # Name the file after whatever narrowed the set, so downloads stay distinguishable.
    parts = [p for p in (filters["subject"], filters["topic"]) if p] or ["all"]
    basename = re.sub(r"[^A-Za-z0-9._-]+", "_", "questions_" + "_".join(parts)).strip("._-")

    widest = max((len(q.options) for q in result.questions), default=4)
    buffer, mimetype, filename = export_questions(
        result, output_format, filters["subject"], filters["topic"],
        max_options=max(2, min(widest, 26)), basename=basename or "questions",
    )
    repo.log("questions.exported", actor=current_user(),
             detail={"count": len(rows), **{k: v for k, v in filters.items() if v}},
             ip=client_ip())
    return Response(buffer.getvalue(), mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.route("/questions/<int:question_id>/delete", methods=["POST"])
@admin_required
def questions_delete(question_id: int):
    check_csrf()
    # Removes this account's history of the question. The shared row survives while
    # anybody else still references it.
    if bank.delete(question_id, **_bank_scope()):
        repo.log("question.deleted", actor=current_user(),
                 target=str(question_id), ip=client_ip())
        flash_ok("Question removed from the bank.")
    else:
        flash_error("That question is no longer in the bank.")
    return redirect(request.referrer or url_for("admin.questions"))


@bp.route("/questions/clear", methods=["POST"])
@admin_required
def questions_clear():
    check_csrf()
    # The same filters the page was showing, so this deletes exactly what was listed.
    filters = {
        "subject": request.form.get("subject", "").strip(),
        "topic": request.form.get("topic", "").strip(),
        "term": request.form.get("q", "").strip(),
        "repeats_only": request.form.get("repeats", "") in ("1", "true", "on"),
    }
    removed = bank.clear(**filters, **_bank_scope())
    repo.log("questions.cleared", actor=current_user(),
             detail={"removed": removed, **filters}, ip=client_ip())
    flash_ok(f"Removed {removed} question(s) from the bank.")
    return redirect(url_for("admin.questions"))


# --------------------------------------------------------------------------------------
# Corrections (the accuracy feedback loop)
# --------------------------------------------------------------------------------------

@bp.route("/corrections")
@role_required("editor")
def corrections():
    return render_template(
        "admin/corrections.html",
        corrections=learning.recent(limit=300),
        total=learning.count(),
        learning_enabled=learning.enabled(),
    )


@bp.route("/corrections/<int:correction_id>/delete", methods=["POST"])
@admin_required
def corrections_delete(correction_id: int):
    check_csrf()
    if learning.delete(correction_id):
        repo.log("correction.deleted", actor=current_user(),
                 target=str(correction_id), ip=client_ip())
        flash_ok("Correction removed.")
    else:
        flash_error("That correction no longer exists.")
    return redirect(url_for("admin.corrections"))


@bp.route("/corrections/clear", methods=["POST"])
@admin_required
def corrections_clear():
    check_csrf()
    removed = learning.clear()
    repo.log("corrections.cleared", actor=current_user(),
             detail={"removed": removed}, ip=client_ip())
    flash_ok(f"Removed {removed} stored correction(s).")
    return redirect(url_for("admin.corrections"))


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------

@bp.route("/audit")
@admin_required
def audit():
    action = request.args.get("action", "").strip()
    return render_template("admin/audit.html",
                           entries=repo.list_audit(limit=300, action=action),
                           actions=repo.audit_actions(), action=action)


# --------------------------------------------------------------------------------------
# Profile (any signed-in user)
# --------------------------------------------------------------------------------------

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        check_csrf()
        if request.form.get("action") == "password":
            current = request.form.get("current_password") or ""
            if not repo.verify_password(user, current):
                flash_error("Your current password is incorrect.")
                return redirect(url_for("admin.profile"))
            try:
                validate_password(request.form.get("password") or "",
                                  request.form.get("confirm"))
            except ValueError as exc:
                flash_error(str(exc))
                return redirect(url_for("admin.profile"))
            repo.set_password(user["id"], request.form.get("password"))
            repo.log("profile.password_changed", actor=user, ip=client_ip())
            flash_ok("Password changed.")
        else:
            repo.update_user(user["id"], name=(request.form.get("name") or "").strip())
            repo.log("profile.updated", actor=user, ip=client_ip())
            flash_ok("Profile updated.")
        return redirect(url_for("admin.profile"))

    return render_template("admin/profile.html", user=user,
                           usage_today=repo.usage_today(user["id"]),
                           recent_jobs=repo.list_jobs(limit=10, user_id=user["id"]))


# --------------------------------------------------------------------------------------
# Online exams: list assigned quizzes and view their results
# --------------------------------------------------------------------------------------

@bp.route("/exams")
@login_required
def exams_list():
    """Every exam this user may see: an admin sees all, others see their own."""
    user = current_user()
    scope = None if has_role("admin") else user["id"]
    return render_template("admin/exams.html",
                           exams=exams.list_exams(created_by=scope),
                           showing_everyone=has_role("admin"))


@bp.route("/exams/<int:exam_id>")
@login_required
def exam_detail(exam_id: int):
    """Results dashboard for one exam: stats plus every attempt."""
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)
    share_url = url_for("take_exam", token=exam["token"], _external=True)
    # Enrolled students, grouped for the assignment picker.
    all_students = repo.list_users(role="student", limit=1000)
    batches = sorted({s.get("batch") or "" for s in all_students if s.get("batch")})
    assigned_ids = set(exams.assigned_user_ids(exam_id))
    return render_template("admin/exam_detail.html", exam=exam,
                           attempts=exams.list_attempts(exam_id),
                           stats=exams.exam_stats(exam_id),
                           share_url=share_url,
                           all_students=all_students, batches=batches,
                           assigned_ids=assigned_ids,
                           assigned_students=exams.assigned_students(exam_id))


@bp.route("/exams/<int:exam_id>/toggle", methods=["POST"])
@login_required
def exam_toggle(exam_id: int):
    check_csrf()
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)
    exams.set_open(exam["token"], not exam["is_open"])
    repo.log("exam.toggled", actor=current_user(), target=str(exam_id),
             detail={"is_open": not exam["is_open"]}, ip=client_ip())
    flash_ok("Exam reopened." if not exam["is_open"] else "Exam closed to new attempts.")
    return redirect(url_for("admin.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
@login_required
def exam_delete(exam_id: int):
    check_csrf()
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)
    exams.delete_exam(exam_id)
    repo.log("exam.deleted", actor=current_user(), target=str(exam_id), ip=client_ip())
    flash_ok("Exam and its attempts were deleted.")
    return redirect(url_for("admin.exams_list"))


@bp.route("/exams/<int:exam_id>/assign", methods=["POST"])
@login_required
def exam_assign(exam_id: int):
    """Assign this exam to whole batches and/or individually-picked students."""
    check_csrf()
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)

    user_ids = set()
    # Individually ticked students.
    for sid in request.form.getlist("student_ids"):
        try:
            user_ids.add(int(sid))
        except (TypeError, ValueError):
            pass
    # Whole batches selected -> expand to their students.
    batches = set(request.form.getlist("batches"))
    if batches:
        for s in repo.list_users(role="student", limit=1000):
            if (s.get("batch") or "") in batches:
                user_ids.add(s["id"])

    if not user_ids:
        flash_error("Select at least one student or batch to assign.")
        return redirect(url_for("admin.exam_detail", exam_id=exam_id))

    n = exams.assign_students(exam_id, list(user_ids), assigned_by=current_user()["id"])
    repo.log("exam.assigned", actor=current_user(), target=str(exam_id),
             detail={"added": n, "total_selected": len(user_ids)}, ip=client_ip())
    flash_ok(f"Assigned to {n} new student(s).")
    return redirect(url_for("admin.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/unassign/<int:user_id>", methods=["POST"])
@login_required
def exam_unassign(exam_id: int, user_id: int):
    check_csrf()
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)
    exams.unassign_student(exam_id, user_id)
    flash_ok("Student unassigned.")
    return redirect(url_for("admin.exam_detail", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@login_required
def exam_edit(exam_id: int):
    """Reconfigure a live exam: title, instructions, schedule, options, proctoring,
    and the questions themselves."""
    exam = exams.get_exam_by_id(exam_id)
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)

    if request.method == "POST":
        check_csrf()
        settings = {
            "time_limit_min": request.form.get("time_limit_min") or 0,
            "shuffle_q": request.form.get("shuffle_q") == "on",
            "shuffle_opts": request.form.get("shuffle_opts") == "on",
            "reveal_answers": request.form.get("reveal_answers") == "on",
            "allow_pdf": request.form.get("allow_pdf") == "on",
            "available_from": request.form.get("available_from", ""),
            "available_until": request.form.get("available_until", ""),
            "instructions": request.form.get("instructions", ""),
            "proctored": request.form.get("proctored") == "on",
            "proctor_interval": request.form.get("proctor_interval") or 20,
        }
        # Optional question edits arrive as a JSON blob from the editor.
        questions = None
        raw = request.form.get("questions_json", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    questions = parsed
            except (ValueError, TypeError):
                flash_error("Could not read the edited questions; other changes were still saved.")

        try:
            exams.update_exam(
                exam_id,
                title=request.form.get("title", ""),
                subject=request.form.get("subject", ""),
                topic=request.form.get("topic", ""),
                settings=settings, questions=questions,
            )
        except ValueError as exc:
            flash_error(str(exc))
            return redirect(url_for("admin.exam_edit", exam_id=exam_id))
        repo.log("exam.updated", actor=current_user(), target=str(exam_id), ip=client_ip())
        flash_ok("Exam updated.")
        return redirect(url_for("admin.exam_detail", exam_id=exam_id))

    return render_template("admin/exam_edit.html", exam=exam)


@bp.route("/exams/attempt/<int:attempt_id>/proctor")
@login_required
def exam_proctor(attempt_id: int):
    """Live proctor view for one attempt: the latest webcam frames, auto-refreshing."""
    attempt = exams.get_attempt(attempt_id)
    if attempt is None:
        abort(404)
    exam = exams.get_exam_by_id(attempt["exam_id"])
    if exam is None:
        abort(404)
    if not has_role("admin") and exam.get("created_by") not in (None, current_user()["id"]):
        abort(403)
    return render_template("admin/exam_proctor.html", exam=exam, attempt=attempt,
                           snapshots=exams.list_snapshots(attempt_id))


@bp.route("/exams/attempt/<int:attempt_id>/latest-frame")
def exam_latest_frame(attempt_id: int):
    """JSON: the newest snapshot for an attempt (polled by the live proctor view).

    Auth is checked inline rather than via @login_required so an expired session
    returns 401 JSON — the polling view can show a clear message instead of silently
    following a redirect to the login HTML.
    """
    user = current_user()
    if user is None:
        return jsonify({"error": "auth"}), 401
    attempt = exams.get_attempt(attempt_id)
    if attempt is None:
        return jsonify({"error": "not_found"}), 404
    exam = exams.get_exam_by_id(attempt["exam_id"])
    if exam and not has_role("admin") and exam.get("created_by") not in (None, user["id"]):
        return jsonify({"error": "forbidden"}), 403
    snap = exams.latest_snapshot(attempt_id)
    return jsonify({
        "image": snap["image"] if snap else "",
        "at": snap["created_at"] if snap else "",
        "count": exams.snapshot_count(attempt_id),
    })


@bp.route("/exams/attempt/<int:attempt_id>/live")
def exam_live(attempt_id: int):
    """JSON: the newest in-memory live frame for an attempt (polled fast for motion).

    Falls back to the latest durable snapshot when no live frame is present (e.g. the
    learner has finished, so only the recorded timeline remains).
    """
    user = current_user()
    if user is None:
        return jsonify({"error": "auth"}), 401
    attempt = exams.get_attempt(attempt_id)
    if attempt is None:
        return jsonify({"error": "not_found"}), 404
    exam = exams.get_exam_by_id(attempt["exam_id"])
    if exam and not has_role("admin") and exam.get("created_by") not in (None, user["id"]):
        return jsonify({"error": "forbidden"}), 403

    frame = exams.get_live_frame(attempt_id)
    if frame:
        return jsonify({"image": frame["image"], "live": frame["live"],
                        "age": frame["age"], "source": "live"})
    snap = exams.latest_snapshot(attempt_id)
    return jsonify({"image": snap["image"] if snap else "", "live": False,
                    "age": None, "source": "snapshot" if snap else "none"})


@bp.route("/exams/from-bank", methods=["GET", "POST"])
@role_required("editor")
def exam_from_bank():
    """Build an exam from questions already in the bank.

    GET shows a filterable, checkable list of banked questions. POST takes the
    selected ids (plus exam settings) and creates the exam, then jumps to its
    results dashboard where the share link lives.
    """
    scope = _bank_scope()

    if request.method == "POST":
        check_csrf()
        ids = request.form.getlist("qid")
        rows = bank.get_by_ids([i for i in ids if i])
        if not rows:
            flash_error("Select at least one question to build an exam.")
            return redirect(url_for("admin.exam_from_bank",
                                    subject=request.form.get("subject", ""),
                                    topic=request.form.get("topic", "")))

        # Reuse the existing bank->questions conversion, then the shared exam builder.
        result = bank.to_result(rows)
        questions = [q.to_dict() for q in result.questions]
        settings = {
            "time_limit_min": request.form.get("time_limit_min") or 0,
            "shuffle_q": request.form.get("shuffle_q") == "on",
            "shuffle_opts": request.form.get("shuffle_opts") == "on",
            "reveal_answers": request.form.get("reveal_answers") == "on",
            "allow_pdf": request.form.get("allow_pdf") == "on",
            "available_from": request.form.get("available_from", ""),
            "available_until": request.form.get("available_until", ""),
        }
        try:
            exam = exams.create_exam(
                title=request.form.get("title", "").strip(),
                subject=request.form.get("subject", "").strip(),
                topic=request.form.get("topic", "").strip(),
                questions=questions, settings=settings,
                created_by=current_user()["id"],
            )
        except ValueError as exc:
            flash_error(str(exc))
            return redirect(url_for("admin.exam_from_bank"))
        repo.log("exam.created_from_bank", actor=current_user(),
                 target=exam["token"], detail={"count": len(questions)}, ip=client_ip())
        flash_ok(f"Created “{exam['title']}” from {len(questions)} banked question(s).")
        return redirect(url_for("admin.exam_detail", exam_id=exam["id"]))

    filters = _bank_filters()
    return render_template(
        "admin/exam_from_bank.html",
        questions=bank.search(limit=500, **filters, **scope),
        subjects=bank.subjects(**scope),
        topics=bank.topics(filters["subject"], **scope),
        bank_enabled=bank.enabled(),
        **filters,
    )


# --------------------------------------------------------------------------------------
# Student enrollment
# --------------------------------------------------------------------------------------

@bp.route("/students")
@role_required("editor")
def students():
    """List enrolled students, optionally filtered by batch or search."""
    q = (request.args.get("q") or "").strip()
    batch = (request.args.get("batch") or "").strip()
    rows = repo.list_users(search=q, role="student", limit=1000)
    if batch:
        rows = [r for r in rows if (r.get("batch") or "") == batch]
    batches = sorted({r.get("batch") or "" for r in repo.list_users(role="student", limit=1000) if r.get("batch")})
    return render_template("admin/students.html", students=rows, batches=batches,
                           q=q, batch=batch)


@bp.route("/students/new", methods=["GET", "POST"])
@role_required("editor")
def student_new():
    """Enroll a student: Batch, Name, Email, Roll Number, Password."""
    if request.method == "POST":
        check_csrf()
        email = (request.form.get("email") or "").strip()
        name = (request.form.get("name") or "").strip()
        batch = (request.form.get("batch") or "").strip()
        roll_no = (request.form.get("roll_no") or "").strip()
        password = request.form.get("password") or ""
        try:
            if "@" not in email:
                raise ValueError("Enter a valid email address.")
            if repo.get_user_by_email(email):
                raise ValueError("A user with that email already exists.")
            validate_password(password)
        except ValueError as exc:
            flash_error(str(exc))
            return render_template("admin/student_form.html", mode="new", student=request.form)
        uid = repo.create_user(email, password, name=name, role="student",
                               batch=batch, roll_no=roll_no)
        repo.log("student.enrolled", actor=current_user(), target=email,
                 detail={"batch": batch, "roll_no": roll_no}, ip=client_ip())
        flash_ok(f"Enrolled {name or email}.")
        return redirect(url_for("admin.students"))
    return render_template("admin/student_form.html", mode="new", student={})


# Header aliases so the CSV is forgiving about column naming/casing.
_CSV_FIELDS = {
    "batch":    {"batch", "batch name", "class", "batch_name"},
    "name":     {"name", "student name", "student", "full name", "student_name"},
    "email":    {"email", "email address", "e-mail", "mail"},
    "roll_no":  {"roll", "roll no", "roll number", "roll_no", "rollno", "roll_number"},
    "password": {"password", "pass", "pwd"},
}


def _map_csv_headers(headers):
    """Map the CSV's header row to our field names. Returns {field: column_index}."""
    mapping = {}
    for i, h in enumerate(headers or []):
        key = (h or "").strip().lower()
        for field, aliases in _CSV_FIELDS.items():
            if key in aliases and field not in mapping:
                mapping[field] = i
    return mapping


@bp.route("/students/import", methods=["GET", "POST"])
@role_required("editor")
def student_import():
    """Bulk-enroll students from a CSV: Batch, Name, Email, Roll Number, Password."""
    if request.method == "GET":
        return render_template("admin/student_import.html")

    check_csrf()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash_error("Choose a CSV file to upload.")
        return redirect(url_for("admin.student_import"))

    try:
        raw = upload.read().decode("utf-8-sig", errors="replace")
    except Exception:
        flash_error("Could not read that file. Please upload a UTF-8 CSV.")
        return redirect(url_for("admin.student_import"))

    reader = csv.reader(io.StringIO(raw))
    try:
        headers = next(reader)
    except StopIteration:
        flash_error("The CSV is empty.")
        return redirect(url_for("admin.student_import"))

    cols = _map_csv_headers(headers)
    if "email" not in cols:
        flash_error("The CSV must have an 'Email' column. Download the sample for the right format.")
        return redirect(url_for("admin.student_import"))

    default_pw = (request.form.get("default_password") or "").strip()
    created, skipped, errors = 0, 0, []

    def cell(row, field):
        idx = cols.get(field)
        return (row[idx].strip() if idx is not None and idx < len(row) else "")

    for line_no, row in enumerate(reader, start=2):
        if not any((c or "").strip() for c in row):
            continue  # skip blank lines
        email = cell(row, "email")
        name = cell(row, "name")
        batch = cell(row, "batch")
        roll_no = cell(row, "roll_no")
        password = cell(row, "password") or default_pw

        if "@" not in email:
            errors.append(f"Row {line_no}: invalid or missing email.")
            continue
        if repo.get_user_by_email(email):
            skipped += 1
            continue
        try:
            validate_password(password)
        except ValueError as exc:
            errors.append(f"Row {line_no} ({email}): {exc}")
            continue
        repo.create_user(email, password, name=name, role="student",
                         batch=batch, roll_no=roll_no)
        created += 1

    repo.log("student.imported", actor=current_user(),
             detail={"created": created, "skipped": skipped, "errors": len(errors)},
             ip=client_ip())

    msg = f"Imported {created} student(s)."
    if skipped:
        msg += f" Skipped {skipped} existing email(s)."
    flash_ok(msg)
    for e in errors[:10]:
        flash_error(e)
    if len(errors) > 10:
        flash_error(f"…and {len(errors) - 10} more row error(s).")
    return redirect(url_for("admin.students"))


@bp.route("/students/import/sample")
@role_required("editor")
def student_import_sample():
    """Download a sample CSV with the expected columns."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Batch", "Name", "Email", "Roll Number", "Password"])
    w.writerow(["CS-2026", "Ravi Kumar", "ravi@example.com", "CS01", "welcome@123"])
    w.writerow(["CS-2026", "Asha Patel", "asha@example.com", "CS02", "welcome@123"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="students_sample.csv"'})


@bp.route("/students/<int:user_id>", methods=["GET", "POST"])
@role_required("editor")
def student_edit(user_id: int):
    student = repo.get_user(user_id)
    if student is None or student["role"] != "student":
        abort(404)
    if request.method == "POST":
        check_csrf()
        repo.update_user(user_id,
                         name=(request.form.get("name") or "").strip(),
                         email=(request.form.get("email") or "").strip(),
                         batch=(request.form.get("batch") or "").strip(),
                         roll_no=(request.form.get("roll_no") or "").strip(),
                         is_active=request.form.get("is_active") == "on")
        pw = request.form.get("password") or ""
        if pw:
            try:
                validate_password(pw)
                repo.set_password(user_id, pw)
            except ValueError as exc:
                flash_error(str(exc))
                return redirect(url_for("admin.student_edit", user_id=user_id))
        repo.log("student.updated", actor=current_user(), target=str(user_id), ip=client_ip())
        flash_ok("Student updated.")
        return redirect(url_for("admin.students"))
    return render_template("admin/student_form.html", mode="edit", student=student)


@bp.route("/students/<int:user_id>/delete", methods=["POST"])
@role_required("editor")
def student_delete(user_id: int):
    check_csrf()
    student = repo.get_user(user_id)
    if student is None or student["role"] != "student":
        abort(404)
    repo.delete_user(user_id)
    repo.log("student.deleted", actor=current_user(), target=str(user_id), ip=client_ip())
    flash_ok("Student removed.")
    return redirect(url_for("admin.students"))


def _query_int(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
