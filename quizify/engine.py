"""Segmentation engine: a list of :class:`Block`s in, a list of raw questions out.

The engine works in two passes. First it *profiles* the document to learn which
conventions it actually uses — whether questions are ``Q1.`` or ``1)``, whether
options are ``A.`` or ``(i)``. Then it walks the blocks with that profile in hand,
which lets it reject lines that merely resemble a question or an option. Format
collisions (``1.`` as a question vs ``1.`` as an option) are the single largest
source of mis-parsing, and the profile is what resolves them.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import Block
from . import patterns as P

# Styles that can plausibly mark a question but also an option.
_AMBIGUOUS_STYLES = {"num_dot", "num_paren", "num_colon", "num_bracket", "roman_up"}

# An option run rarely exceeds this; beyond it we are almost certainly mis-reading.
MAX_OPTIONS = 26


@dataclass
class RawQuestion:
    """Accumulator for one question while the state machine is walking the document."""

    number: Optional[int] = None
    declared_number: Optional[int] = None
    stem_lines: List[str] = field(default_factory=list)
    options: List[Tuple[Optional[int], List[str], bool]] = field(default_factory=list)
    answer_hints: List[Tuple[str, str]] = field(default_factory=list)  # (source, raw value)
    explanation: Optional[str] = None
    marks: Optional[float] = None
    directive: Optional[str] = None
    numbered: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return " ".join(part for part in self.stem_lines if part).strip()

    def add_option(self, index: Optional[int], text: str, marked: bool) -> None:
        self.options.append((index, [text], marked))

    def extend_last_option(self, text: str) -> bool:
        if not self.options:
            return False
        self.options[-1][1].append(text)
        return True

    def option_texts(self) -> List[Tuple[Optional[int], str, bool]]:
        return [(idx, " ".join(parts).strip(), marked) for idx, parts, marked in self.options]

    def is_empty(self) -> bool:
        return not self.stem and not self.options


_UNLABELLED_STYLES = {"bullet", "checkbox", "dash_bullet"}

# Label styles that a True/False worksheet typically uses to number its statements.
# Each is also a normal option label, which is exactly why the verdicts are needed to
# tell the two apart.
_STATEMENT_LABEL_STYLES = {"roman_low", "alpha_paren", "alpha_dot", "num_dot",
                           "num_paren", "paren_num", "paren_alpha"}


def _count_verdicts(blocks: List[Block]) -> int:
    """How many statements carry a bare TRUE/FALSE verdict.

    The signal is a statement followed by a lone verdict — either trailing the same line
    or sitting on the next one. A document that states its answers properly
    ("Answer: True") is left to the normal path, which already handles it unambiguously.
    """
    verdicts = 0
    for index, block in enumerate(blocks):
        text = block.text
        if P.ANSWER_BOOL_RE.search(text):
            continue  # an explicit "Answer: True" — not this convention
        if P.split_trailing_bool(text)[1]:
            verdicts += 1
            continue
        # A verdict alone on its line counts only after real prose. Option text is
        # short, so the length test is what separates a worksheet statement from the
        # "A. True / B. False" pair of a two-option question.
        if index and P.BARE_BOOL_LINE_RE.match(text):
            previous = blocks[index - 1].text
            if len(previous) > 25 and not P.BARE_BOOL_LINE_RE.match(previous):
                verdicts += 1
    return verdicts


@dataclass
class StyleProfile:
    question_styles: Dict[str, int] = field(default_factory=dict)
    option_styles: Dict[str, int] = field(default_factory=dict)
    dominant_question: Optional[str] = None
    dominant_option: Optional[str] = None
    # Set when questions and options genuinely share one convention (e.g. "1." for both).
    # The engine then falls back to option-sequence continuity to tell them apart.
    shared_style: Optional[str] = None
    labelled_options: bool = False
    numbered_questions: bool = False
    uses_blank_separation: bool = False
    # Set when the document is a True/False worksheet: numbered statements, no choices,
    # and a bare TRUE/FALSE verdict after each one instead of an "Answer:" line.
    true_false_statements: bool = False

    def question_style_allowed(self, style: str) -> bool:
        if self.dominant_question is None:
            # No established convention: accept anything except the option style.
            return style != self.dominant_option or style == self.shared_style
        if style == self.dominant_question or style == self.shared_style:
            return True
        if style == self.dominant_option:
            return False
        # When the document's question convention is a distinct, non-ambiguous marker
        # (e.g. "Q1)" / "Question 1:"), a plain numbered line ("2.") can only be a new
        # question — the options here use their own labelled style, so it is safe to admit
        # even an otherwise-ambiguous number. (A sequence guard still rejects stray prose
        # numbers.) This fixes mixed-style docs where "Q1) … / 2. …" lost the second stem.
        if (style in _AMBIGUOUS_STYLES
                and self.dominant_question not in _AMBIGUOUS_STYLES
                and style != self.dominant_option):
            return True
        # A distinct, unambiguous marker (Q1. amid bare numbers) still reads as a question.
        return style not in _AMBIGUOUS_STYLES

    def option_style_allowed(self, style: str) -> bool:
        if style in _UNLABELLED_STYLES:
            return True  # bullets coexist with any labelled convention
        if self.dominant_option is None:
            return True
        return style == self.dominant_option or style == self.shared_style


# --------------------------------------------------------------------------------------
# Pass 1 — profiling
# --------------------------------------------------------------------------------------

def build_profile(blocks: List[Block]) -> StyleProfile:
    """Learn the document's conventions.

    Options are profiled first because a run of A/B/C/D (or 1/2/3/4) is the most
    reliable signal in a quiz. Question styles are then counted over the lines the
    option convention did *not* claim, which stops the far more numerous option
    lines from drowning out the question numbering.
    """
    o_counter: Counter = Counter()
    blank_separated = 0
    considered: List[Block] = []

    for block in blocks:
        text = block.text
        if P.ANSWER_SECTION_RE.match(text) or P.NOISE_RE.match(text):
            continue
        if block.blank_before:
            blank_separated += 1
        considered.append(block)
        for style, index, _ in P.match_option_openers(text):
            if index is not None:
                o_counter[style] += 1

    profile = StyleProfile(option_styles=dict(o_counter))

    # Ignore incidental hits so one stray "A." cannot set the convention.
    significant = [(s, c) for s, c in o_counter.most_common() if c >= 3]
    if significant:
        profile.dominant_option = significant[0][0]
        profile.labelled_options = True

    # A True/False worksheet numbers its statements the way other documents label their
    # options, so the verdicts are the only thing that tells the two apart. Requiring a
    # verdict for most labelled lines — not merely a couple — is what keeps a normal
    # multiple-choice paper that happens to contain the word "True" out of this branch.
    verdicts = _count_verdicts(considered)
    labelled = o_counter.get(profile.dominant_option, 0) if profile.dominant_option else 0
    profile.true_false_statements = verdicts >= 2 and verdicts * 2 >= labelled

    if profile.true_false_statements and profile.dominant_option in _STATEMENT_LABEL_STYLES:
        # Those labels are numbering statements, not choices. Nothing in this document
        # is an option, so the convention is re-read as question numbering.
        profile.dominant_option = None
        profile.labelled_options = False

    q_counter: Counter = Counter()
    shadowed = 0
    for block in considered:
        candidates = P.match_question_openers(
            block.text, statement_labels=profile.true_false_statements)
        if not candidates:
            continue
        # A line the option convention already claims cannot also vote on question
        # style — otherwise the far more numerous option lines decide it.
        claimed_by_options = profile.dominant_option and any(
            style == profile.dominant_option
            for style, index, _ in P.match_option_openers(block.text)
            if index is not None
        )
        if claimed_by_options:
            shadowed += 1
            continue
        q_counter[candidates[0][0]] += 1

    profile.question_styles = dict(q_counter)
    if q_counter:
        style, count = q_counter.most_common(1)[0]
        # An unambiguous marker such as "Q1." establishes the convention on its own;
        # a shape that could equally be an option needs to repeat first.
        if count >= 2 or style not in _AMBIGUOUS_STYLES:
            profile.dominant_question = style
            profile.numbered_questions = True

    # No line survived as a question candidate, yet several were absorbed by the option
    # style: the document uses one convention for both and the engine has to
    # disambiguate positionally instead.
    if (profile.dominant_question is None and profile.dominant_option
            and shadowed >= 2 and not q_counter):
        profile.shared_style = profile.dominant_option
        profile.numbered_questions = True

    profile.uses_blank_separation = blank_separated >= max(3, len(blocks) // 8)
    return profile


# --------------------------------------------------------------------------------------
# Pass 2 — segmentation
# --------------------------------------------------------------------------------------

class Segmenter:
    def __init__(self, blocks: List[Block], profile: StyleProfile):
        self.blocks = blocks
        self.profile = profile
        self.questions: List[RawQuestion] = []
        self.current: Optional[RawQuestion] = None
        self.grid_answers: Dict[int, str] = {}
        self.sequential_answers: List[str] = []
        self.pending_directive: Optional[str] = None
        self.orphan_lines: List[str] = []
        self.in_answer_section = False
        self.warnings: List[str] = []
        self._last_number: Optional[int] = None

    # -- lifecycle ---------------------------------------------------------------------

    def _flush(self) -> None:
        if self.current and not self.current.is_empty():
            self.questions.append(self.current)
        self.current = None

    def _start(self, stem: str, number: Optional[int], numbered: bool) -> None:
        self._flush()
        self.current = RawQuestion(
            number=number,
            declared_number=number,
            stem_lines=[stem] if stem else [],
            numbered=numbered,
            directive=self.pending_directive,
        )
        if number is not None:
            self._last_number = number
        self.orphan_lines = []

    # -- helpers -----------------------------------------------------------------------

    def _next_option_index(self) -> int:
        return len(self.current.options) + 1 if self.current else 1

    def _option_breaks_sequence(self, index: Optional[int]) -> bool:
        """True when a labelled option restarts the alphabet, i.e. a new question began."""
        if index is None or not self.current or len(self.current.options) < 2:
            return False
        prior = [i for i, _, _ in self.current.options if i is not None]
        if not prior:
            return False
        return index <= prior[-1]

    def _continues_option_run(self, index: Optional[int]) -> bool:
        """Would this label be the next option of the question currently open?

        Used only when the document labels questions and options identically, where
        position in the sequence is the only thing that separates the two.
        """
        if self.current is None or index is None:
            return False
        prior = [i for i, _, _ in self.current.options if i is not None]
        if not prior:
            # First option after a stem: accept 1 (A) — anything else looks like a jump.
            return index == 1 and bool(self.current.stem)
        return index == prior[-1] + 1

    def _is_continuing_option(self, text: str) -> bool:
        """Does this line extend the option run of the question currently open?"""
        if self.current is None or not self.profile.dominant_option:
            return False
        for style, index, body in P.match_option_openers(text):
            if style == self.profile.dominant_option and body:
                return self._continues_option_run(index)
        return False

    def _select_candidate(self, candidates, allowed, preferred: Optional[str]):
        """Pick the reading that agrees with the document's convention."""
        if not candidates:
            return None
        if preferred:
            for candidate in candidates:
                if candidate[0] == preferred:
                    return candidate
        for candidate in candidates:
            if allowed(candidate[0]):
                return candidate
        return None

    def _plausible_question_number(self, number: Optional[int]) -> bool:
        """Guard against prose like '1985 saw ...' being read as question 1985."""
        if number is None or self._last_number is None:
            return True
        delta = number - self._last_number
        return -2 <= delta <= 25

    def _record_answer(self, source: str, value: str) -> None:
        if self.current is not None:
            self.current.answer_hints.append((source, value))
        else:
            self.sequential_answers.append(value)

    # -- main loop ---------------------------------------------------------------------

    def run(self) -> Tuple[List[RawQuestion], Dict[int, str], List[str], List[str]]:
        for i, block in enumerate(self.blocks):
            self._handle(i, block)
        self._flush()
        return self.questions, self.grid_answers, self.sequential_answers, self.warnings

    def _handle(self, i: int, block: Block) -> None:
        text = block.text
        if not text or P.NOISE_RE.match(text):
            return

        # --- trailing answer-key section -------------------------------------------
        if P.ANSWER_SECTION_RE.match(text):
            self._flush()
            self.in_answer_section = True
            return
        if self.in_answer_section:
            pairs = P.parse_answer_grid(text)
            if pairs:
                self.grid_answers.update({n: l for n, l in pairs})
                return
            standalone = P.ANSWER_STANDALONE_RE.match(text)
            if standalone:
                self.sequential_answers.append(standalone.group(1).upper())
                return
            # A real question opener means the key section has ended.
            if P.match_question_opener(text) and P.STRONG_QUESTION_RE.search(text):
                self.in_answer_section = False
            else:
                return

        # --- inline answer grid ------------------------------------------------------
        pairs = P.parse_answer_grid(text)
        if pairs and self._looks_like_grid_line(text, pairs):
            self.grid_answers.update({n: l for n, l in pairs})
            return

        # --- explanation --------------------------------------------------------------
        expl = P.EXPLANATION_RE.match(text)
        if expl and self.current is not None:
            body = expl.group(1).strip()
            self.current.explanation = (
                f"{self.current.explanation} {body}".strip() if self.current.explanation else body
            )
            return

        # --- bare TRUE/FALSE verdict ---------------------------------------------------
        # In a True/False worksheet a lone "True" is the answer to the statement above,
        # not an option or a continuation. Checked before the option handlers, which
        # would otherwise claim it.
        if (self.profile.true_false_statements and self.current is not None
                and P.BARE_BOOL_LINE_RE.match(text)):
            token = P.normalize_bool_token(P.BARE_BOOL_LINE_RE.match(text).group(1))
            if token:
                self._record_answer("inline", token)
                return

        # --- explicit answer -----------------------------------------------------------
        if self._consume_answer_line(text):
            return

        # --- directive / section heading ----------------------------------------------
        if P.DIRECTIVE_RE.match(text) and not P.match_option_opener(text):
            self._flush()
            self.pending_directive = text
            return

        # --- option that continues the current run ------------------------------------
        # Checked before the question opener: an established option convention that
        # extends the run in progress outranks a speculative question reading, which is
        # what stops single letters like "I." or "X." being taken for roman numerals.
        if self._is_continuing_option(text) and self._try_option(text, block):
            return

        # --- question opener -----------------------------------------------------------
        if self._try_question(text, block):
            return

        # --- option --------------------------------------------------------------------
        if self._try_option(text, block):
            return

        # --- Word auto-numbered list ---------------------------------------------------
        if block.list_level is not None and self._try_autonumber(text, block):
            return

        # --- bare option (documents that label nothing at all) -------------------------
        if not self.profile.labelled_options and self._try_bare_option(text, block):
            return

        # --- continuation --------------------------------------------------------------
        self._continue(text, block)

    # -- individual handlers -------------------------------------------------------------

    def _looks_like_grid_line(self, text: str, pairs: List[Tuple[int, str]]) -> bool:
        """Distinguish '1-B  2-D  3-A' from a normal sentence that happens to match."""
        if len(pairs) >= 3:
            return True
        # TRUE/FALSE values look identical to a numbered option ("2. No"), so outside an
        # explicit answer-key section only single-letter keys are trusted.
        if any(len(value) > 1 for _, value in pairs):
            return False
        covered = sum(len(f"{n}{l}") + 2 for n, l in pairs)
        if len(pairs) >= 2 and covered >= len(text) * 0.4:
            return True
        # A single pair only counts on a short standalone line with no prose.
        return len(pairs) == 1 and len(text) <= 12 and not re.search(r"[a-z]{3,}", text)

    def _consume_answer_line(self, text: str) -> bool:
        """Handle 'Answer: B', 'Ans - True', 'Correct: A, C' and bare key lines."""
        multi = P.ANSWER_MULTI_RE.search(text)
        if multi:
            letters = [g.upper() for g in multi.groups() if g]
            self._record_answer("inline", ",".join(letters))
            return True

        boolean = P.ANSWER_BOOL_RE.search(text)
        if boolean:
            token = P.normalize_bool_token(boolean.group(1))
            if token:
                self._record_answer("inline", token)
                return True

        single = P.ANSWER_SINGLE_RE.search(text)
        if single:
            self._record_answer("inline", single.group(1).upper())
            # "Answer: B. Because ..." — keep the tail as an explanation.
            tail = text[single.end():].strip(" .:-–")
            if len(tail) > 25 and self.current is not None and not self.current.explanation:
                self.current.explanation = tail
            return True

        free = P.ANSWER_FREE_RE.search(text)
        if free:
            value = free.group(1).strip()
            if value and len(value) <= 200:
                self._record_answer("inline_text", value)
                return True

        # A line containing only a key, e.g. "B." — but never when we have no question yet
        # and never when the option style would claim it.
        standalone = P.ANSWER_STANDALONE_RE.match(text)
        if standalone and self.current is not None and self.current.options:
            token = standalone.group(1).upper()
            if not (self.profile.dominant_option and len(text) <= 3 and text[0].isalpha()):
                self._record_answer("standalone", token)
                return True
        return False

    def _try_question(self, text: str, block: Block) -> bool:
        opener = self._select_candidate(
            P.match_question_openers(
                text, statement_labels=self.profile.true_false_statements),
            self.profile.question_style_allowed,
            self.profile.dominant_question or self.profile.shared_style,
        )
        if not opener:
            return self._try_unlabelled_question(text, block)

        style, number, rest = opener

        # A bare number needs corroboration before we treat it as a question.
        if style == "num_bare":
            if not (P.STRONG_QUESTION_RE.match(rest) or rest.endswith("?") or len(rest) > 40):
                return False
        if not self._plausible_question_number(number):
            return False

        # Questions and options share one convention: this line is an option whenever
        # it continues the run of the question already open.
        if style == self.profile.shared_style and self._continues_option_run(number):
            return False

        rest, verdict = self._split_verdict(rest)
        self._start(rest, number, numbered=True)
        if verdict:
            self._record_answer("inline", verdict)
        self._absorb_marks()
        return True

    def _split_verdict(self, text: str) -> Tuple[str, Optional[str]]:
        """Peel a trailing TRUE/FALSE verdict off a statement.

        Only active once the document has been recognised as a True/False worksheet,
        where the relaxed match is safe because the convention is already established.
        """
        if not self.profile.true_false_statements:
            return text, None
        stem, verdict = P.split_trailing_bool(text)
        if verdict is None:
            stem, verdict = P.split_trailing_bool(text, loose=True)
        return stem, verdict

    def _try_unlabelled_question(self, text: str, block: Block) -> bool:
        """Documents with no question numbers: rely on structure and phrasing."""
        if self.profile.numbered_questions:
            return False
        strong = bool(P.STRONG_QUESTION_RE.match(text) or text.rstrip().endswith("?"))
        dangling = bool(P.DANGLING_STEM_RE.search(text))
        if not (strong or dangling):
            return False
        # Only break the current question once it already collected its options.
        if self.current is not None and not self.current.options:
            return False
        if self.current is not None and not (block.blank_before or strong):
            return False
        self._start(text, None, numbered=False)
        self._absorb_marks()
        return True

    def _try_option(self, text: str, block: Block) -> bool:
        opener = self._select_candidate(
            P.match_option_openers(text),
            self.profile.option_style_allowed,
            self.profile.dominant_option,
        )
        if not opener:
            return False
        style, index, body = opener
        if not body:
            return False

        # An unlabelled bullet is only an option when a question is open.
        if index is None and self.current is None:
            return False

        # Reject a line that is much more likely to be the next question.
        if (style in _AMBIGUOUS_STYLES and self.profile.dominant_question == style
                and self.profile.dominant_option != style):
            return False

        # Under a shared convention, only a line that continues the run is an option.
        if style == self.profile.shared_style and not self._continues_option_run(index):
            return False

        if self.current is None:
            # Options arrived before any recognised stem — adopt the preceding prose.
            stem = " ".join(self.orphan_lines[-3:]).strip()
            self._start(stem, None, numbered=False)

        if self._option_breaks_sequence(index):
            # Alphabet restarted: this is a fresh question whose stem was plain prose.
            trailing = self.current.stem_lines[-1] if len(self.current.stem_lines) > 1 else ""
            self._flush()
            self.current = RawQuestion(stem_lines=[trailing] if trailing else [],
                                       directive=self.pending_directive)

        if len(self.current.options) >= MAX_OPTIONS:
            self.current.warnings.append("Too many options detected; extra lines were ignored.")
            return True

        body, marked_symbol = P.strip_correct_marker(body)
        marked_format = self._is_marked_by_format(block)
        self.current.add_option(index if index is not None else self._next_option_index(),
                                body, marked_symbol or marked_format)
        return True

    def _try_bare_option(self, text: str, block: Block) -> bool:
        """Choices written as plain lines under a stem, with no labels of any kind.

            Which planet is closest to the sun?
            Mercury
            Venus

        Only runs when the document labels no options anywhere, so it cannot steal
        lines from a normally-labelled quiz.
        """
        if self.current is None or not self.current.stem:
            return False
        if len(self.current.options) >= MAX_OPTIONS:
            return False
        # The stem has to look finished — otherwise this line is still part of it.
        stem = self.current.stem
        if not (self.current.options or stem.rstrip().endswith("?")
                or P.DANGLING_STEM_RE.search(stem)):
            return False
        # Long prose and fresh question phrasing are not options.
        if len(text) > 220 or P.STRONG_QUESTION_RE.match(text) or text.rstrip().endswith("?"):
            return False
        body, marked = P.strip_correct_marker(text)
        self.current.add_option(self._next_option_index(), body,
                                marked or self._is_marked_by_format(block))
        return True

    def _is_marked_by_format(self, block: Block) -> bool:
        """Bold/highlight only counts as a correctness cue — never when it's document-wide."""
        return bool(block.highlight or block.bold or block.underline)

    def _try_autonumber(self, text: str, block: Block) -> bool:
        """Word list numbering keeps the marker out of the text; level 0 = question."""
        if block.list_level == 0:
            self._start(text, None, numbered=True)
            return True
        if self.current is not None:
            self.current.add_option(self._next_option_index(), text, self._is_marked_by_format(block))
            return True
        return False

    def _continue(self, text: str, block: Block) -> None:
        if self.current is None:
            self.orphan_lines.append(text)
            if len(self.orphan_lines) > 6:
                self.orphan_lines.pop(0)
            return

        # A wrapped option line continues the last option; otherwise extend the stem.
        if self.current.options:
            if block.blank_before and P.STRONG_QUESTION_RE.match(text):
                self._start(text, None, numbered=False)
            else:
                self.current.extend_last_option(text)
        else:
            self.current.stem_lines.append(text)
        self._absorb_marks()

    def _absorb_marks(self) -> None:
        if self.current is None or self.current.marks is not None:
            return
        m = P.MARKS_RE.search(self.current.stem)
        if m:
            try:
                self.current.marks = float(m.group(1))
            except ValueError:
                return
            self.current.stem_lines = [P.MARKS_RE.sub("", self.current.stem).strip()]


# --------------------------------------------------------------------------------------
# Fallback for fully unlabelled documents
# --------------------------------------------------------------------------------------

def segment_unlabelled(blocks: List[Block]) -> List[RawQuestion]:
    """No question numbers, no option labels — group by blank lines and phrasing.

    Used when the primary pass finds nothing, e.g. text pasted as:

        Which planet is closest to the sun?
        Mercury
        Venus
        Earth
    """
    questions: List[RawQuestion] = []
    current: Optional[RawQuestion] = None

    def flush() -> None:
        nonlocal current
        if current and current.stem and current.options:
            questions.append(current)
        current = None

    for block in blocks:
        text = block.text
        if not text or P.NOISE_RE.match(text) or P.ANSWER_SECTION_RE.match(text):
            continue

        starts_question = bool(
            P.STRONG_QUESTION_RE.match(text)
            or text.rstrip().endswith("?")
            or P.DANGLING_STEM_RE.search(text)
        )

        if current is None:
            if starts_question or block.blank_before or not questions:
                current = RawQuestion(stem_lines=[text], numbered=False)
            continue

        if not current.options:
            # Still gathering the stem unless this line clearly begins the choices.
            if starts_question and block.blank_before:
                flush()
                current = RawQuestion(stem_lines=[text], numbered=False)
            else:
                body, marked = P.strip_correct_marker(text)
                current.add_option(1, body, marked)
            continue

        if starts_question or (block.blank_before and len(current.options) >= 2):
            flush()
            current = RawQuestion(stem_lines=[text], numbered=False)
        else:
            body, marked = P.strip_correct_marker(text)
            current.add_option(len(current.options) + 1, body, marked)

    flush()
    return questions
