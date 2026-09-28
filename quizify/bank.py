"""The question bank: a record of every question this install has produced.

Two jobs:

    lookup()   before a download — has this question been used before, and when?
    record()   on download — remember the questions, or bump the ones already known.

Questions are keyed by :func:`quizify.learning.fingerprint`, the same normalised stem
the deduplicator uses, so a question recognises itself across documents that number it
differently or reorder its options.

The bank is deliberately separate from :mod:`quizify.learning`. Learning stores only the
handful of questions a user corrected and opted in to share; the bank stores what was
downloaded, so the owner of the install can see reuse and export their own library.
``QUIZ_QUESTION_BANK`` turns it off entirely.

Every entry point degrades to a no-op without a database, so parsing never depends on it.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .learning import fingerprint
from .models import Option, ParseResult, Question, QuestionType

log = logging.getLogger("quizify")

# Beyond this a single download is doing something unusual; cap the work per request.
MAX_PER_DOWNLOAD = 2_000


def enabled() -> bool:
    return config.get_bool("QUIZ_QUESTION_BANK", True)


def _db():
    """The admin database module, or None when this install has no database."""
    try:
        from .admin import db
        return db
    except Exception:  # pragma: no cover - admin package unavailable
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# --------------------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------------------

def _owner_sql(user_id: Optional[int], everyone: bool, alias: str = "u"):
    """Restrict a query to one account's uses.

    ``questions`` holds one row per distinct question no matter who banked it, so
    ownership lives entirely in ``question_uses``. Every per-account view therefore has
    to be derived from that table — reading subject, source or counts off the questions
    row would show whoever happened to store it last.

    ``IS`` rather than ``=`` so an anonymous visitor (NULL) matches other anonymous
    uses and nothing else; ``= NULL`` would silently match no rows.
    """
    if everyone:
        return "", []
    return f" AND {alias}.user_id IS ?", [user_id]


def lookup(questions: Sequence[Dict[str, Any]], user_id: Optional[int] = None,
           everyone: bool = False, history: int = 3) -> Dict[str, Dict[str, Any]]:
    """Find which of ``questions`` this account has banked before.

    Returns fingerprint -> ``{times_used, first_used_at, last_used_at, uses}`` counted
    over that account's own uses only. Questions somebody else banked are absent, which
    is what stops a reuse notice revealing another user's file names.
    """
    db = _db()
    if db is None or not enabled() or not questions:
        return {}

    keys = [k for k in dict.fromkeys(fingerprint(_text(q.get("question")))
                                     for q in questions if isinstance(q, dict)) if k]
    if not keys:
        return {}

    owner, owner_args = _owner_sql(user_id, everyone)
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(keys), 400):
        batch = keys[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = db.query(
                f"""SELECT q.id            AS id,
                           q.fingerprint   AS fingerprint,
                           COUNT(u.id)     AS times_used,
                           MIN(u.created_at) AS first_used_at,
                           MAX(u.created_at) AS last_used_at
                      FROM questions q
                      JOIN question_uses u ON u.question_id = q.id
                     WHERE q.fingerprint IN ({placeholders}){owner}
                     GROUP BY q.id""",
                batch + owner_args)
        except Exception as exc:  # pragma: no cover - table missing on an old database
            log.warning("Could not read the question bank: %s", exc)
            return {}
        for row in rows:
            entry = dict(row)
            entry["uses"] = _recent_uses(db, entry["id"], history, user_id, everyone)
            found[entry["fingerprint"]] = entry
    return found


def _recent_uses(db, question_id: int, limit: int,
                 user_id: Optional[int] = None, everyone: bool = False
                 ) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    owner, owner_args = _owner_sql(user_id, everyone)
    try:
        rows = db.query(
            f"SELECT source_name, subject, topic, created_at FROM question_uses u "
            f"WHERE u.question_id = ?{owner} "
            f"ORDER BY u.created_at DESC, u.id DESC LIMIT ?",
            [question_id] + owner_args + [limit])
        return db.rows_to_dicts(rows)
    except Exception:  # pragma: no cover
        return []


def annotate(payload: Dict[str, Any], user_id: Optional[int] = None) -> int:
    """Tag each question with *this account's* prior use of it, in place.

    Adds a ``seen_before`` object and returns how many were tagged, so the preview can
    warn before anything is downloaded again. Every field comes from the viewer's own
    history, so "you have used this before" is literally true and names only their files.
    """
    questions = payload.get("questions") or []
    known = lookup(questions, user_id=user_id)
    if not known:
        return 0

    repeats = 0
    for question in questions:
        if not isinstance(question, dict):
            continue
        entry = known.get(fingerprint(_text(question.get("question"))))
        if entry is None:
            continue
        uses = entry.get("uses") or []
        latest = uses[0] if uses else {}
        question["seen_before"] = {
            "times_used": entry["times_used"],
            "first_used_at": entry["first_used_at"],
            "last_used_at": entry["last_used_at"],
            "last_source": latest.get("source_name") or "",
            "subject": latest.get("subject") or "",
            "topic": latest.get("topic") or "",
            "uses": uses,
        }
        repeats += 1
    return repeats


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------

def record(questions: Sequence[Dict[str, Any]], *, user_id: Optional[int] = None,
           subject: str = "", topic: str = "", source_name: str = "",
           source_format: str = "", engine: str = "",
           count_use: bool = True) -> Dict[str, int]:
    """Add ``questions`` to the bank, bumping any that are already there.

    ``count_use`` controls whether a question already in the bank is treated as being
    used again. Previewing a document banks its questions and counts the use; the
    download that follows passes ``count_use=False`` so it refreshes the stored copy
    with any edits without charging the same document a second use. A question that is
    somehow still absent is inserted and counted either way, so nothing is ever lost.

    Returns ``{"added": n, "repeated": n, "refreshed": n}``. Never raises: a bank
    problem must not cost the user their download.
    """
    db = _db()
    if db is None or not enabled() or not questions:
        return {"added": 0, "repeated": 0, "refreshed": 0}

    added = repeated = refreshed = 0
    for raw in list(questions)[:MAX_PER_DOWNLOAD]:
        if not isinstance(raw, dict):
            continue
        stem = _text(raw.get("question")).strip()
        key = fingerprint(stem)
        if not key:
            continue

        options = [o for o in (raw.get("options") or []) if isinstance(o, dict)]
        labels = [_text(v) for v in (raw.get("answer_labels") or []) if _text(v).strip()]
        try:
            confidence = float(raw.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        try:
            existing = db.one("SELECT id FROM questions WHERE fingerprint = ?", (key,))
            if existing is None:
                question_id = db.execute(
                    """
                    INSERT INTO questions
                        (fingerprint, question, qtype, options_json, answer,
                         answer_labels, answer_text, explanation, subject, topic,
                         source_name, source_format, engine, confidence, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key, stem[:2000], _text(raw.get("type"))[:40],
                     json.dumps(options, ensure_ascii=False)[:20_000],
                     _text(raw.get("answer"))[:200], ",".join(labels)[:200],
                     _text(raw.get("answer_text"))[:500],
                     _text(raw.get("explanation"))[:2000],
                     subject[:120], topic[:120], source_name[:255],
                     source_format[:32], engine[:40], confidence, user_id),
                )
                added += 1
            else:
                question_id = existing["id"]
                # Keep the newest reading of the question: a later run may have a better
                # answer, and the count plus the uses table preserve the history anyway.
                # The counter and timestamp only move when this genuinely is a new use.
                usage_sql = (", times_used = times_used + 1, last_used_at = datetime('now')"
                             if count_use else "")
                db.execute(
                    f"""
                    UPDATE questions SET
                        question = ?, qtype = ?, options_json = ?, answer = ?,
                        answer_labels = ?, answer_text = ?, explanation = ?,
                        subject = ?, topic = ?, source_name = ?, source_format = ?,
                        engine = ?, confidence = ?{usage_sql}
                    WHERE id = ?
                    """,
                    (stem[:2000], _text(raw.get("type"))[:40],
                     json.dumps(options, ensure_ascii=False)[:20_000],
                     _text(raw.get("answer"))[:200], ",".join(labels)[:200],
                     _text(raw.get("answer_text"))[:500],
                     _text(raw.get("explanation"))[:2000],
                     subject[:120], topic[:120], source_name[:255],
                     source_format[:32], engine[:40], confidence, question_id),
                )
                if count_use:
                    repeated += 1
                else:
                    refreshed += 1
                    continue  # no new use, so no row in the history

            db.execute(
                "INSERT INTO question_uses (question_id, user_id, source_name, subject, topic) "
                "VALUES (?, ?, ?, ?, ?)",
                (question_id, user_id, source_name[:255], subject[:120], topic[:120]),
            )
        except Exception as exc:
            log.warning("Could not bank a question: %s", exc)
    return {"added": added, "repeated": repeated, "refreshed": refreshed}


# --------------------------------------------------------------------------------------
# Browsing and export
# --------------------------------------------------------------------------------------

def _filter_sql(subject: str, topic: str, term: str, repeats_only: bool,
                user_id: Optional[int], everyone: bool):
    """The WHERE/HAVING clauses shared by search, count, and clear.

    Subject and topic match against the *use history* rather than the question's own
    columns, and against this account's uses only. A question reused under a different
    topic would otherwise disappear from the topic it was originally filed under, and
    a question somebody else filed under "Physics" would surface in your Physics set.
    """
    where = "1=1"
    params: List[Any] = []
    for column, value in (("subject", subject), ("topic", topic)):
        if value:
            scope, scope_args = _owner_sql(user_id, everyone, alias="f")
            where += (f" AND EXISTS (SELECT 1 FROM question_uses f "
                      f"WHERE f.question_id = q.id AND f.{column} = ?{scope})")
            params.append(value)
            params.extend(scope_args)
    if term:
        where += " AND q.question LIKE ?"
        params.append(f"%{term}%")
    # Counted over the joined rows, so "reused" means reused *by this account*.
    having = " HAVING COUNT(u.id) > 1" if repeats_only else ""
    return where, having, params


# The question's own text is shared storage; everything describing *when and how it was
# used* is taken from the viewer's own history rows.
_SEARCH_COLUMNS = """
    q.id, q.question, q.qtype, q.options_json, q.answer, q.answer_labels,
    q.answer_text, q.explanation, q.confidence, q.engine,
    COUNT(u.id)       AS times_used,
    MAX(u.created_at) AS last_used_at,
    MIN(u.created_at) AS first_used_at
"""


def _latest(column: str, owner: str) -> str:
    return (f"(SELECT {column} FROM question_uses n "
            f"WHERE n.question_id = q.id{owner.replace('u.', 'n.')} "
            f"ORDER BY n.created_at DESC, n.id DESC LIMIT 1) AS {column}")


def search(subject: str = "", topic: str = "", term: str = "",
           limit: int = 100, offset: int = 0, repeats_only: bool = False,
           user_id: Optional[int] = None, everyone: bool = False
           ) -> List[Dict[str, Any]]:
    """Banked questions this account has used, most recently used first."""
    db = _db()
    if db is None:
        return []
    owner, owner_args = _owner_sql(user_id, everyone)
    where, having, params = _filter_sql(subject, topic, term, repeats_only,
                                        user_id, everyone)
    latest_args = [] if everyone else [user_id] * 3
    try:
        rows = db.query(
            f"""SELECT {_SEARCH_COLUMNS},
                       {_latest('subject', owner)},
                       {_latest('topic', owner)},
                       {_latest('source_name', owner)}
                  FROM questions q
                  JOIN question_uses u ON u.question_id = q.id
                 WHERE {where}{owner}
                 GROUP BY q.id{having}
                 ORDER BY MAX(u.created_at) DESC, q.id DESC
                 LIMIT ? OFFSET ?""",
            latest_args + params + owner_args
            + [max(1, min(int(limit), 5000)), max(0, int(offset))])
        return db.rows_to_dicts(rows)
    except Exception as exc:  # pragma: no cover
        log.warning("Question bank search failed: %s", exc)
        return []


def count(subject: str = "", topic: str = "", term: str = "",
          repeats_only: bool = False, user_id: Optional[int] = None,
          everyone: bool = False) -> int:
    db = _db()
    if db is None:
        return 0
    owner, owner_args = _owner_sql(user_id, everyone)
    where, having, params = _filter_sql(subject, topic, term, repeats_only,
                                        user_id, everyone)
    try:
        return int(db.scalar(
            f"""SELECT COUNT(*) FROM (
                    SELECT q.id FROM questions q
                      JOIN question_uses u ON u.question_id = q.id
                     WHERE {where}{owner}
                     GROUP BY q.id{having})""",
            params + owner_args))
    except Exception:  # pragma: no cover
        return 0


def get_by_ids(ids: Sequence[int]) -> List[Dict[str, Any]]:
    """Fetch specific banked questions by id, preserving the given order.

    Used when building an exam from a hand-picked selection: the caller sends the
    ids of the rows it wants to reuse.
    """
    db = _db()
    if db is None or not ids:
        return []
    clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not clean:
        return []
    placeholders = ",".join("?" * len(clean))
    try:
        rows = db.rows_to_dicts(db.query(
            f"""SELECT id, question, qtype, options_json, answer, answer_labels,
                       answer_text, explanation, confidence, engine
                  FROM questions WHERE id IN ({placeholders})""",
            clean))
    except Exception as exc:  # pragma: no cover
        log.warning("Question bank get_by_ids failed: %s", exc)
        return []
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in clean if i in by_id]


def subjects(user_id: Optional[int] = None, everyone: bool = False) -> List[Dict[str, Any]]:
    """Distinct subjects with their question counts, for the filter menu."""
    return _facet("subject", user_id=user_id, everyone=everyone)


def topics(subject: str = "", user_id: Optional[int] = None,
           everyone: bool = False) -> List[Dict[str, Any]]:
    """Distinct topics, optionally narrowed to one subject."""
    return _facet("topic", subject, user_id=user_id, everyone=everyone)


def _facet(column: str, subject: str = "", user_id: Optional[int] = None,
           everyone: bool = False) -> List[Dict[str, Any]]:
    """Counts drawn from the use history, so they agree with what the filters return.

    Scoped like everything else: a subject only appears in the menu if this account
    has filed something under it.
    """
    db = _db()
    if db is None:
        return []
    owner, owner_args = _owner_sql(user_id, everyone)
    sql = (f"SELECT u.{column} AS name, COUNT(DISTINCT u.question_id) AS count "
           f"FROM question_uses u WHERE u.{column} != ''{owner}")
    params: List[Any] = list(owner_args)
    if subject and column != "subject":
        scope, scope_args = _owner_sql(user_id, everyone, alias="s")
        sql += (" AND u.question_id IN (SELECT s.question_id FROM question_uses s "
                f"WHERE s.subject = ?{scope})")
        params.append(subject)
        params.extend(scope_args)
    sql += f" GROUP BY u.{column} ORDER BY count DESC, name"
    try:
        return db.rows_to_dicts(db.query(sql, params))
    except Exception:  # pragma: no cover
        return []


def stats(user_id: Optional[int] = None, everyone: bool = False) -> Dict[str, int]:
    """Bank totals for this account (or the whole install when ``everyone``)."""
    db = _db()
    if db is None:
        return {"total": 0, "repeats": 0, "subjects": 0}
    owner, owner_args = _owner_sql(user_id, everyone)
    try:
        return {
            "total": int(db.scalar(
                f"SELECT COUNT(DISTINCT u.question_id) FROM question_uses u "
                f"WHERE 1=1{owner}", owner_args)),
            "repeats": int(db.scalar(
                f"""SELECT COUNT(*) FROM (
                        SELECT u.question_id FROM question_uses u WHERE 1=1{owner}
                         GROUP BY u.question_id HAVING COUNT(*) > 1)""", owner_args)),
            "subjects": int(db.scalar(
                f"SELECT COUNT(DISTINCT u.subject) FROM question_uses u "
                f"WHERE u.subject != ''{owner}", owner_args)),
        }
    except Exception:  # pragma: no cover
        return {"total": 0, "repeats": 0, "subjects": 0}


def to_result(rows: Sequence[Dict[str, Any]]) -> ParseResult:
    """Turn banked rows back into a ParseResult so the normal exporters can render them."""
    questions: List[Question] = []
    for number, row in enumerate(rows, 1):
        try:
            options = json.loads(row.get("options_json") or "[]")
        except (ValueError, TypeError):
            options = []

        labels = [p for p in _text(row.get("answer_labels")).split(",") if p]
        try:
            qtype = QuestionType(_text(row.get("qtype")))
        except ValueError:
            qtype = QuestionType.UNKNOWN

        questions.append(Question(
            text=_text(row.get("question")),
            options=[
                Option(label=_text(o.get("label")) or chr(65 + i),
                       text=_text(o.get("text")),
                       correct=bool(o.get("correct")))
                for i, o in enumerate(options) if isinstance(o, dict)
            ],
            qtype=qtype,
            number=number,
            answer_labels=labels,
            answer_text=_text(row.get("answer_text")) or None,
            explanation=_text(row.get("explanation")) or None,
            confidence=float(row.get("confidence") or 0),
            engine=_text(row.get("engine")) or "rules",
        ))

    return ParseResult(questions=questions, source_name="question bank",
                       source_format="bank", engine="bank")


def _drop_orphans(db) -> None:
    """Remove question rows nobody uses any more.

    The question row is shared storage, so it only goes when the last account that
    referenced it has let it go. Deleting it while another account still has history
    pointing at it would wipe their entry too.
    """
    db.execute("DELETE FROM questions WHERE id NOT IN "
               "(SELECT DISTINCT question_id FROM question_uses)")


def delete(question_id: int, user_id: Optional[int] = None,
           everyone: bool = False) -> bool:
    """Forget one question — this account's history of it, not anybody else's."""
    db = _db()
    if db is None:
        return False
    owner, owner_args = _owner_sql(user_id, everyone)
    try:
        removed = db.execute(
            f"DELETE FROM question_uses WHERE question_id = ? AND rowid IN "
            f"(SELECT u.rowid FROM question_uses u WHERE u.question_id = ?{owner})",
            [question_id, question_id] + owner_args)
        _drop_orphans(db)
        return bool(removed)
    except Exception:  # pragma: no cover
        return False


def clear(subject: str = "", topic: str = "", term: str = "",
          repeats_only: bool = False, user_id: Optional[int] = None,
          everyone: bool = False) -> int:
    """Remove banked questions matching the filters, for this account only.

    Takes the same filters as :func:`search` so a "delete these" action removes exactly
    the set the caller was shown, never more — and never another account's history.
    Returns the number of questions that left this account's bank.
    """
    db = _db()
    if db is None:
        return 0
    owner, owner_args = _owner_sql(user_id, everyone)
    where, having, params = _filter_sql(subject, topic, term, repeats_only,
                                        user_id, everyone)
    try:
        targets = [r["id"] for r in db.query(
            f"""SELECT q.id FROM questions q
                  JOIN question_uses u ON u.question_id = q.id
                 WHERE {where}{owner}
                 GROUP BY q.id{having}""", params + owner_args)]
        if not targets:
            return 0
        placeholders = ",".join("?" for _ in targets)
        db.execute(
            f"DELETE FROM question_uses WHERE rowid IN "
            f"(SELECT u.rowid FROM question_uses u "
            f" WHERE u.question_id IN ({placeholders}){owner})",
            targets + owner_args)
        _drop_orphans(db)
        return len(targets)
    except Exception as exc:  # pragma: no cover
        log.warning("Could not clear the question bank: %s", exc)
        return 0
