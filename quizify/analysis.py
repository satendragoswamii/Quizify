"""Turn raw segmented questions into finished :class:`Question` objects.

Two jobs live here:

* **Type detection** — decide whether a question is multiple choice, True/False,
  fill-in-the-blank, matching, assertion-reason, ordering, numeric, or short answer.
* **Answer resolution** — reconcile every answer signal we collected (inline keys,
  answer-key sections, grids, symbols, bold formatting) into one answer plus a
  confidence score, so the caller can tell a certain answer from a guessed one.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .engine import RawQuestion
from .models import AnswerSource, Option, Question, QuestionType, SOURCE_CONFIDENCE
from . import patterns as P


# --------------------------------------------------------------------------------------
# Question type
# --------------------------------------------------------------------------------------

def _is_true_false_options(options: Sequence[Option]) -> bool:
    if not 2 <= len(options) <= 3:
        return False
    tokens = {P.normalize_bool_token(o.text) for o in options}
    return None not in tokens and tokens <= {"TRUE", "FALSE"}


def detect_type(stem: str, options: Sequence[Option], answer_labels: Sequence[str],
                answer_text: Optional[str]) -> QuestionType:
    if P.ASSERTION_RE.search(stem) and P.REASON_RE.search(stem):
        return QuestionType.ASSERTION_REASON
    if P.MATCHING_RE.search(stem):
        return QuestionType.MATCHING
    if _is_true_false_options(options) or P.TRUE_FALSE_STEM_RE.search(stem):
        return QuestionType.TRUE_FALSE
    if any(P.normalize_bool_token(a) for a in answer_labels) and not options:
        return QuestionType.TRUE_FALSE
    if P.ORDERING_RE.search(stem):
        return QuestionType.ORDERING

    if options:
        if len(answer_labels) > 1 or P.MULTI_SELECT_RE.search(stem):
            return QuestionType.MCQ_MULTI
        return QuestionType.MCQ_SINGLE

    # No options — decide between blank, numeric, and free text.
    if P.FILL_BLANK_RE.search(stem):
        return QuestionType.FILL_BLANK
    if answer_text and P.NUMERIC_ANSWER_RE.match(answer_text.strip()):
        return QuestionType.NUMERIC
    if re.search(r"\b(?:calculate|compute|how many|how much|find the value)\b", stem, re.I):
        return QuestionType.NUMERIC
    if stem:
        return QuestionType.SHORT_ANSWER
    return QuestionType.UNKNOWN


# --------------------------------------------------------------------------------------
# Answer resolution
# --------------------------------------------------------------------------------------

def _labels_from_value(value: str, options: Sequence[Option]) -> Tuple[List[str], Optional[str]]:
    """Interpret a raw answer string as option labels, a boolean, or free text."""
    value = value.strip()
    if not value:
        return [], None

    boolean = P.normalize_bool_token(value)
    if boolean and (not options or _is_true_false_options(options)):
        return [boolean], None

    # "A,C" or "A and C" or "2" (positional) or "iii" (roman)
    parts = [p.strip() for p in re.split(r"[,/&+]|\band\b", value, flags=re.I) if p.strip()]
    labels: List[str] = []
    for part in parts:
        token = part.strip(" ().[]")
        if len(token) == 1 and token.isalpha():
            labels.append(token.upper())
            continue
        index: Optional[int] = None
        if token.isdigit():
            index = int(token)
        elif len(token) > 1:
            index = P.roman_to_int(token)
        if index and options and 1 <= index <= len(options):
            labels.append(options[index - 1].label)
    if labels and len(labels) == len(parts):
        return _validate_labels(labels, options), None

    # Not a label — try matching the text of an option verbatim.
    match = _match_option_text(value, options)
    if match:
        return [match], None
    return [], value


def _validate_labels(labels: Sequence[str], options: Sequence[Option]) -> List[str]:
    """Drop labels that point past the end of the option list."""
    if not options:
        return list(dict.fromkeys(labels))
    valid = {o.label for o in options}
    kept = [x for x in dict.fromkeys(labels) if x in valid]
    return kept


def _match_option_text(value: str, options: Sequence[Option]) -> Optional[str]:
    """Answer given as prose ('Answer: Mercury') — find which option it names."""
    if not options:
        return None
    norm = re.sub(r"[^a-z0-9]+", "", value.lower())
    if not norm:
        return None
    exact = [o.label for o in options if re.sub(r"[^a-z0-9]+", "", o.text.lower()) == norm]
    if len(exact) == 1:
        return exact[0]
    if len(norm) >= 4:
        partial = [o.label for o in options
                   if norm in re.sub(r"[^a-z0-9]+", "", o.text.lower())]
        if len(partial) == 1:
            return partial[0]
    return None


def resolve_answer(
    raw: RawQuestion,
    options: List[Option],
    position: int,
    grid_answers: Dict[int, str],
    sequential_answers: List[str],
    sequential_cursor: List[int],
) -> Tuple[List[str], Optional[str], AnswerSource]:
    """Pick the single most trustworthy answer among every signal we collected."""
    candidates: List[Tuple[AnswerSource, List[str], Optional[str]]] = []

    # 1. Inline "Answer: X" beside the question.
    for source, value in raw.answer_hints:
        labels, text = _labels_from_value(value, options)
        if labels or text:
            kind = AnswerSource.INLINE if source.startswith("inline") else AnswerSource.ANSWER_KEY
            candidates.append((kind, labels, text))

    # 2. Answer grid / key section, matched on the question's own number first.
    key = raw.declared_number if raw.declared_number is not None else position
    if key in grid_answers:
        labels, text = _labels_from_value(grid_answers[key], options)
        if labels or text:
            candidates.append((AnswerSource.GRID, labels, text))
    elif position in grid_answers:
        labels, text = _labels_from_value(grid_answers[position], options)
        if labels or text:
            candidates.append((AnswerSource.GRID, labels, text))

    # 3. Symbol / formatting cues on the options themselves. Uniform marking across
    #    every option carries no information, so require exactly one marked option.
    marked = [o for o in options if o.marked]
    if len(marked) == 1:
        candidates.append((AnswerSource.MARKER, [marked[0].label], None))
    elif 1 < len(marked) < len(options):
        candidates.append((AnswerSource.MARKER, [o.label for o in marked], None))

    # 4. Sequential answer list (an unnumbered key section listing answers in order).
    if sequential_cursor[0] < len(sequential_answers) and not candidates:
        labels, text = _labels_from_value(sequential_answers[sequential_cursor[0]], options)
        sequential_cursor[0] += 1
        if labels or text:
            candidates.append((AnswerSource.ANSWER_KEY, labels, text))

    # 5. A True/False stem with no options at all still implies its two choices.
    if not candidates and not options and P.TRUE_FALSE_STEM_RE.search(raw.stem):
        return [], None, AnswerSource.NONE

    if not candidates:
        return [], None, AnswerSource.NONE

    candidates.sort(key=lambda c: SOURCE_CONFIDENCE[c[0]], reverse=True)
    source, labels, text = candidates[0]
    return labels, text, source


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------

def _build_options(raw: RawQuestion) -> List[Option]:
    """Relabel options to a clean A, B, C… sequence while keeping original ordering."""
    options: List[Option] = []
    for position, (_, text, marked) in enumerate(raw.option_texts(), 1):
        text = P.normalize_blanks(text).strip()
        if not text:
            continue
        options.append(Option(label=P.index_to_label(position), text=text, marked=marked))
    return options


def _score(question: Question, raw: RawQuestion) -> float:
    score = 0.35
    if raw.numbered:
        score += 0.15
    if len(question.options) >= 2:
        score += 0.20
    if len(question.options) >= 3:
        score += 0.05
    if question.answer_display:
        score += SOURCE_CONFIDENCE[question.answer_source] * 0.25
    if question.qtype != QuestionType.UNKNOWN:
        score += 0.05
    if len(question.text) < 8:
        score -= 0.25
    score -= 0.12 * len(question.warnings)
    return max(0.0, min(1.0, score))


def _validate(question: Question) -> None:
    """Attach human-readable warnings for anything a reviewer should eyeball."""
    if not question.text:
        question.warnings.append("Question text is empty.")
    elif len(question.text) < 8:
        question.warnings.append("Question text looks unusually short.")

    if question.qtype in (QuestionType.MCQ_SINGLE, QuestionType.MCQ_MULTI):
        if len(question.options) < 2:
            question.warnings.append("Fewer than two options were detected.")
        texts = [o.text.strip().lower() for o in question.options]
        if len(texts) != len(set(texts)):
            question.warnings.append("Duplicate options detected.")

    if not question.answer_display:
        question.warnings.append("No answer found.")
    elif question.answer_labels and question.options:
        valid = {o.label for o in question.options}
        unknown = [x for x in question.answer_labels
                   if x not in valid and not P.normalize_bool_token(x)]
        if unknown:
            question.warnings.append(
                f"Answer '{','.join(unknown)}' does not match any option."
            )

    if question.qtype == QuestionType.MCQ_SINGLE and len(question.answer_labels) > 1:
        question.warnings.append("Multiple answers found for a single-answer question.")


def finalize(
    raws: List[RawQuestion],
    grid_answers: Dict[int, str],
    sequential_answers: List[str],
) -> List[Question]:
    """Convert raw segments into validated, typed, answered questions."""
    questions: List[Question] = []
    cursor = [0]

    for position, raw in enumerate(raws, 1):
        stem = P.normalize_blanks(raw.stem).strip()
        options = _build_options(raw)

        # A True/False stem with no explicit choices gets its implied options.
        if not options and P.TRUE_FALSE_STEM_RE.search(stem):
            stem = P.TRUE_FALSE_CLEAN_RE.sub(" ", stem).strip(" .:-")
            options = [Option(label="A", text="TRUE"), Option(label="B", text="FALSE")]

        if not stem and not options:
            continue

        labels, answer_text, source = resolve_answer(
            raw, options, position, grid_answers, sequential_answers, cursor
        )

        # A True/False answer maps back onto the TRUE/FALSE options.
        if labels and _is_true_false_options(options):
            mapped = []
            for label in labels:
                token = P.normalize_bool_token(label)
                if token:
                    hit = next((o.label for o in options
                                if P.normalize_bool_token(o.text) == token), None)
                    mapped.append(hit or label)
                else:
                    mapped.append(label)
            labels = mapped

        for option in options:
            option.correct = option.label in labels

        qtype = detect_type(stem, options, labels, answer_text)

        question = Question(
            text=stem,
            options=options,
            qtype=qtype,
            number=position,
            answer_labels=labels,
            answer_text=answer_text,
            explanation=raw.explanation,
            marks=raw.marks,
            directive=raw.directive,
            answer_source=source,
            warnings=list(raw.warnings),
        )
        _validate(question)
        question.confidence = _score(question, raw)
        questions.append(question)

    return questions


def deduplicate(questions: List[Question]) -> List[Question]:
    """Drop repeats that arise when a document is read from both tables and paragraphs."""
    seen: Dict[str, int] = {}
    result: List[Question] = []
    for question in questions:
        key = re.sub(r"[^a-z0-9]+", "", question.text.lower())[:160]
        if not key:
            result.append(question)
            continue
        if key in seen:
            # Keep whichever copy carries more information.
            existing = result[seen[key]]
            if (len(question.options), bool(question.answer_display)) > (
                len(existing.options), bool(existing.answer_display)
            ):
                question.number = existing.number
                result[seen[key]] = question
            continue
        seen[key] = len(result)
        result.append(question)

    for index, question in enumerate(result, 1):
        question.number = index
    return result
