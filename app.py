"""Quizify — convert quiz documents of any format into Excel, CSV, or JSON.

This module is the HTTP layer only. All parsing lives in the ``quizify`` package:

    quizify/extractors.py  file/paste -> Blocks
    quizify/engine.py      Blocks -> raw questions (style profiling + segmentation)
    quizify/analysis.py    raw questions -> typed, answered, validated questions
    quizify/ai.py          optional AI assist across many providers
    quizify/export.py      results -> xlsx / csv / json
    quizify/admin/         admin panel: users, settings, providers, jobs, audit
"""

import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

import quizify
from quizify import admin, ai, config, exams, features, signaling
from quizify import student as student_portal
from quizify.extractors import SUPPORTED_EXTS, ExtractionError

logging.basicConfig(level=os.environ.get("QUIZ_LOG_LEVEL", "INFO"))
log = logging.getLogger("quizify")

app = Flask(__name__)

# WebSocket support for live proctoring signalling (WebRTC offer/answer/ICE relay).
try:
    from flask_sock import Sock
    sock = Sock(app)
    WEBSOCKETS_OK = True
except ImportError:  # pragma: no cover - live proctoring degrades to snapshots
    sock = None
    WEBSOCKETS_OK = False

# The admin panel owns configuration from here on: it opens the database, loads
# stored settings into the runtime overlay, and registers /admin. Everything below
# reads through `config`, so a change saved in the panel applies immediately.
admin.init_admin(app)
app.register_blueprint(student_portal.bp)

app.config["MAX_CONTENT_LENGTH"] = config.get_int("QUIZ_MAX_UPLOAD_MB", 25) * 1024 * 1024

VALID_FORMATS = {"excel", "xlsx", "csv", "json"}
VALID_AI_MODES = {"off", "auto", "always"}
VALID_ANSWER_STYLES = {"label", "answer", "choice"}


# --------------------------------------------------------------------------------------
# Authentication gate
# --------------------------------------------------------------------------------------
# Nothing in the app is visible until you sign in. An anonymous visitor may only reach
# the login pages (admin + student), static assets, and the health check; every other
# request is redirected to the admin login. Signed-in staff get full access; signed-in
# students are further confined by the student sandbox below.

# Endpoints an anonymous visitor is allowed to hit (login flow + infrastructure).
_PUBLIC_ENDPOINTS = {
    "admin.login", "admin.setup",      # admin sign-in / first-run admin creation
    "student.login",                   # student sign-in
    "api_health",                      # health check
    "static",                          # static files
}


@app.before_request
def _require_login():
    # Signed-in users (staff or student) pass this gate; the student sandbox below
    # then applies its own tighter rules.
    if admin.current_user() is not None:
        return

    endpoint = request.endpoint or ""
    if endpoint in _PUBLIC_ENDPOINTS:
        return
    # Blueprint static endpoints (e.g. "admin.static") are also fine.
    if endpoint.endswith(".static"):
        return

    # Anonymous and hitting anything else: block. APIs get JSON 401; pages redirect
    # to the login screen so the app never renders for a logged-out visitor.
    if request.path.startswith("/api/") or request.path.startswith("/ws/"):
        return jsonify({"error": "Sign in required."}), 401
    return redirect(url_for("admin.login", next=request.full_path))


# --------------------------------------------------------------------------------------
# Student sandbox
# --------------------------------------------------------------------------------------
# A signed-in student may only reach their own portal and the endpoints needed to take
# an exam assigned to them. The converter, its APIs, and everyone else's data are off
# limits. This is enforced centrally so no route can accidentally leak to a student.

# Exam-taking API paths a student legitimately needs while taking an assigned exam.
# These carry their own per-exam assignment/ownership checks.
_STUDENT_EXAM_API_PREFIXES = ("/api/exam/",)   # start, submit, snapshot, live-frame


@app.before_request
def _confine_students():
    student = student_portal.current_student()
    if student is None:
        return  # not a student session — normal access rules apply

    path = request.path

    # Always-allowed: the student portal, static files, health, the live proctor WS,
    # and the exam-taking APIs (each of which re-checks assignment ownership).
    if (path.startswith("/student")
            or path.startswith("/static/")
            or path.startswith("/ws/proctor/")
            or path == "/api/health"
            or path.startswith(_STUDENT_EXAM_API_PREFIXES)):
        return

    # Everything else is off-limits: the converter and its APIs, exam creation, the
    # public take page, AND result/review pages — a student takes each exam once and
    # cannot review it afterwards. All roads lead back to their portal.
    if path.startswith("/api/"):
        return jsonify({"error": "Not available for student accounts."}), 403
    return redirect(url_for("student.portal"))


# --------------------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------------------

class BadRequest(Exception):
    """A user-facing input problem — reported as a message, not a stack trace."""


def _settings() -> features.Settings:
    """The effective settings for whoever is making this request.

    Resolved once per request and threaded through, so a conversion cannot see one
    value while deciding what to do and a different one while doing it.
    """
    return features.for_user(admin.current_user())


@app.context_processor
def inject_defaults() -> Dict[str, Any]:
    """Expose the viewer's own defaults, so the form reflects what they may actually do."""
    settings = _settings()
    return {
        "app_name": config.get("APP_NAME", "Quizify"),
        "defaults": {
            "max_options": max(2, min(settings.max_options(), 26)),
            "format": config.get("QUIZ_DEFAULT_FORMAT", "excel"),
            "ai_mode": settings.ai_mode(settings.raw("QUIZ_DEFAULT_AI_MODE") or "auto"),
        },
        "allowed": {
            "ai": settings.ai_allowed(),
            "editing": settings.editing(),
            "formats": sorted(settings.allowed_formats(VALID_FORMATS)),
        },
    }


def _read_form(source: Optional[Dict[str, Any]] = None,
               settings: Optional[features.Settings] = None) -> Dict[str, Any]:
    """Read converter options from the posted form, or from a JSON body.

    The export endpoint carries the same options in JSON rather than form fields,
    so both entry points normalise and clamp values identically. Every limit comes
    from ``settings``, which means a per-user restriction is applied here rather than
    trusted to the browser that submitted the form.
    """
    form = request.form if source is None else source
    settings = settings or _settings()

    def text(key: str) -> str:
        value = form.get(key)
        return "" if value is None else str(value).strip()

    allowed_formats = settings.allowed_formats(VALID_FORMATS)
    output_format = (text("format") or config.get("QUIZ_DEFAULT_FORMAT", "excel")).lower()
    if output_format not in allowed_formats:
        output_format = "excel" if "excel" in allowed_formats else sorted(allowed_formats)[0]

    ai_mode = (text("ai_mode") or settings.raw("QUIZ_DEFAULT_AI_MODE") or "auto").lower()
    if ai_mode not in VALID_AI_MODES:
        ai_mode = "auto"
    # An account without AI stays on the rule engine however the form was submitted.
    ai_mode = settings.ai_mode(ai_mode)

    default_options = settings.max_options()
    try:
        max_options = int(text("max_options") or default_options)
    except (TypeError, ValueError):
        max_options = default_options
    max_options = max(2, min(max_options, 26))

    # How the Answer column labels a correct option in the output.
    answer_style = (text("answer_style") or "label").lower()
    if answer_style not in VALID_ANSWER_STYLES:
        answer_style = "label"

    return {
        "subject": text("subject"),
        "topic": text("topic"),
        "format": output_format,
        "ai_mode": ai_mode,
        "max_options": max_options,
        "answer_style": answer_style,
        # Empty means "use the configured chain, best first".
        "provider": text("provider").lower() or None,
    }


def _parse_request(options: Dict[str, Any],
                   settings: Optional[features.Settings] = None) -> quizify.ParseResult:
    """Parse whichever input the request supplied — an upload or pasted text."""
    settings = settings or _settings()
    upload = request.files.get("file")
    quiz_text = (request.form.get("quiz_text") or "").strip()

    if upload and upload.filename:
        filename = secure_filename(upload.filename) or "upload"
        extension = os.path.splitext(filename)[1].lower()
        allowed = settings.allowed_extensions(SUPPORTED_EXTS)
        if extension and extension not in allowed:
            raise BadRequest(
                f"'{extension}' files are not accepted. Allowed types: "
                + ", ".join(sorted(e.lstrip('.') for e in allowed))
            )
        data = upload.read()
        if not data:
            raise BadRequest("The uploaded file is empty.")
        # The app-wide limit is the outer ceiling; this applies the account's own,
        # which can only ever be the tighter of the two.
        limit = settings.max_upload_bytes()
        if len(data) > limit:
            raise BadRequest(
                f"That file is {len(data) / (1024 * 1024):.1f} MB. Your limit is "
                f"{limit // (1024 * 1024)} MB.")
        return quizify.parse_file(
            data, filename,
            subject=options["subject"], topic=options["topic"],
            ai_mode=options["ai_mode"], provider=options["provider"],
        )

    if quiz_text:
        return quizify.parse_text(
            quiz_text,
            subject=options["subject"], topic=options["topic"],
            ai_mode=options["ai_mode"], provider=options["provider"],
        )

    raise BadRequest("Please either upload a file or paste quiz text.")


def _error_page(message: str, status: int) -> Tuple[str, int]:
    return render_template("index.html", error=message), status


def _download_basename(result: quizify.ParseResult) -> str:
    """Derive a safe download name from the source, e.g. 'biology_quiz_questions'."""
    stem = os.path.splitext(os.path.basename(result.source_name or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return f"{stem or 'quiz'}_questions"


class _LoginRequired(Exception):
    """The converter is configured to require a sign-in."""


class _QuotaExceeded(Exception):
    """The caller is over their daily conversion limit."""


def _gate(check_quota: bool = True) -> Optional[Dict[str, Any]]:
    """Enforce the optional sign-in requirement and per-user daily quota.

    Returns the signed-in user, or None when the converter is open to everyone.

    The quota counts conversions, so only the parsing endpoints spend it. Exporting
    an already-parsed result passes ``check_quota=False``: the conversion was paid
    for at preview time, and charging again would strand a user on their last slot
    with a result they could see but not download.
    """
    user = admin.current_user()
    if config.get_bool("QUIZ_REQUIRE_LOGIN", False) and user is None:
        raise _LoginRequired()
    if check_quota and user is not None:
        # A per-user quota override stands in for the column when the column is unset,
        # so a limit can be granted from either place.
        quota = features.for_user(user).daily_quota()
        if quota and admin.repo.usage_today(user["id"]) >= quota:
            raise _QuotaExceeded(
                f"You have reached your daily limit of {quota} conversions.")
    return user


def _source_label(result: Optional[quizify.ParseResult]) -> str:
    if result is not None:
        return (result.source_name or "pasted text")[:255]
    upload = request.files.get("file")
    return ((upload.filename or "upload") if upload else "pasted text")[:255]


def _record(options: Dict[str, Any], user: Optional[Dict[str, Any]],
            result: Optional[quizify.ParseResult], started: float,
            status: str = "ok", error: str = "") -> None:
    """Record a conversion attempt.

    Only reached once a request has passed the gate, so a rejected request never
    lands in the history and never counts against the caller's quota.
    """
    stats = result.stats if result else {}
    admin.record_job(
        user_id=user["id"] if user else None,
        source_name=_source_label(result),
        source_format=(result.source_format if result else ""),
        subject=options.get("subject", ""),
        topic=options.get("topic", ""),
        output_format=options.get("format", ""),
        engine=(result.engine if result else ""),
        questions=stats.get("total", 0),
        answered=stats.get("answered", 0),
        avg_confidence=stats.get("avg_confidence", 0.0),
        duration_ms=int((time.time() - started) * 1000),
        status=status,
        error=error[:1000],
        ip=admin.auth.client_ip(),
    )


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        if config.get_bool("QUIZ_REQUIRE_LOGIN", False) and admin.current_user() is None:
            return redirect(url_for("admin.login", next=request.full_path))
        return render_template("index.html")

    settings = _settings()
    options = _read_form(settings=settings)
    started = time.time()
    try:
        user = _gate()
        result = _parse_request(options, settings)
    except _LoginRequired:
        return redirect(url_for("admin.login", next=url_for("index")))
    except _QuotaExceeded as exc:
        return _error_page(str(exc), 429)
    except (BadRequest, ExtractionError) as exc:
        _record(options, admin.current_user(), None, started, "error", str(exc))
        return _error_page(str(exc), 400)
    except Exception as exc:  # unexpected — log it, show something useful
        log.exception("Parsing failed")
        _record(options, admin.current_user(), None, started, "error", str(exc))
        return _error_page(f"Could not process this input: {exc}", 500)

    if not result.questions:
        message = result.warnings[0] if result.warnings else "No questions found."
        _record(options, user, result, started, "empty", message)
        return _error_page(message, 400)

    buffer, mimetype, filename = quizify.export(
        result, options["format"], options["subject"], options["topic"],
        max_options=options["max_options"], basename=_download_basename(result),
        answer_style=options["answer_style"],
    )
    _record(options, user, result, started)
    # This path downloads directly, so it banks its questions just like /api/export.
    if settings.question_bank():
        try:
            quizify.bank.record(
                [q.to_dict() for q in result.questions],
                user_id=user["id"] if user else None,
                subject=options["subject"], topic=options["topic"],
                source_name=result.source_name, source_format=result.source_format,
                engine=result.engine,
            )
        except Exception:
            log.exception("Could not update the question bank")
    log.info("Parsed %s question(s) from %s via %s",
             len(result.questions), result.source_name or "pasted text", result.engine)
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype=mimetype)


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """Parse and return JSON instead of a download — lets the UI preview before exporting."""
    settings = _settings()
    options = _read_form(settings=settings)
    started = time.time()
    try:
        user = _gate()
        result = _parse_request(options, settings)
    except _LoginRequired:
        return jsonify({"error": "Sign in to use this endpoint."}), 401
    except _QuotaExceeded as exc:
        return jsonify({"error": str(exc)}), 429
    except (BadRequest, ExtractionError) as exc:
        _record(options, admin.current_user(), None, started, "error", str(exc))
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Parsing failed")
        _record(options, admin.current_user(), None, started, "error", str(exc))
        return jsonify({"error": f"Could not process this input: {exc}"}), 500

    _record(options, user, result, started, "ok" if result.questions else "empty")
    payload = result.to_dict()
    payload["subject"] = options["subject"]
    payload["topic"] = options["topic"]
    payload["allowed"] = settings.describe()
    if settings.question_bank():
        # Order matters. Annotate against what the bank held *before* this run, otherwise
        # recording below would make every question flag itself as a repeat of itself.
        payload["repeats"] = quizify.bank.annotate(
            payload, user_id=user["id"] if user else None)
        try:
            payload["banked"] = quizify.bank.record(
                [q.to_dict() for q in result.questions],
                user_id=user["id"] if user else None,
                subject=options["subject"], topic=options["topic"],
                source_name=result.source_name, source_format=result.source_format,
                engine=result.engine,
            )
        except Exception:
            log.exception("Could not update the question bank")
    else:
        payload["repeats"] = 0
    # The preview shows this, so a slow run explains itself instead of just feeling slow.
    payload["duration_ms"] = int((time.time() - started) * 1000)
    log.info("Parsed %s question(s) from %s via %s in %sms",
             len(result.questions), result.source_name or "pasted text",
             result.engine, payload["duration_ms"])
    return jsonify(payload), 200


@app.route("/api/export", methods=["POST"])
def api_export():
    """Render an already-parsed result as a download.

    This is the second half of the preview flow: /api/parse hands the questions to
    the browser, the user reviews them, and they come back here to be exported. The
    source is never parsed twice, so the download costs no extra AI call, no extra
    wait, and no second entry in the conversion history.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400

    settings = _settings()
    options = _read_form(data, settings)
    try:
        user = _gate(check_quota=False)
    except _LoginRequired:
        return jsonify({"error": "Sign in to use this endpoint."}), 401

    result = quizify.ParseResult.from_dict(data)
    if not result.questions:
        return jsonify({"error": "There are no questions to export."}), 400

    # Downloading is the point at which the user is satisfied with their edits, so it is
    # also when their corrections are worth keeping. Strictly opt-in, and never allowed
    # to interfere with the download it rides along with.
    if data.get("learn") and settings.learning():
        try:
            stored = quizify.learning.record(
                data.get("corrections") or [],
                user_id=user["id"] if user else None,
                subject=options["subject"], topic=options["topic"],
                source_format=result.source_format, engine=result.engine,
            )
            if stored:
                log.info("Stored %s correction(s) from %s", stored,
                         result.source_name or "pasted text")
        except Exception:
            log.exception("Could not store corrections")

    try:
        buffer, mimetype, filename = quizify.export(
            result, options["format"], options["subject"], options["topic"],
            max_options=options["max_options"], basename=_download_basename(result),
            answer_style=options["answer_style"],
        )
    except Exception as exc:
        log.exception("Export failed")
        return jsonify({"error": f"Could not build the file: {exc}"}), 500

    # The preview already banked these and counted the use, so this pass only refreshes
    # the stored copy with whatever the user edited. Counting again here would charge a
    # single document two uses just for being previewed before it was downloaded.
    if settings.question_bank():
        try:
            quizify.bank.record(
                [q.to_dict() for q in result.questions],
                user_id=user["id"] if user else None,
                subject=options["subject"], topic=options["topic"],
                source_name=result.source_name, source_format=result.source_format,
                engine=result.engine, count_use=False,
            )
        except Exception:
            log.exception("Could not update the question bank")

    return send_file(buffer, as_attachment=True, download_name=filename, mimetype=mimetype)


@app.route("/api/health", methods=["GET"])
def api_health():
    from quizify import extractors as ex

    settings = _settings()

    # Only advertise what this install can actually read.
    usable = {e for e in SUPPORTED_EXTS if e not in ex.IMAGE_EXTS or ex.OCR_OK}
    if not ex.PDF_OK:
        usable.discard(".pdf")

    return jsonify({
        "status": "ok",
        "supported_formats": sorted(e.lstrip(".") for e in usable),
        "ai_backend": ai.backend_name(),
        "ai_chain": [p.name for p in ai.available_providers()],
        "ai_providers": ai.describe_providers(),
        "readers": {
            "docx": ex.DOCX_OK, "pdf": ex.PDF_OK, "html": ex.HTML_OK,
            "rtf": ex.RTF_OK, "xlsx": ex.XLSX_OK, "ocr": ex.OCR_OK,
        },
        # Reported as they apply to whoever is asking, so the converter shows this
        # account the features it actually has rather than the install's defaults.
        "learning": {
            "enabled": settings.learning() and quizify.learning.enabled(),
            "corrections": quizify.learning.count(),
        },
        "question_bank": {
            "enabled": settings.question_bank() and quizify.bank.enabled(),
            # This caller's own bank, not the install's.
            **quizify.bank.stats(user_id=(admin.current_user() or {}).get("id")),
        },
        "allowed": settings.describe(),
        "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
    }), 200


@app.route("/api/providers", methods=["GET"])
def api_providers():
    """Which AI providers exist, which are configured, and where to get a key."""
    return jsonify({
        "active": ai.backend_name(),
        "chain": [p.name for p in ai.available_providers()],
        "providers": ai.describe_providers(),
    }), 200


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Answer questions about how to use Quizify."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400

    system_prompt = (
        "You are Quizify's assistant. Quizify converts quiz documents into Excel, CSV, or JSON.\n"
        "It reads .docx, .pdf, .txt, .md, .csv, .tsv, .xlsx, .json, .html, .rtf, and pasted text "
        "(images need OCR to be installed).\n"
        "It recognises MCQs, multi-select, True/False, fill-in-the-blank, matching, "
        "assertion-reason, ordering, numeric, and short-answer questions, and picks up answers from "
        "'Answer: B' lines, answer-key sections, answer grids like '1-B, 2-D', bold or highlighted "
        "options, and * or check marks.\n"
        "To use it: upload a file or paste text, fill in Subject and Topic, choose the output "
        "format, then click Process & Download. Rows needing a human check are tinted in the "
        "Excel output, and a Summary sheet reports the counts.\n"
        "Answer in 2-4 sentences. If you are unsure, say so."
    )

    try:
        reply, provider = ai.chat(message, system_prompt,
                                  provider=(data.get("provider") or None))
        return jsonify({"reply": reply, "provider": provider}), 200
    except ai.AIUnavailable as exc:
        return jsonify({
            "error": str(exc),
            "fallback": True,
            "message": message,
        }), 503
    except Exception as exc:
        log.warning("Chat request failed: %s", exc)
        return jsonify({"error": str(exc), "fallback": True, "message": message}), 502


# --------------------------------------------------------------------------------------
# Online exams: assign a parsed quiz, take it via a share link, auto-grade
# --------------------------------------------------------------------------------------

@app.route("/api/exams", methods=["POST"])
def api_create_exam():
    """Assign a previewed quiz as an online exam. Returns the share link.

    Reuses the preview payload shape (same questions the browser already holds), so a
    quiz can be turned into an exam without re-parsing the source.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return jsonify({"error": "There are no questions to assign."}), 400

    user = admin.current_user()
    settings = {
        "time_limit_min": data.get("time_limit_min") or 0,
        "shuffle_q": bool(data.get("shuffle_q")),
        "shuffle_opts": bool(data.get("shuffle_opts")),
        "reveal_answers": data.get("reveal_answers", True),
        "allow_pdf": data.get("allow_pdf", True),
        "available_from": data.get("available_from") or "",
        "available_until": data.get("available_until") or "",
        "instructions": data.get("instructions") or "",
        "proctored": bool(data.get("proctored")),
        "proctor_interval": data.get("proctor_interval") or 20,
    }
    try:
        exam = exams.create_exam(
            title=str(data.get("title") or "").strip(),
            subject=str(data.get("subject") or "").strip(),
            topic=str(data.get("topic") or "").strip(),
            questions=questions,
            settings=settings,
            created_by=user["id"] if user else None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Could not create exam")
        return jsonify({"error": f"Could not assign the exam: {exc}"}), 500

    share_url = url_for("take_exam", token=exam["token"], _external=True)
    log.info("Assigned exam %s (%s questions) as %s", exam["id"],
             exam["question_count"], exam["token"])
    return jsonify({
        "token": exam["token"],
        "share_url": share_url,
        "question_count": exam["question_count"],
        "total_points": exam["total_points"],
    }), 201


@app.route("/exam/<token>", methods=["GET"])
def take_exam(token: str):
    """Public exam-taking page. Shows questions without the answer key."""
    exam = exams.get_exam(token)
    if exam is None:
        return render_template("exam_closed.html", reason="not_found"), 404

    avail = exams.availability(exam)
    if not avail["open"]:
        return render_template("exam_closed.html", reason=avail["reason"],
                               exam=exam, avail=avail), 403

    import random
    questions = [exams.public_question(q) for q in exam["questions"]]
    if exam["shuffle_q"]:
        random.shuffle(questions)
    if exam["shuffle_opts"]:
        for q in questions:
            random.shuffle(q["options"])

    return render_template("exam_take.html", exam=exam, questions=questions)


@app.route("/api/exam/<token>/submit", methods=["POST"])
def api_submit_exam(token: str):
    """Grade a submission and return the result (plus a link to the result page)."""
    exam = exams.get_exam(token)
    if exam is None:
        return jsonify({"error": "This exam no longer exists."}), 404
    avail = exams.availability(exam)
    if not avail["open"]:
        msg = {"closed": "This exam is closed.",
               "not_yet": "This exam has not opened yet.",
               "ended": "This exam's deadline has passed."}.get(avail["reason"], "This exam is closed.")
        return jsonify({"error": msg}), 403

    # A signed-in student may only submit exams assigned to them; the attempt is tied
    # to their account (and their name/email come from the enrolment, not the payload).
    student = student_portal.current_student()
    if student is not None and not exams.is_assigned(exam["id"], student["id"]):
        return jsonify({"error": "This exam is not assigned to you."}), 403

    data = request.get_json(silent=True) or {}
    answers = data.get("answers")
    if not isinstance(answers, dict):
        answers = {}

    # A proctored attempt was created at start; finalise that row so its snapshots stay.
    attempt_id = data.get("attempt_id")
    try:
        attempt_id = int(attempt_id) if attempt_id else None
    except (TypeError, ValueError):
        attempt_id = None
    if attempt_id is not None:
        existing = exams.get_attempt(attempt_id)
        if existing is None or existing["exam_id"] != exam["id"]:
            attempt_id = None

    try:
        result = exams.grade_attempt(
            exam, answers,
            taker_name=(student["name"] if student else str(data.get("taker_name") or "").strip()),
            taker_email=(student["email"] if student else str(data.get("taker_email") or "").strip()),
            duration_sec=data.get("duration_sec") or 0,
            ip=admin.auth.client_ip(),
            attempt_id=attempt_id,
            user_id=(student["id"] if student else None),
        )
    except Exception as exc:
        log.exception("Could not grade exam attempt")
        return jsonify({"error": f"Could not grade your answers: {exc}"}), 500

    result["result_url"] = url_for(
        "exam_result", token=token, attempt=result["attempt_id"], _external=True)
    return jsonify(result), 200


@app.route("/api/exam/<token>/start", methods=["POST"])
def api_start_exam(token: str):
    """Open an in-progress attempt for a proctored exam so snapshots have a home."""
    exam = exams.get_exam(token)
    if exam is None:
        return jsonify({"error": "This exam no longer exists."}), 404
    if not exams.availability(exam)["open"]:
        return jsonify({"error": "This exam is not open."}), 403

    data = request.get_json(silent=True) or {}
    # If a student is signed in, the attempt is tied to their account and they must
    # actually be assigned this exam.
    student = student_portal.current_student()
    if student is not None and not exams.is_assigned(exam["id"], student["id"]):
        return jsonify({"error": "This exam is not assigned to you."}), 403

    attempt_id = exams.start_attempt(
        exam["id"],
        taker_name=(student["name"] if student else str(data.get("taker_name") or "").strip()),
        taker_email=(student["email"] if student else str(data.get("taker_email") or "").strip()),
        ip=admin.auth.client_ip(),
        user_id=(student["id"] if student else None),
    )
    return jsonify({"attempt_id": attempt_id,
                    "interval": exam["proctor_interval"]}), 201


@app.route("/api/exam/<token>/snapshot", methods=["POST"])
def api_exam_snapshot(token: str):
    """Receive one webcam frame for a proctored attempt (data URI), stored durably."""
    exam = exams.get_exam(token)
    if exam is None or not exam["proctored"]:
        return jsonify({"error": "Not a proctored exam."}), 404

    data = request.get_json(silent=True) or {}
    try:
        attempt_id = int(data.get("attempt_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing attempt."}), 400
    att = exams.get_attempt(attempt_id)
    if att is None or att["exam_id"] != exam["id"]:
        return jsonify({"error": "Unknown attempt."}), 404

    ok = exams.save_snapshot(attempt_id, exam["id"], data.get("image") or "")
    return (jsonify({"saved": True}), 200) if ok else (jsonify({"saved": False}), 200)


@app.route("/api/exam/<token>/live-frame", methods=["POST"])
def api_exam_live_frame(token: str):
    """Receive a high-rate live frame (kept in memory only) for smooth admin viewing.

    Separate from /snapshot: this fires several times per second, so it is NOT written
    to the database — only the latest frame per attempt is held in memory.
    """
    exam = exams.get_exam(token)
    if exam is None or not exam["proctored"]:
        return jsonify({"error": "Not a proctored exam."}), 404
    data = request.get_json(silent=True) or {}
    try:
        attempt_id = int(data.get("attempt_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing attempt."}), 400
    # Lightweight: don't hit the DB on every frame; the attempt was validated at start.
    ok = exams.put_live_frame(attempt_id, data.get("image") or "")
    return jsonify({"ok": ok}), 200


if WEBSOCKETS_OK:
    @sock.route("/ws/proctor/<int:attempt_id>/<role>")
    def ws_proctor(ws, attempt_id: int, role: str):
        """WebRTC signalling relay between a learner's camera and the admin viewer.

        Only SDP offer/answer and ICE candidates pass through here — the video stream
        is peer-to-peer and never touches the server. The ``camera`` role is a learner
        with a live attempt; the ``viewer`` role must be a signed-in admin/editor.
        """
        if role not in ("camera", "viewer"):
            return
        attempt = exams.get_attempt(attempt_id)
        if attempt is None:
            return
        # Viewer must be an authenticated staff member allowed to see this attempt.
        if role == "viewer":
            user = admin.current_user()
            if user is None:
                return
            exam = exams.get_exam_by_id(attempt["exam_id"])
            if exam and not admin.auth.has_role("admin", user) \
                    and exam.get("created_by") not in (None, user["id"]):
                return

        key = str(attempt_id)
        signaling.join(key, role, ws)
        # Let the other side know a peer arrived, so it can (re)start negotiation.
        signaling.notify(key, role, "peer-joined")
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                signaling.relay(key, role, msg)
        except Exception:
            pass
        finally:
            signaling.leave(key, role, ws)
            signaling.notify(key, role, "peer-left")


@app.route("/exam/<token>/result/<int:attempt>", methods=["GET"])
def exam_result(token: str, attempt: int):
    """Result page for one attempt: score plus (optionally) the answer breakdown."""
    exam = exams.get_exam(token)
    if exam is None:
        return render_template("exam_closed.html", reason="not_found"), 404
    att = exams.get_attempt(attempt)
    if att is None or att["exam_id"] != exam["id"]:
        return render_template("exam_closed.html", reason="not_found"), 404

    # A signed-in student may only see their own result — never another taker's.
    student = student_portal.current_student()
    if student is not None and att.get("user_id") != student["id"]:
        return redirect(url_for("student.portal"))

    return render_template("exam_result.html", exam=exam, attempt=att,
                           reveal=bool(exam["reveal_answers"]))


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_):
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    message = f"That file is too large. The limit is {limit} MB."
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 413
    return _error_page(message, 413)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
