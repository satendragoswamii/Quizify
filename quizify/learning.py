"""Accuracy feedback loop built from corrections users make in the preview.

Nothing here trains a model. Fine-tuning is an offline job run at a provider, and it
needs far more data than a single install accumulates. What this module does instead
is reuse corrections directly, which is both cheaper and effective immediately:

    record()    a user fixed a question in the preview -> remember the fixed version
    apply()     a later parse meets that question again -> restore the fixed answer
    examples()  the AI backend is about to run -> show it corrections as worked examples

Storage is opt-in per conversion and holds only questions that were actually edited,
so an install that nobody opts into behaves exactly as it did before.

Every entry point degrades to a no-op when the admin database is absent or the feature
is switched off, so the parsing pipeline never depends on any of this succeeding.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .models import AnswerSource, ParseResult, Question, SOURCE_CONFIDENCE

log = logging.getLogger("quizify")

# How many worked examples to put in front of the AI backend. Enough to demonstrate the
# conventions this install keeps getting wrong, small enough not to crowd the document.
MAX_EXAMPLES = 5
MAX_EXAMPLE_CHARS = 2_000

# Below this, the rule engine's own answer is weak enough that a stored human correction
# should win. At or above it the document itself stated the answer plainly, so it stands.
OVERRIDE_BELOW_CONFIDENCE = 0.6


def enabled() -> bool:
    """Whether the feedback loop may run at all. Admins can switch it off globally."""
    return config.get_bool("QUIZ_LEARNING", True)


def fingerprint(text: str) -> str:
    """Key a question by its stem, ignoring numbering, spacing, and punctuation.

    Matches the normalisation ``deduplicate`` uses, so the same question written
    ``Q1. What is X?`` in one document and ``1) what is x`` in another lands on one key.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:160]


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------

def _db():
    """The admin database module, or None when this install has no database.

    Imported lazily: the admin package pulls in Flask and the HTTP layer, which the
    parsing pipeline has no business requiring just to parse a document.
    """
    try:
        from .admin import db
        return db
    except Exception:  # pragma: no cover - admin package unavailable
        return None


def _changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Which parts of a question the user actually altered."""
    changed = []
    if (before.get("question") or "").strip() != (after.get("question") or "").strip():
        changed.append("question")
    if _option_texts(before) != _option_texts(after):
        changed.append("options")
    if (before.get("answer") or "") != (after.get("answer") or ""):
        changed.append("answer")
    if (before.get("type") or "") != (after.get("type") or ""):
        changed.append("type")
    if (before.get("explanation") or "") != (after.get("explanation") or ""):
        changed.append("explanation")
    return changed


def _option_texts(question: Dict[str, Any]) -> List[str]:
    return [str((o or {}).get("text", "")).strip()
            for o in (question.get("options") or []) if isinstance(o, dict)]


def record(pairs: Sequence[Dict[str, Any]], *, user_id: Optional[int] = None,
           subject: str = "", topic: str = "", source_format: str = "",
           engine: str = "") -> int:
    """Store the corrections in ``pairs`` — each ``{"before": {...}, "after": {...}}``.

    Only genuinely edited questions are kept. Re-correcting the same question replaces
    the earlier entry rather than accumulating duplicates, so the newest human judgement
    is the one that gets reused. Returns how many corrections were stored.
    """
    db = _db()
    if db is None or not enabled() or not pairs:
        return 0

    stored = 0
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        before = pair.get("before") if isinstance(pair.get("before"), dict) else {}
        after = pair.get("after") if isinstance(pair.get("after"), dict) else {}

        stem = str(after.get("question") or "").strip()
        key = fingerprint(stem)
        if not key:
            continue

        fields = _changed_fields(before, after)
        if not fields:
            continue

        try:
            db.execute(
                """
                INSERT INTO corrections
                    (user_id, fingerprint, question, subject, topic, source_format,
                     engine, fields, before_json, after_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    question      = excluded.question,
                    subject       = excluded.subject,
                    topic         = excluded.topic,
                    source_format = excluded.source_format,
                    engine        = excluded.engine,
                    fields        = excluded.fields,
                    before_json   = excluded.before_json,
                    after_json    = excluded.after_json,
                    created_at    = datetime('now')
                """,
                (user_id, key, stem[:500], subject[:120], topic[:120],
                 source_format[:32], engine[:40], ",".join(fields),
                 json.dumps(before, ensure_ascii=False)[:20_000],
                 json.dumps(after, ensure_ascii=False)[:20_000]),
            )
            stored += 1
        except Exception as exc:  # a broken feedback loop must never fail a download
            log.warning("Could not store correction: %s", exc)
    return stored


def lookup(keys: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch stored corrections for the given fingerprints, keyed by fingerprint."""
    db = _db()
    if db is None or not enabled() or not keys:
        return {}

    unique = [k for k in dict.fromkeys(keys) if k]
    found: Dict[str, Dict[str, Any]] = {}
    # Chunked so a large document cannot build an oversized statement.
    for start in range(0, len(unique), 400):
        batch = unique[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = db.query(
                f"SELECT fingerprint, after_json FROM corrections "
                f"WHERE fingerprint IN ({placeholders})", batch)
        except Exception as exc:  # pragma: no cover - table missing on an old database
            log.warning("Could not read corrections: %s", exc)
            return {}
        for row in rows:
            try:
                payload = json.loads(row["after_json"])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                found[row["fingerprint"]] = payload
    return found


def recent(limit: int = 100) -> List[Dict[str, Any]]:
    """Most recently corrected questions, for the admin panel."""
    db = _db()
    if db is None:
        return []
    try:
        rows = db.query(
            "SELECT id, fingerprint, question, subject, topic, source_format, engine, "
            "fields, created_at FROM corrections ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),))
        return db.rows_to_dicts(rows)
    except Exception:  # pragma: no cover
        return []


def count() -> int:
    db = _db()
    if db is None:
        return 0
    try:
        return int(db.scalar("SELECT COUNT(*) FROM corrections"))
    except Exception:  # pragma: no cover
        return 0


def delete(correction_id: int) -> bool:
    db = _db()
    if db is None:
        return False
    try:
        return bool(db.execute("DELETE FROM corrections WHERE id = ?", (correction_id,)))
    except Exception:  # pragma: no cover
        return False


def clear() -> int:
    db = _db()
    if db is None:
        return 0
    try:
        return int(db.execute("DELETE FROM corrections"))
    except Exception:  # pragma: no cover
        return 0


# --------------------------------------------------------------------------------------
# Applying what was learned
# --------------------------------------------------------------------------------------

def _labels_for(stored: Dict[str, Any], question: Question) -> List[str]:
    """Translate a stored answer onto this question's option labels.

    The stored correction may come from a document that ordered its options
    differently, so an answer is matched by option *text* first and only falls back to
    the raw label when the texts give no match.
    """
    stored_options = [str((o or {}).get("text", "")).strip()
                      for o in (stored.get("options") or []) if isinstance(o, dict)]
    correct_texts = [str((o or {}).get("text", "")).strip()
                     for o in (stored.get("options") or [])
                     if isinstance(o, dict) and o.get("correct")]

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    if correct_texts:
        wanted = {norm(t) for t in correct_texts if t}
        matched = [o.label for o in question.options if norm(o.text) in wanted]
        if matched:
            return matched

    labels = [str(v).strip().upper()
              for v in (stored.get("answer_labels") or []) if str(v).strip()]
    valid = {o.label for o in question.options}
    # Only trust bare labels when the option lists actually correspond.
    if labels and stored_options == [o.text.strip() for o in question.options]:
        return [label for label in labels if label in valid]
    if labels and not question.options:
        return labels
    return []


def apply(result: ParseResult) -> int:
    """Fill in answers this parse missed, using corrections a user made before.

    Conservative by design: a confident answer found in the document itself is never
    overwritten. A stored correction is used only where the parser found nothing, or
    where it was unsure enough that a human's earlier verdict is the better bet.

    Returns how many questions were improved.
    """
    if not enabled() or not result.questions:
        return 0

    keys = [fingerprint(q.text) for q in result.questions]
    stored_by_key = lookup(keys)
    if not stored_by_key:
        return 0

    improved = 0
    for question, key in zip(result.questions, keys):
        stored = stored_by_key.get(key)
        if stored is None:
            continue
        if question.answer_display and question.confidence >= OVERRIDE_BELOW_CONFIDENCE:
            continue

        labels = _labels_for(stored, question)
        answer_text = str(stored.get("answer_text") or "").strip()
        if not labels and not answer_text:
            continue
        if question.answer_display == (",".join(labels) or answer_text):
            continue

        question.answer_labels = labels
        question.answer_text = answer_text or None
        question.answer_source = AnswerSource.LEARNED
        question.confidence = max(question.confidence, SOURCE_CONFIDENCE[AnswerSource.LEARNED])
        for option in question.options:
            option.correct = option.label in labels
        question.warnings = [w for w in question.warnings if w != "No answer found."]

        explanation = str(stored.get("explanation") or "").strip()
        if explanation and not question.explanation:
            question.explanation = explanation
        improved += 1

    if improved:
        result.notes.append(
            f"Recovered {improved} answer(s) from earlier corrections.")
    return improved


# --------------------------------------------------------------------------------------
# Teaching the AI backend
# --------------------------------------------------------------------------------------

def examples(subject: str = "", topic: str = "", limit: int = MAX_EXAMPLES) -> str:
    """Worked examples drawn from stored corrections, as prompt text.

    Corrections from the same subject or topic come first, since those carry the
    conventions the current document is most likely to share. Returns "" when there is
    nothing to teach, in which case the prompt is left exactly as it was.
    """
    db = _db()
    if db is None or not enabled():
        return ""

    try:
        rows = db.query(
            """
            SELECT after_json FROM corrections
            ORDER BY
                CASE WHEN subject != '' AND subject = ? THEN 0
                     WHEN topic   != '' AND topic   = ? THEN 1
                     ELSE 2 END,
                created_at DESC
            LIMIT ?
            """,
            (subject, topic, max(1, min(int(limit), MAX_EXAMPLES))),
        )
    except Exception:  # pragma: no cover - table missing on an old database
        return ""

    rendered: List[str] = []
    for row in rows:
        try:
            payload = json.loads(row["after_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        block = _render_example(payload)
        if block:
            rendered.append(block)

    if not rendered:
        return ""

    body = "\n\n".join(rendered)[:MAX_EXAMPLE_CHARS]
    return (
        "A human reviewer previously corrected these questions from this library. "
        "Follow the same conventions where the document allows:\n\n"
        f"{body}\n\n"
    )


def _render_example(payload: Dict[str, Any]) -> str:
    """One correction as a compact question/answer sketch."""
    stem = str(payload.get("question") or "").strip()
    if not stem:
        return ""

    lines = [f"Q: {stem[:300]}"]
    for option in (payload.get("options") or [])[:8]:
        if not isinstance(option, dict):
            continue
        text = str(option.get("text") or "").strip()
        if not text:
            continue
        mark = " (correct)" if option.get("correct") else ""
        lines.append(f"  {option.get('label', '?')}. {text[:160]}{mark}")

    answer = ",".join(str(v) for v in (payload.get("answer_labels") or [])) \
        or str(payload.get("answer_text") or "").strip()
    if answer:
        lines.append(f"A: {answer[:120]}")
    return "\n".join(lines)
