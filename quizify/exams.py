"""Online-exam feature: assign a parsed quiz, take it via a share link, auto-grade.

The data model lives in three tables (see ``admin/db.py``): ``exams`` holds a frozen
snapshot of the questions plus its settings, and ``exam_attempts`` holds each taker's
submission and graded breakdown.

Grading is objective-only. MCQ (single), multi-select, True/False, numeric and
fill-in-the-blank are scored against the answer key; anything without a machine-checkable
answer (short answer, matching without a key, etc.) is recorded as *needs review* and
contributes no points to the auto score.
"""

import json
import re
import secrets
import threading
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from .admin import db

# --------------------------------------------------------------------------------------
# Live proctoring buffer
# --------------------------------------------------------------------------------------
# For a smooth "live video" feel the taker streams many frames per second. Persisting
# every one would bloat the database, so the newest frame per attempt is kept in memory
# only and served to the admin at a high refresh rate. Durable snapshots (every
# proctor_interval seconds) still go to exam_snapshots for the record.
_LIVE_LOCK = threading.Lock()
_LIVE_FRAMES: Dict[int, Dict[str, Any]] = {}   # attempt_id -> {image, ts}
_LIVE_TTL = 120                                 # drop a stream considered stale after 2 min


def put_live_frame(attempt_id: int, image: str) -> bool:
    """Store the newest live frame for an attempt in memory (overwrites the previous)."""
    image = str(image or "")
    if not image.startswith("data:image/") or len(image) > _MAX_SNAPSHOT_CHARS:
        return False
    with _LIVE_LOCK:
        _LIVE_FRAMES[attempt_id] = {"image": image, "ts": _time.time()}
        # Opportunistically evict stale streams so the dict cannot grow without bound.
        if len(_LIVE_FRAMES) > 200:
            cutoff = _time.time() - _LIVE_TTL
            for aid in [k for k, v in _LIVE_FRAMES.items() if v["ts"] < cutoff]:
                _LIVE_FRAMES.pop(aid, None)
    return True


def get_live_frame(attempt_id: int) -> Optional[Dict[str, Any]]:
    """The newest in-memory live frame for an attempt, or None if none/expired."""
    with _LIVE_LOCK:
        frame = _LIVE_FRAMES.get(attempt_id)
        if frame is None:
            return None
        age = _time.time() - frame["ts"]
        return {"image": frame["image"], "age": round(age, 1),
                "live": age <= 10}   # "live" if a frame arrived in the last 10s


def clear_live_frame(attempt_id: int) -> None:
    with _LIVE_LOCK:
        _LIVE_FRAMES.pop(attempt_id, None)

# Question types we can score without a human. Everything else is display-only and
# flagged for manual review on the results page.
_AUTO_GRADED = {"MCQ", "Multi-Select", "True/False", "Numeric", "Fill in the Blank"}

_TOKEN_BYTES = 9          # ~12 url-safe chars; unguessable but short enough to share
_MAX_QUESTIONS = 500


# --------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------

def _norm_text(value: Any) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation for loose compare."""
    s = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return s.strip(" .,:;!?\"'")


def _norm_number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _labels(value: Any) -> List[str]:
    """Coerce an answer-labels value into a clean, upper-cased list like ['A','C']."""
    if isinstance(value, list):
        items = value
    elif value is None:
        items = []
    else:
        # A submitted single label, or a comma string like "A,C".
        items = re.split(r"[,\s]+", str(value))
    return sorted({str(x).strip().upper() for x in items if str(x).strip()})


# --------------------------------------------------------------------------------------
# Question shaping (what the taker sees vs. the answer key we keep server-side)
# --------------------------------------------------------------------------------------

def _clean_question(raw: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Freeze one question into the exam snapshot, keeping the answer key server-side."""
    options = []
    for o in raw.get("options") or []:
        if not isinstance(o, dict):
            continue
        text = str(o.get("text") or "").strip()
        if not text:
            continue
        options.append({"label": str(o.get("label") or "").strip().upper(), "text": text})

    qtype = str(raw.get("type") or "Unknown")
    # Accept "marks" (from the parser/preview) or "points" (from the exam editor).
    points = raw.get("marks")
    if points in (None, ""):
        points = raw.get("points")
    try:
        points = float(points) if points not in (None, "") else 1.0
    except (TypeError, ValueError):
        points = 1.0
    if points <= 0:
        points = 1.0

    return {
        "id": index,
        "number": raw.get("number") or index + 1,
        "type": qtype,
        "question": str(raw.get("question") or "").strip(),
        "options": options,
        "answer_labels": _labels(raw.get("answer_labels")),
        "answer_text": str(raw.get("answer_text") or raw.get("answer") or "").strip(),
        "explanation": str(raw.get("explanation") or "").strip(),
        "points": points,
        "auto": qtype in _AUTO_GRADED and bool(
            _labels(raw.get("answer_labels")) or str(raw.get("answer_text") or raw.get("answer") or "").strip()
        ),
    }


def public_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """The version of a question sent to a taker — never includes the answer."""
    return {
        "id": q["id"],
        "number": q["number"],
        "type": q["type"],
        "question": q["question"],
        "options": [{"label": o["label"], "text": o["text"]} for o in q["options"]],
        "points": q["points"],
    }


# --------------------------------------------------------------------------------------
# Create / read
# --------------------------------------------------------------------------------------

def create_exam(*, title: str, subject: str, topic: str,
                questions: List[Dict[str, Any]], settings: Dict[str, Any],
                created_by: Optional[int] = None) -> Dict[str, Any]:
    """Freeze ``questions`` into a new exam and return its row (including the token)."""
    cleaned = [_clean_question(q, i) for i, q in enumerate(questions[:_MAX_QUESTIONS])
               if isinstance(q, dict) and str(q.get("question") or "").strip()]
    if not cleaned:
        raise ValueError("There are no questions to assign.")

    total_points = round(sum(q["points"] for q in cleaned), 2)
    token = secrets.token_urlsafe(_TOKEN_BYTES)

    exam_id = db.execute(
        """INSERT INTO exams
             (token, title, subject, topic, questions_json, question_count, total_points,
              time_limit_min, shuffle_q, shuffle_opts, reveal_answers, allow_pdf,
              available_from, available_until, instructions, proctored, proctor_interval,
              created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token,
            (title or subject or "Untitled Quiz").strip()[:200],
            (subject or "").strip()[:120],
            (topic or "").strip()[:120],
            json.dumps(cleaned, ensure_ascii=False),
            len(cleaned),
            total_points,
            max(0, int(settings.get("time_limit_min") or 0)),
            1 if settings.get("shuffle_q") else 0,
            1 if settings.get("shuffle_opts") else 0,
            0 if settings.get("reveal_answers") is False else 1,
            0 if settings.get("allow_pdf") is False else 1,
            _clean_dt(settings.get("available_from")),
            _clean_dt(settings.get("available_until")),
            str(settings.get("instructions") or "").strip()[:5000],
            1 if settings.get("proctored") else 0,
            max(5, min(int(settings.get("proctor_interval") or 20), 300)),
            created_by,
        ),
    )
    return get_exam_by_id(exam_id)


def update_exam(exam_id: int, *, title: Optional[str] = None, subject: Optional[str] = None,
                topic: Optional[str] = None, settings: Optional[Dict[str, Any]] = None,
                questions: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Reconfigure an existing exam: metadata, schedule, options, and/or questions.

    Only the arguments provided are changed; ``None`` leaves a field untouched. When
    ``questions`` is given the snapshot is rebuilt and point totals recomputed.
    """
    exam = get_exam_by_id(exam_id)
    if exam is None:
        return None

    sets: List[str] = []
    args: List[Any] = []

    def put(col: str, value: Any):
        sets.append(f"{col} = ?")
        args.append(value)

    if title is not None:
        put("title", (title or "Untitled Quiz").strip()[:200])
    if subject is not None:
        put("subject", (subject or "").strip()[:120])
    if topic is not None:
        put("topic", (topic or "").strip()[:120])

    s = settings or {}
    if "time_limit_min" in s:
        put("time_limit_min", max(0, int(s.get("time_limit_min") or 0)))
    if "shuffle_q" in s:
        put("shuffle_q", 1 if s.get("shuffle_q") else 0)
    if "shuffle_opts" in s:
        put("shuffle_opts", 1 if s.get("shuffle_opts") else 0)
    if "reveal_answers" in s:
        put("reveal_answers", 1 if s.get("reveal_answers") else 0)
    if "allow_pdf" in s:
        put("allow_pdf", 1 if s.get("allow_pdf") else 0)
    if "available_from" in s:
        put("available_from", _clean_dt(s.get("available_from")))
    if "available_until" in s:
        put("available_until", _clean_dt(s.get("available_until")))
    if "instructions" in s:
        put("instructions", str(s.get("instructions") or "").strip()[:5000])
    if "proctored" in s:
        put("proctored", 1 if s.get("proctored") else 0)
    if "proctor_interval" in s:
        put("proctor_interval", max(5, min(int(s.get("proctor_interval") or 20), 300)))

    if questions is not None:
        cleaned = [_clean_question(q, i) for i, q in enumerate(questions[:_MAX_QUESTIONS])
                   if isinstance(q, dict) and str(q.get("question") or "").strip()]
        if not cleaned:
            raise ValueError("An exam needs at least one question.")
        put("questions_json", json.dumps(cleaned, ensure_ascii=False))
        put("question_count", len(cleaned))
        put("total_points", round(sum(q["points"] for q in cleaned), 2))

    if sets:
        db.execute(f"UPDATE exams SET {', '.join(sets)} WHERE id = ?", args + [exam_id])
    return get_exam_by_id(exam_id)


def _clean_dt(value: Any) -> str:
    """Keep a datetime-local string ('YYYY-MM-DDTHH:MM') as-is; blank anything else."""
    s = str(value or "").strip()
    return s[:19] if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s) else ""


def availability(exam: Dict[str, Any], now: Optional[str] = None) -> Dict[str, Any]:
    """Whether the exam can be taken right now, given its schedule window.

    Comparison is lexical on 'YYYY-MM-DDTHH:MM' strings, which sort chronologically.
    ``now`` defaults to the current local wall-clock in the same format.
    """
    import datetime
    if now is None:
        now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")
    start = (exam.get("available_from") or "").strip()
    end = (exam.get("available_until") or "").strip()
    if not exam.get("is_open"):
        return {"open": False, "reason": "closed"}
    if start and now < start:
        return {"open": False, "reason": "not_yet", "starts": start}
    if end and now > end:
        return {"open": False, "reason": "ended", "ended": end}
    return {"open": True, "reason": "open", "starts": start, "ends": end}


def _row_to_exam(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    exam = dict(row)
    try:
        exam["questions"] = json.loads(exam.get("questions_json") or "[]")
    except (ValueError, TypeError):
        exam["questions"] = []
    return exam


def get_exam(token: str) -> Optional[Dict[str, Any]]:
    return _row_to_exam(db.one("SELECT * FROM exams WHERE token = ?", (token,)))


def get_exam_by_id(exam_id: int) -> Optional[Dict[str, Any]]:
    return _row_to_exam(db.one("SELECT * FROM exams WHERE id = ?", (exam_id,)))


def list_exams(created_by: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Every exam with a live attempt count, most recent first."""
    sql = (
        "SELECT e.*, "
        "  (SELECT COUNT(*) FROM exam_attempts a WHERE a.exam_id = e.id) AS attempts, "
        "  (SELECT ROUND(AVG(a.percent), 1) FROM exam_attempts a WHERE a.exam_id = e.id) AS avg_percent "
        "FROM exams e "
    )
    params: List[Any] = []
    if created_by is not None:
        sql += "WHERE e.created_by = ? "
        params.append(created_by)
    sql += "ORDER BY e.created_at DESC LIMIT ?"
    params.append(int(limit))
    return db.rows_to_dicts(db.query(sql, params))


def set_open(token: str, is_open: bool) -> bool:
    return db.execute("UPDATE exams SET is_open = ? WHERE token = ?",
                      (1 if is_open else 0, token)) > 0


def delete_exam(exam_id: int) -> bool:
    return db.execute("DELETE FROM exams WHERE id = ?", (exam_id,)) > 0


# --------------------------------------------------------------------------------------
# Student assignments
# --------------------------------------------------------------------------------------

def assign_students(exam_id: int, user_ids: List[int], assigned_by: Optional[int] = None) -> int:
    """Assign an exam to a set of students. Idempotent per (exam, student).

    Returns how many new assignments were created.
    """
    created = 0
    for uid in user_ids:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        rows = db.execute(
            "INSERT OR IGNORE INTO exam_assignments (exam_id, user_id, assigned_by) "
            "VALUES (?, ?, ?)", (exam_id, uid, assigned_by))
        # execute() returns lastrowid for INSERT; a duplicate ignored yields 0 rowcount.
        created += 1 if rows else 0
    return created


def unassign_student(exam_id: int, user_id: int) -> bool:
    return db.execute(
        "DELETE FROM exam_assignments WHERE exam_id = ? AND user_id = ?",
        (exam_id, user_id)) > 0


def assigned_user_ids(exam_id: int) -> List[int]:
    return [r["user_id"] for r in db.query(
        "SELECT user_id FROM exam_assignments WHERE exam_id = ?", (exam_id,))]


def assigned_students(exam_id: int) -> List[Dict[str, Any]]:
    """Students assigned to an exam, with their latest attempt score if any."""
    return db.rows_to_dicts(db.query(
        """SELECT u.id, u.name, u.email, u.batch, u.roll_no,
                  (SELECT a.percent FROM exam_attempts a
                    WHERE a.exam_id = ea.exam_id AND a.user_id = u.id
                    ORDER BY a.created_at DESC LIMIT 1) AS last_percent,
                  (SELECT COUNT(*) FROM exam_attempts a
                    WHERE a.exam_id = ea.exam_id AND a.user_id = u.id) AS attempts
             FROM exam_assignments ea
             JOIN users u ON u.id = ea.user_id
            WHERE ea.exam_id = ?
            ORDER BY u.batch, u.roll_no, u.name""", (exam_id,)))


def is_assigned(exam_id: int, user_id: int) -> bool:
    return db.one(
        "SELECT 1 FROM exam_assignments WHERE exam_id = ? AND user_id = ?",
        (exam_id, user_id)) is not None


def exams_for_student(user_id: int) -> List[Dict[str, Any]]:
    """Every exam assigned to a student, with schedule + their attempt status."""
    return db.rows_to_dicts(db.query(
        """SELECT e.id, e.token, e.title, e.subject, e.topic, e.question_count,
                  e.total_points, e.time_limit_min, e.available_from, e.available_until,
                  e.is_open, e.proctored,
                  (SELECT a.id FROM exam_attempts a
                    WHERE a.exam_id = e.id AND a.user_id = ? AND a.graded_count >= 0
                      AND a.answers_json != '{}'
                    ORDER BY a.created_at DESC LIMIT 1) AS attempt_id,
                  (SELECT a.percent FROM exam_attempts a
                    WHERE a.exam_id = e.id AND a.user_id = ? AND a.answers_json != '{}'
                    ORDER BY a.created_at DESC LIMIT 1) AS attempt_percent,
                  (SELECT a.created_at FROM exam_attempts a
                    WHERE a.exam_id = e.id AND a.user_id = ? AND a.answers_json != '{}'
                    ORDER BY a.created_at DESC LIMIT 1) AS attempt_at
             FROM exam_assignments ea
             JOIN exams e ON e.id = ea.exam_id
            WHERE ea.user_id = ?
            ORDER BY e.created_at DESC""", (user_id, user_id, user_id, user_id)))


def student_attempt(exam_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """A student's most recent *submitted* attempt for an exam, if any."""
    row = db.one(
        """SELECT * FROM exam_attempts
            WHERE exam_id = ? AND user_id = ? AND answers_json != '{}'
            ORDER BY created_at DESC LIMIT 1""", (exam_id, user_id))
    return dict(row) if row else None


# --------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------

def _grade_one(q: Dict[str, Any], submitted: Any) -> Dict[str, Any]:
    """Grade a single question. Returns a per-question result dict."""
    qtype = q["type"]
    points = q["points"]
    result: Dict[str, Any] = {
        "id": q["id"],
        "number": q["number"],
        "type": qtype,
        "question": q["question"],
        "points": points,
        "your_answer": "",
        "correct_answer": _answer_display(q),
        "explanation": q.get("explanation", ""),
        "auto": q.get("auto", False),
        "correct": False,
        "awarded": 0.0,
        "needs_review": False,
    }

    if not q.get("auto"):
        # No machine-checkable key — record the response and flag for review.
        result["your_answer"] = _stringify(submitted)
        result["needs_review"] = True
        return result

    if qtype in ("MCQ", "Multi-Select", "True/False"):
        chosen = _labels(submitted)
        key = _labels(q.get("answer_labels"))
        # For True/False the answer may be stored as text, not a label.
        if not key and qtype == "True/False":
            return _grade_text(q, submitted, result)
        result["your_answer"] = ", ".join(chosen) if chosen else "—"
        result["correct"] = bool(chosen) and chosen == key
    elif qtype == "Numeric":
        want = _norm_number(q.get("answer_text"))
        got = _norm_number(submitted)
        result["your_answer"] = _stringify(submitted)
        result["correct"] = want is not None and got is not None and abs(want - got) < 1e-9
    else:  # Fill in the Blank and any other text-keyed auto type
        return _grade_text(q, submitted, result)

    result["awarded"] = points if result["correct"] else 0.0
    return result


def _grade_text(q: Dict[str, Any], submitted: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    want = _norm_text(q.get("answer_text"))
    got = _norm_text(submitted)
    result["your_answer"] = _stringify(submitted)
    result["correct"] = bool(want) and got == want
    result["awarded"] = q["points"] if result["correct"] else 0.0
    return result


def _answer_display(q: Dict[str, Any]) -> str:
    if q.get("answer_labels"):
        return ", ".join(q["answer_labels"])
    return q.get("answer_text") or ""


def _stringify(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value or "").strip()


def start_attempt(exam_id: int, *, taker_name: str = "", taker_email: str = "",
                  ip: str = "", user_id: Optional[int] = None) -> int:
    """Create an in-progress attempt row and return its id.

    Used by proctored exams so webcam snapshots have an attempt to attach to while
    the taker is still working. It is finalised (scored) later by ``grade_attempt``
    with this id. ``user_id`` links the attempt to an enrolled student when known.
    """
    return db.execute(
        """INSERT INTO exam_attempts (exam_id, taker_name, taker_email, ip, user_id)
           VALUES (?, ?, ?, ?, ?)""",
        (exam_id, (taker_name or "Anonymous").strip()[:120],
         (taker_email or "").strip()[:200], ip, user_id))


def grade_attempt(exam: Dict[str, Any], answers: Dict[str, Any], *,
                  taker_name: str = "", taker_email: str = "",
                  duration_sec: int = 0, ip: str = "",
                  attempt_id: Optional[int] = None,
                  user_id: Optional[int] = None) -> Dict[str, Any]:
    """Grade a submission against ``exam`` and persist the attempt. Returns the result.

    When ``attempt_id`` is given (a proctored attempt started earlier) the existing
    row is updated in place, keeping any snapshots linked to it; otherwise a new row
    is inserted.
    """
    details: List[Dict[str, Any]] = []
    score = 0.0
    correct_count = 0
    graded_count = 0
    needs_review = False

    for q in exam["questions"]:
        submitted = answers.get(str(q["id"]), answers.get(q["id"]))
        r = _grade_one(q, submitted)
        details.append(r)
        if r["needs_review"]:
            needs_review = True
        if r["auto"]:
            graded_count += 1
            score += r["awarded"]
            if r["correct"]:
                correct_count += 1

    total_points = round(sum(q["points"] for q in exam["questions"] if q.get("auto")), 2)
    percent = round((score / total_points) * 100, 1) if total_points > 0 else 0.0

    if attempt_id:
        # Finalise the in-progress (proctored) attempt so its snapshots stay linked.
        db.execute(
            """UPDATE exam_attempts SET
                 taker_name = ?, taker_email = ?, answers_json = ?, detail_json = ?,
                 score = ?, total_points = ?, correct_count = ?, graded_count = ?,
                 percent = ?, needs_review = ?, duration_sec = ?
               WHERE id = ?""",
            (
                (taker_name or "Anonymous").strip()[:120],
                (taker_email or "").strip()[:200],
                json.dumps(answers, ensure_ascii=False),
                json.dumps(details, ensure_ascii=False),
                round(score, 2), total_points, correct_count, graded_count,
                percent, 1 if needs_review else 0, max(0, int(duration_sec or 0)),
                attempt_id,
            ),
        )
    else:
        attempt_id = db.execute(
            """INSERT INTO exam_attempts
                 (exam_id, taker_name, taker_email, answers_json, detail_json, score,
                  total_points, correct_count, graded_count, percent, needs_review,
                  duration_sec, ip, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exam["id"],
                (taker_name or "Anonymous").strip()[:120],
                (taker_email or "").strip()[:200],
                json.dumps(answers, ensure_ascii=False),
                json.dumps(details, ensure_ascii=False),
                round(score, 2),
                total_points,
                correct_count,
                graded_count,
                percent,
                1 if needs_review else 0,
                max(0, int(duration_sec or 0)),
                ip,
                user_id,
            ),
        )

    return {
        "attempt_id": attempt_id,
        "score": round(score, 2),
        "total_points": total_points,
        "correct_count": correct_count,
        "graded_count": graded_count,
        "percent": percent,
        "needs_review": needs_review,
        "details": details,
    }


# --------------------------------------------------------------------------------------
# Attempts
# --------------------------------------------------------------------------------------

def get_attempt(attempt_id: int) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM exam_attempts WHERE id = ?", (attempt_id,))
    if row is None:
        return None
    attempt = dict(row)
    try:
        attempt["details"] = json.loads(attempt.get("detail_json") or "[]")
    except (ValueError, TypeError):
        attempt["details"] = []
    return attempt


def list_attempts(exam_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
    return db.rows_to_dicts(db.query(
        "SELECT id, taker_name, taker_email, score, total_points, correct_count, "
        "graded_count, percent, needs_review, duration_sec, created_at "
        "FROM exam_attempts WHERE exam_id = ? ORDER BY created_at DESC LIMIT ?",
        (exam_id, int(limit)),
    ))


# --------------------------------------------------------------------------------------
# Proctoring snapshots
# --------------------------------------------------------------------------------------

_MAX_SNAPSHOT_CHARS = 400_000   # ~300 KB image; anything larger is rejected
_MAX_SNAPSHOTS_PER_ATTEMPT = 400


def save_snapshot(attempt_id: int, exam_id: int, image: str) -> bool:
    """Store one webcam frame for an attempt. Returns False if rejected.

    Images arrive as ``data:image/...;base64,...`` URIs. A hard size cap and a
    per-attempt count cap keep a misbehaving client from filling the database.
    """
    image = str(image or "")
    if not image.startswith("data:image/") or len(image) > _MAX_SNAPSHOT_CHARS:
        return False
    count = db.scalar("SELECT COUNT(*) FROM exam_snapshots WHERE attempt_id = ?",
                      (attempt_id,), default=0)
    if count >= _MAX_SNAPSHOTS_PER_ATTEMPT:
        return False
    db.execute(
        "INSERT INTO exam_snapshots (attempt_id, exam_id, image) VALUES (?, ?, ?)",
        (attempt_id, exam_id, image))
    return True


def latest_snapshot(attempt_id: int) -> Optional[Dict[str, Any]]:
    row = db.one(
        "SELECT id, image, created_at FROM exam_snapshots "
        "WHERE attempt_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (attempt_id,))
    return dict(row) if row else None


def list_snapshots(attempt_id: int, limit: int = 400) -> List[Dict[str, Any]]:
    return db.rows_to_dicts(db.query(
        "SELECT id, image, created_at FROM exam_snapshots "
        "WHERE attempt_id = ? ORDER BY created_at ASC, id ASC LIMIT ?",
        (attempt_id, int(limit))))


def snapshot_count(attempt_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM exam_snapshots WHERE attempt_id = ?",
                         (attempt_id,), default=0))


def exam_stats(exam_id: int) -> Dict[str, Any]:
    row = db.one(
        "SELECT COUNT(*) AS attempts, ROUND(AVG(percent), 1) AS avg_percent, "
        "MAX(percent) AS best_percent, MIN(percent) AS low_percent, "
        "SUM(needs_review) AS review_count "
        "FROM exam_attempts WHERE exam_id = ?", (exam_id,))
    stats = dict(row) if row else {}
    stats["attempts"] = stats.get("attempts") or 0
    stats["avg_percent"] = stats.get("avg_percent") or 0
    stats["best_percent"] = stats.get("best_percent") or 0
    stats["low_percent"] = stats.get("low_percent") or 0
    stats["review_count"] = stats.get("review_count") or 0
    return stats
