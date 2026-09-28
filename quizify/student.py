"""Student portal: a separate login and dashboard for enrolled students.

Students authenticate with the same secure session mechanism as staff (via
``admin.auth``) but land here instead of the admin panel. They can only see and take
exams that have been assigned to their account, and only within the exam's schedule.

Kept as its own blueprint (``/student``) so the student experience is fully isolated
from the admin UI.
"""

import functools
from typing import Any, Callable

from flask import (
    Blueprint, abort, redirect, render_template, request, url_for,
)

from . import exams
from .admin import auth, repo

bp = Blueprint("student", __name__, url_prefix="/student")


def current_student():
    """The signed-in user, but only when they are a student; else None."""
    user = auth.current_user()
    if user and user.get("role") == "student":
        return user
    return None


def student_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if current_student() is None:
            return redirect(url_for("student.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


@bp.app_context_processor
def _inject():
    from . import config
    return {"app_name": config.get("APP_NAME", "Quizify"),
            "csrf_token": auth.csrf_token,
            "student": current_student()}


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_student() is not None:
        return redirect(url_for("student.portal"))

    if request.method == "POST":
        auth.check_csrf()
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        try:
            user = auth.login(email, password)
        except auth.LoginError as exc:
            return render_template("student/login.html", error=str(exc), email=email), 401
        if user.get("role") != "student":
            # A non-student authenticated here — send them out; the portal is students only.
            auth.logout()
            return render_template(
                "student/login.html",
                error="This login is for students. Staff should use the admin panel.",
                email=email), 403
        target = request.args.get("next") or url_for("student.portal")
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("student.portal")
        return redirect(target)

    return render_template("student/login.html", email="")


@bp.route("/logout", methods=["POST"])
def logout():
    auth.check_csrf()
    auth.logout()
    return redirect(url_for("student.login"))


# --------------------------------------------------------------------------------------
# Portal
# --------------------------------------------------------------------------------------

@bp.route("/")
@student_required
def portal():
    """List the exams assigned to this student, with schedule/attempt status."""
    student = current_student()
    rows = exams.exams_for_student(student["id"])
    now = _now_str()
    for e in rows:
        e["state"] = _exam_state(e, now)
    return render_template("student/portal.html", student=student, exams=rows, now=now)


@bp.route("/exam/<token>")
@student_required
def take(token: str):
    """Open an assigned exam for the student — enforcing assignment and schedule."""
    student = current_student()
    exam = exams.get_exam(token)
    if exam is None:
        abort(404)
    if not exams.is_assigned(exam["id"], student["id"]):
        return render_template("student/blocked.html", reason="not_assigned", exam=exam), 403

    avail = exams.availability(exam)
    if not avail["open"]:
        return render_template("student/blocked.html", reason=avail["reason"],
                               exam=exam, avail=avail), 403

    # One attempt only: if they already submitted, they may not re-attempt or review.
    done = exams.student_attempt(exam["id"], student["id"])
    if done:
        return render_template("student/blocked.html", reason="completed",
                               exam=exam, attempt=done), 403

    import random
    questions = [exams.public_question(q) for q in exam["questions"]]
    if exam["shuffle_q"]:
        random.shuffle(questions)
    if exam["shuffle_opts"]:
        for q in questions:
            random.shuffle(q["options"])
    return render_template("student/exam.html", exam=exam, questions=questions,
                           student=student)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _now_str() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")


def _exam_state(e: dict, now: str) -> str:
    """A student-facing status for an assigned exam card."""
    if e.get("attempt_id"):
        return "completed"
    if not e.get("is_open"):
        return "closed"
    start = (e.get("available_from") or "").strip()
    end = (e.get("available_until") or "").strip()
    if start and now < start:
        return "scheduled"     # not open yet
    if end and now > end:
        return "expired"
    return "available"
