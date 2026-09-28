"""Quizify parsing engine.

Public surface:

    parse_text(text, ...)   -> ParseResult
    parse_file(data, name)  -> ParseResult
    export(result, fmt, ...) -> (buffer, mimetype, filename)

The pipeline is: extract blocks -> profile the document's conventions -> segment
into questions -> detect types and resolve answers -> optionally refine the weak
spots with an AI backend.
"""

import time
from typing import List, Optional

from .analysis import deduplicate, finalize
from .engine import Segmenter, build_profile, segment_unlabelled
from .export import export
from .extractors import (
    ExtractionError,
    SUPPORTED_EXTS,
    blocks_from_text,
    blocks_to_text,
    extract_blocks,
)
from .models import (
    AnswerSource,
    Block,
    Option,
    ParseResult,
    Question,
    QuestionType,
)
from . import ai, bank, features, learning

__all__ = [
    "parse_text", "parse_file", "parse_blocks", "export",
    "ParseResult", "Question", "Option", "QuestionType", "AnswerSource", "Block",
    "ExtractionError", "SUPPORTED_EXTS", "ai", "bank", "features", "learning",
]

# Below this share of confident questions, an `auto` run asks the AI for a second opinion.
AI_AUTO_CONFIDENCE_FLOOR = 0.55
AI_AUTO_MIN_ANSWER_RATE = 0.30


def parse_blocks(blocks: List[Block]) -> ParseResult:
    """Run the rule engine over already-extracted blocks."""
    result = ParseResult()
    if not blocks:
        result.warnings.append("No readable content was found.")
        return result

    profile = build_profile(blocks)
    raws, grid_answers, sequential_answers, warnings = Segmenter(blocks, profile).run()

    # Nothing matched the labelled conventions — fall back to structural grouping.
    if not raws:
        raws = segment_unlabelled(blocks)
        if raws:
            result.notes.append("Parsed using unlabelled-format detection.")

    questions = finalize(raws, grid_answers, sequential_answers)
    result.questions = deduplicate(questions)
    result.warnings.extend(warnings)

    # Restore answers a user corrected on an earlier run before anything downstream
    # judges this result. Doing it here rather than after the AI step means a document
    # the corrections already cover can clear the `auto` threshold and skip the AI call.
    learning.apply(result)

    if not result.questions:
        result.warnings.append(
            "No questions were recognised. Check that the file contains questions "
            "with options, or try pasting the text directly."
        )
    return result


def _needs_ai(result: ParseResult) -> bool:
    """Decide whether an `auto` run should call the AI backend."""
    if not result.questions:
        return True
    stats = result.stats
    total = stats["total"]
    if stats["avg_confidence"] < AI_AUTO_CONFIDENCE_FLOOR:
        return True
    if total >= 3 and (stats["answered"] / total) < AI_AUTO_MIN_ANSWER_RATE:
        return True
    return stats["low_confidence"] / total > 0.5


def _question_key(text: str) -> str:
    """A normalised fingerprint of a question stem, for matching AI ↔ rule output."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())[:120]


def _keys_overlap(a: str, b: str) -> bool:
    """True when two question keys are the same or one is a prefix of the other.

    The AI often lightly rewords a stem (extra punctuation, dropped filler), so an exact
    match is too strict; a shared prefix catches the reworded-but-same question while
    still rejecting a genuinely different (invented) one.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 24 and longer.startswith(shorter)


def _merge_ai(rule_result: ParseResult, ai_questions: List[Question]) -> ParseResult:
    """Prefer the AI's structure, but never let it invent questions.

    The rule engine's set of questions is the ground truth for *which* questions exist
    in the document — the AI only exists to structure them better and recover answers it
    can see. So when the rules already found questions, any AI question that does not
    correspond to a rule-detected one is dropped (models sometimes hallucinate extra
    questions, which previously inflated the count).
    """
    rule_questions = rule_result.questions
    by_key = {}
    for question in rule_questions:
        key = _question_key(question.text)
        if key:
            by_key[key] = question

    def matching_rule_question(ai_q: Question):
        key = _question_key(ai_q.text)
        original = by_key.get(key)
        if original is not None:
            return original
        for rk, rq in by_key.items():
            if _keys_overlap(key, rk):
                return rq
        return None

    kept: List[Question] = []
    for question in ai_questions:
        original = matching_rule_question(question)
        # Guard against invented questions: if the rules found a real set, an AI question
        # with no counterpart is discarded rather than added to the output.
        if original is None:
            if rule_questions:
                continue
            kept.append(question)
            continue

        if not question.answer_display and original.answer_display:
            question.answer_labels = original.answer_labels
            question.answer_text = original.answer_text
            question.answer_source = original.answer_source
            question.warnings = [w for w in question.warnings if w != "No answer found."]
            for option in question.options:
                option.correct = option.label in question.answer_labels
        if not question.explanation and original.explanation:
            question.explanation = original.explanation
        if question.marks is None and original.marks is not None:
            question.marks = original.marks
        kept.append(question)

    # If the AI dropped some rule-detected questions entirely, keep the rule versions so
    # nothing the document actually contains is lost.
    matched_keys = {_question_key(q.text) for q in kept}
    for rq in rule_questions:
        rk = _question_key(rq.text)
        if rk in matched_keys:
            continue
        if any(_keys_overlap(rk, mk) for mk in matched_keys):
            continue
        kept.append(rq)

    merged = ParseResult(
        questions=deduplicate(kept),
        warnings=list(rule_result.warnings),
        source_name=rule_result.source_name,
        source_format=rule_result.source_format,
        notes=list(rule_result.notes),
    )
    merged.warnings = [w for w in merged.warnings if "No questions were recognised" not in w]
    return merged


def _apply_ai(result: ParseResult, text: str, subject: str, topic: str, mode: str,
              provider: Optional[str] = None) -> ParseResult:
    """Run the AI backend when the mode and the rule-engine outcome call for it."""
    mode = (mode or "auto").lower()
    if mode == "off" or ai.backend_name() is None:
        return result
    if mode == "auto" and not _needs_ai(result):
        return result

    # AI assist is the only slow stage in the pipeline — a provider that times out and
    # falls back can cost minutes while the rules output was ready in milliseconds. Every
    # outcome below reports its elapsed time so that cost is visible rather than mysterious.
    started = time.perf_counter()

    def elapsed() -> str:
        return f"{time.perf_counter() - started:.1f}s"

    try:
        ai_questions, backend = ai.parse_with_ai(
            text, subject, topic, provider=provider,
            examples=learning.examples(subject, topic),
        )
    except ai.AIUnavailable as exc:
        result.notes.append(f"AI assist skipped after {elapsed()}: {exc}")
        return result
    except Exception as exc:  # network / auth / rate limit — rules output still stands
        result.notes.append(
            f"AI assist failed after {elapsed()} ({type(exc).__name__}); used rule-based parsing.")
        return result

    if not ai_questions:
        result.notes.append(
            f"AI assist returned no questions after {elapsed()}; used rule-based parsing.")
        return result

    # Only accept the AI result when it is at least as complete as the rules output.
    if len(ai_questions) < len(result.questions) * 0.6:
        result.notes.append(
            f"AI assist found fewer questions ({len(ai_questions)}) than rule-based "
            f"parsing ({len(result.questions)}); kept the rule-based result."
        )
        return result

    merged = _merge_ai(result, ai_questions)
    merged.engine = f"rules+{backend}" if result.questions else backend
    merged.notes.append(f"Refined with AI assist ({backend}).")
    return merged


def parse_text(text: str, subject: str = "", topic: str = "",
               ai_mode: str = "auto", provider: Optional[str] = None,
               source_name: str = "pasted text") -> ParseResult:
    """Parse pasted quiz text."""
    blocks = blocks_from_text(text or "")
    result = parse_blocks(blocks)
    result.source_name = source_name
    result.source_format = "text"
    return _apply_ai(result, text or "", subject, topic, ai_mode, provider)


def parse_file(data: bytes, filename: str, subject: str = "", topic: str = "",
               ai_mode: str = "auto", provider: Optional[str] = None) -> ParseResult:
    """Parse an uploaded file of any supported type.

    Raises :class:`ExtractionError` when the file cannot be read at all.
    """
    blocks, resolved_format = extract_blocks(data, filename)
    result = parse_blocks(blocks)
    result.source_name = filename
    result.source_format = resolved_format
    return _apply_ai(result, blocks_to_text(blocks), subject, topic, ai_mode, provider)
