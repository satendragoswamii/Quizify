"""Core data structures shared across the Quizify parsing pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class QuestionType(str, Enum):
    """Every question shape the engine knows how to recognise."""

    MCQ_SINGLE = "MCQ"
    MCQ_MULTI = "Multi-Select"
    TRUE_FALSE = "True/False"
    FILL_BLANK = "Fill in the Blank"
    MATCHING = "Matching"
    ASSERTION_REASON = "Assertion-Reason"
    ORDERING = "Ordering"
    NUMERIC = "Numeric"
    SHORT_ANSWER = "Short Answer"
    UNKNOWN = "Unknown"


class AnswerSource(str, Enum):
    """Where a resolved answer came from, ordered loosely by trustworthiness."""

    USER = "user"                  # typed or confirmed by a person in the preview
    INLINE = "inline"              # "Answer: B" right by the question
    ANSWER_KEY = "answer_key"      # trailing "Answer Key" section
    GRID = "grid"                  # "Q1: c, Q2: d" style grid
    MARKER = "marker"              # *, ✓, ** on the option itself
    FORMATTING = "formatting"      # bold / highlighted / underlined option
    LEARNED = "learned"            # recovered from a correction a user made earlier
    TEXT_MATCH = "text_match"      # answer given as prose matching an option
    DERIVED = "derived"            # inferred (e.g. True/False from stem)
    NONE = "none"


SOURCE_CONFIDENCE = {
    # A person read this question and confirmed the answer — nothing outranks that.
    AnswerSource.USER: 1.0,
    AnswerSource.INLINE: 0.98,
    AnswerSource.ANSWER_KEY: 0.95,
    # A human correction, but carried over from a different document rather than found
    # in this one, so it ranks below the in-document evidence above it.
    AnswerSource.LEARNED: 0.93,
    AnswerSource.GRID: 0.92,
    AnswerSource.MARKER: 0.90,
    AnswerSource.TEXT_MATCH: 0.82,
    AnswerSource.FORMATTING: 0.78,
    AnswerSource.DERIVED: 0.60,
    AnswerSource.NONE: 0.0,
}


# --------------------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------------------
# Used only by the ``from_dict`` constructors, which read JSON posted by a browser.
# Anything unusable becomes an empty/None value rather than raising, so one malformed
# field cannot fail a whole export.

def _as_text(value: Any) -> str:
    return "" if value is None or isinstance(value, (list, dict)) else str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_enum(enum_cls, value: Any, fallback):
    try:
        return enum_cls(_as_text(value))
    except ValueError:
        return fallback


@dataclass
class Block:
    """One logical line of source content plus whatever formatting survived extraction."""

    text: str
    index: int = 0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    highlight: bool = False
    list_level: Optional[int] = None
    origin: str = "text"          # text | docx | table | pdf | csv | json | html | ocr
    blank_before: bool = False
    page: Optional[int] = None


@dataclass
class Option:
    label: str                    # normalised A-Z label
    text: str
    correct: bool = False
    marked: bool = False          # carried a *, ✓, bold, or highlight cue


@dataclass
class Question:
    text: str
    options: List[Option] = field(default_factory=list)
    qtype: QuestionType = QuestionType.UNKNOWN
    number: Optional[int] = None
    answer_labels: List[str] = field(default_factory=list)
    answer_text: Optional[str] = None
    explanation: Optional[str] = None
    marks: Optional[float] = None
    directive: Optional[str] = None
    answer_source: AnswerSource = AnswerSource.NONE
    confidence: float = 0.0
    warnings: List[str] = field(default_factory=list)
    engine: str = "rules"

    @property
    def answer_display(self) -> str:
        """Answer rendered the way it should appear in a spreadsheet cell."""
        if self.answer_labels:
            return ",".join(self.answer_labels)
        return self.answer_text or ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "type": self.qtype.value,
            "question": self.text,
            "options": [
                {"label": o.label, "text": o.text, "correct": o.correct}
                for o in self.options
            ],
            "answer": self.answer_display,
            "answer_labels": self.answer_labels,
            "answer_text": self.answer_text,
            "explanation": self.explanation,
            "marks": self.marks,
            "directive": self.directive,
            "answer_source": self.answer_source.value,
            "confidence": round(self.confidence, 3),
            "warnings": self.warnings,
            "engine": self.engine,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Question":
        """Rebuild a question from :meth:`to_dict` output.

        This is the return trip for the preview flow: the browser sends a previewed
        result back to be exported, so the source is never parsed twice. The payload
        arrives from a client, so every field is coerced rather than trusted.
        """
        options: List[Option] = []
        for raw in _as_list(data.get("options")):
            if not isinstance(raw, dict):
                continue
            text = _as_text(raw.get("text")).strip()
            if not text:
                continue
            label = _as_text(raw.get("label")).strip()[:4]
            options.append(Option(
                label=label or chr(65 + len(options)),
                text=text,
                correct=bool(raw.get("correct")),
            ))

        labels = [t for t in (_as_text(v).strip()[:8] for v in _as_list(data.get("answer_labels"))) if t]
        answer_text = _as_text(data.get("answer_text")).strip() or None
        explanation = _as_text(data.get("explanation")).strip() or None
        directive = _as_text(data.get("directive")).strip() or None

        return cls(
            text=_as_text(data.get("question")).strip(),
            options=options,
            qtype=_as_enum(QuestionType, data.get("type"), QuestionType.UNKNOWN),
            number=_as_int(data.get("number")),
            answer_labels=labels,
            answer_text=answer_text,
            explanation=explanation,
            marks=_as_float(data.get("marks")),
            directive=directive,
            answer_source=_as_enum(AnswerSource, data.get("answer_source"), AnswerSource.NONE),
            confidence=min(1.0, max(0.0, _as_float(data.get("confidence")) or 0.0)),
            warnings=[t for t in (_as_text(w).strip()[:300] for w in _as_list(data.get("warnings"))) if t],
            engine=_as_text(data.get("engine")).strip()[:40] or "rules",
        )


@dataclass
class ParseResult:
    questions: List[Question] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_name: str = ""
    source_format: str = ""
    engine: str = "rules"
    notes: List[str] = field(default_factory=list)

    @property
    def stats(self) -> Dict[str, Any]:
        total = len(self.questions)
        answered = sum(1 for q in self.questions if q.answer_display)
        by_type: Dict[str, int] = {}
        for q in self.questions:
            by_type[q.qtype.value] = by_type.get(q.qtype.value, 0) + 1
        avg_conf = sum(q.confidence for q in self.questions) / total if total else 0.0
        return {
            "total": total,
            "answered": answered,
            "unanswered": total - answered,
            "low_confidence": sum(1 for q in self.questions if q.confidence < 0.6),
            "avg_confidence": round(avg_conf, 3),
            "by_type": by_type,
            "engine": self.engine,
            "source_format": self.source_format,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_format": self.source_format,
            "engine": self.engine,
            "stats": self.stats,
            "warnings": self.warnings,
            "notes": self.notes,
            "questions": [q.to_dict() for q in self.questions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], limit: int = 5000) -> "ParseResult":
        """Rebuild a result from :meth:`to_dict` output, for export without re-parsing.

        ``limit`` caps how many questions a single export may carry, so an oversized
        payload cannot tie up a worker building an enormous spreadsheet.
        """
        # Keep anything the engine itself would have kept: ``finalize`` drops a segment
        # only when it has neither a stem nor options, so requiring a stem here would
        # silently lose rows that previewed fine.
        questions = [
            question
            for question in (
                Question.from_dict(raw)
                for raw in _as_list(data.get("questions"))[:limit]
                if isinstance(raw, dict)
            )
            if question.text or question.options
        ]
        return cls(
            questions=questions,
            warnings=[t for t in (_as_text(w).strip()[:500] for w in _as_list(data.get("warnings"))) if t],
            source_name=_as_text(data.get("source_name")).strip()[:255],
            source_format=_as_text(data.get("source_format")).strip()[:32],
            engine=_as_text(data.get("engine")).strip()[:40] or "rules",
            notes=[t for t in (_as_text(n).strip()[:500] for n in _as_list(data.get("notes"))) if t],
        )
