"""Regex library and low-level line decoders.

Everything the engine knows about *how quiz text looks* lives here. Each family of
patterns carries a style name so the engine can work out which convention a given
document uses and then reject lines that merely look similar.
"""

import re
import unicodedata
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------

_SMART_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "‒": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "﻿": "", "​": "", "‌": "", "‍": "",
}

_BLANK_RUN = re.compile(r"[_\.]{3,}|(?:_\s){3,}_")


def normalize_text(text: str) -> str:
    """Fold unicode punctuation and collapse whitespace without losing line structure."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _SMART_MAP.items():
        text = text.replace(bad, good)
    text = text.replace("\t", "    ")
    text = re.sub(r"[ 　]+", " ", text)
    return text.strip()


def normalize_blanks(text: str) -> str:
    """Normalise every fill-in-the-blank run to a consistent ____ token."""
    return _BLANK_RUN.sub("____", text)


# --------------------------------------------------------------------------------------
# Roman numerals
# --------------------------------------------------------------------------------------

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_RE = re.compile(r"^(?=[IVXLCDM]+$)M*(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$", re.I)


def roman_to_int(roman: str) -> Optional[int]:
    """Strict roman numeral decode; returns None for anything malformed."""
    if not roman or not _ROMAN_RE.match(roman):
        return None
    upper = roman.upper()
    total, prev = 0, 0
    for ch in reversed(upper):
        val = _ROMAN_VALUES[ch]
        total = total - val if val < prev else total + val
        prev = max(prev, val)
    return total or None


def index_to_label(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (spreadsheet style, so we never run out of labels)."""
    if index < 1:
        return ""
    label = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        label = chr(65 + rem) + label
    return label


def label_to_index(label: str) -> Optional[int]:
    if not label or not label.isalpha():
        return None
    idx = 0
    for ch in label.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx


# --------------------------------------------------------------------------------------
# Question openers
# --------------------------------------------------------------------------------------
# (style, regex, number-group). Ordered most specific first.

# Style names encode the delimiter as well as the label kind, so "1." and "1)" are
# distinguishable conventions. That distinction is what lets a document use numbered
# questions and numbered options at the same time without the two colliding.
QUESTION_PATTERNS: List[Tuple[str, re.Pattern, int]] = [
    ("q_prefix",    re.compile(r"^Q(?:uestion|ues|n|\.)?\s*[-.:]?\s*(\d{1,4})\s*[\.\):\-–]\s*(.*)$", re.I), 1),
    ("q_prefix",    re.compile(r"^Q(?:uestion|ues|n)?\s*No\.?\s*[-.:]?\s*(\d{1,4})\s*[\.\):\-]?\s*(.*)$", re.I), 1),
    ("q_prefix",    re.compile(r"^Que\.?\s*[-.:]?\s*(\d{1,4})\s*[\.\):\-]?\s*(.*)$", re.I), 1),
    ("q_prefix",    re.compile(r"^Q\s*(\d{1,4})\s+(.*)$", re.I), 1),
    ("num_bracket", re.compile(r"^\((\d{1,4})\)\s*(.+)$"), 1),
    ("num_bracket", re.compile(r"^\[(\d{1,4})\]\s*(.+)$"), 1),
    ("num_bracket", re.compile(r"^(\d{1,4})\]\s*(.+)$"), 1),
    ("num_dot",     re.compile(r"^(\d{1,4})\s*\.\s+(.+)$"), 1),
    ("num_paren",   re.compile(r"^(\d{1,4})\s*\)\s+(.+)$"), 1),
    ("num_colon",   re.compile(r"^(\d{1,4})\s*:\s+(.+)$"), 1),
    ("num_dash",    re.compile(r"^(\d{1,4})\s*[-–]\s+(.+)$"), 1),
    ("roman_up",    re.compile(r"^([IVXLCDM]{1,7})\s*[\.\)]\s+(.+)$"), 1),
    # Lowercase roman numbering ("i)", "ii)", "iv)Sale") is the usual way True/False
    # worksheets number their statements, but it is also a very common *option* label,
    # so the engine only reads it as a question when the document gives it reason to —
    # see StyleProfile.true_false_statements. The paren form tolerates a missing space
    # because worksheets frequently run the label straight into the text; the dot form
    # insists on one, which keeps "i.e." out.
    ("roman_low",   re.compile(r"^([ivxlcdm]{1,7})\s*\)\s*(.+)$"), 1),
    ("roman_low",   re.compile(r"^([ivxlcdm]{1,7})\s*\.\s+(.+)$"), 1),
    ("alpha_paren", re.compile(r"^([a-z])\s*\)\s*(.+)$"), 1),
    ("num_bare",    re.compile(r"^(\d{1,4})\s+(.+)$"), 1),
]

# Question styles that are indistinguishable from ordinary option labels. They are only
# offered as question openers to a document that has proved it numbers *statements* this
# way — see ``match_question_openers(statement_labels=...)``.
STATEMENT_LABEL_QUESTION_STYLES = {"roman_low", "alpha_paren"}

# Words that make a line read as a question even with no number in front of it.
_INTERROGATIVE = (
    r"What|Which|Who|Whom|Whose|Where|When|Why|How|Explain|Describe|Define|Identify|"
    r"Determine|Calculate|Compute|Evaluate|Compare|Discuss|List|Name|State|Choose|Select|"
    r"Consider|Suppose|Given|Find|Solve|Match|Arrange|Fill|Write|Derive|Prove|Draw"
)
_AUXILIARY = r"Is|Are|Was|Were|Does|Do|Did|Has|Have|Had|Can|Could|Would|Should|Will|Shall|If"

STRONG_QUESTION_RE = re.compile(rf"^\s*(?:{_INTERROGATIVE})\b", re.I)
WEAK_QUESTION_RE = re.compile(rf"^\s*(?:{_AUXILIARY})\b", re.I)
# Stems that trail off and expect the options to complete them.
DANGLING_STEM_RE = re.compile(
    r"(?:is|are|was|were|means?|refers?\s+to|is\s+called|are\s+called|is\s+known\s+as|"
    r"denotes?|includes?|equals?|the\s+following|among\s+the\s+following|is\s+that|"
    r"is\s+when|will\s+be|would\s+be|can\s+be|should\s+be)\s*[:\-]?\s*$",
    re.I,
)


# --------------------------------------------------------------------------------------
# Option openers
# --------------------------------------------------------------------------------------
# (style, regex, decoder-kind). decoder-kind: alpha | num | roman | none

OPTION_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    ("paren_alpha",   re.compile(r"^\(([A-Za-z])\)\s*(.*)$"), "alpha"),
    ("bracket_alpha", re.compile(r"^\[([A-Za-z])\]\s*(.*)$"), "alpha"),
    ("paren_num",     re.compile(r"^\((\d{1,2})\)\s*(.*)$"), "num"),
    ("bracket_num",   re.compile(r"^\[(\d{1,2})\]\s*(.*)$"), "num"),
    ("word_alpha",    re.compile(r"^(?:Option|Choice|Opt|Ans)\s*[-\.]?\s*([A-Za-z])\s*[\.\):\-]\s*(.*)$", re.I), "alpha"),
    ("roman_low",     re.compile(r"^([ivxlcdm]{2,7})\s*[\.\)]\s+(.*)$"), "roman"),
    ("alpha_dot",     re.compile(r"^([A-Za-z])\s*\.\s+(.*)$"), "alpha"),
    ("alpha_dot",     re.compile(r"^([A-Za-z])\s*\.(\S.*)$"), "alpha"),
    ("alpha_paren",   re.compile(r"^([A-Za-z])\s*\)\s*(.*)$"), "alpha"),
    ("alpha_colon",   re.compile(r"^([A-Za-z])\s*:\s+(.*)$"), "alpha"),
    ("alpha_dash",    re.compile(r"^([A-Za-z])\s+[-–]\s+(.*)$"), "alpha"),
    ("alpha_arrow",   re.compile(r"^([A-Za-z])\s*(?:->|=>|→)\s*(.*)$"), "alpha"),
    # Single-letter roman numerals (i., v., x.) are also plain letters; keep them as a
    # separate style so a document that uses i/ii/iii resolves consistently.
    ("roman_low",     re.compile(r"^([ivx])\s*[\.\)]\s+(.*)$"), "roman"),
    ("num_dot",       re.compile(r"^0*(\d{1,2})\s*\.\s+(.*)$"), "num"),
    ("num_paren",     re.compile(r"^0*(\d{1,2})\s*\)\s+(.*)$"), "num"),
    ("checkbox",      re.compile(r"^[☐☑☒■□○●⚪⚫]\s*(.*)$"), "none"),
    ("bullet",        re.compile(r"^[•▪‣⁃·*+∙]\s+(.*)$"), "none"),
    ("dash_bullet",   re.compile(r"^[-–]\s+(.*)$"), "none"),
]

# Cues meaning "this option is the correct one".
CORRECT_MARKER_RE = re.compile(
    r"(?:^|\s)(?:\*{1,2}|✓|✔|✅|☞|<{2,}|\(correct\)|\[correct\]|"
    r"\(ans(?:wer)?\)|←)\s*$|^\s*(?:\*{1,2}|✓|✔|✅)\s+",
    re.I,
)


def strip_correct_marker(text: str) -> Tuple[str, bool]:
    """Remove a trailing/leading correctness marker; report whether one was present."""
    cleaned = CORRECT_MARKER_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return (cleaned, True) if cleaned != text.strip() else (text.strip(), False)


# --------------------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------------------

_ANSWER_WORD = r"(?:correct\s+answer|right\s+answer|correct\s+option|correct\s+choice|answer\s+key|answer|correct|solution|ans|key)"

# Multi-answer first: "Answer: A, C" / "Ans - B and D"
ANSWER_MULTI_RE = re.compile(
    rf"\b{_ANSWER_WORD}\s*(?:is|are)?\s*[:=\-–>\)]*\s*"
    r"\(?([A-Za-z])\)?\s*(?:,|/|&|\+|\band\b)\s*\(?([A-Za-z])\)?"
    r"(?:\s*(?:,|/|&|\+|\band\b)\s*\(?([A-Za-z])\)?)?",
    re.I,
)

ANSWER_BOOL_RE = re.compile(rf"\b{_ANSWER_WORD}\s*(?:is)?\s*[:=\-–>]*\s*(TRUE|FALSE|YES|NO|T|F)\b", re.I)

# True/False worksheets usually carry no "Answer:" label at all — the verdict simply
# trails the statement ("... is levied on inter-state supply.    True") or sits alone on
# the next line. Two tiers, because a bare word is weak evidence on its own:
#
#   TRAILING_BOOL_RE       strict — used to *detect* that a document follows this
#                          convention. Requires sentence punctuation or a wide gap
#                          before the verdict, so ordinary prose ending in "is true"
#                          is not mistaken for an answer.
#   TRAILING_BOOL_LOOSE_RE relaxed — applied only once the convention is established,
#                          for the statements that happen to end without punctuation.
#
# Extraction collapses runs of whitespace, so the punctuation branch is what does the
# work in practice; the wide-gap branch survives for sources that keep their tabs.
_BOOL_TAIL = r"[\(\[]?(?P<value>TRUE|FALSE)[\)\]]?\s*[\.\)]?\s*$"

TRAILING_BOOL_RE = re.compile(
    r"^(?P<stem>.*?)(?:(?<=[\.\?\!:;])\s+|\s{2,}|\t+)" + _BOOL_TAIL, re.I)

TRAILING_BOOL_LOOSE_RE = re.compile(r"^(?P<stem>.*?\w.*?)\s+" + _BOOL_TAIL, re.I)

# A line that is nothing but the verdict, for statements that wrap onto their own line.
BARE_BOOL_LINE_RE = re.compile(r"^\s*[\(\[]?(TRUE|FALSE)[\)\]]?\s*[\.\)]?\s*$", re.I)


def split_trailing_bool(text: str, loose: bool = False) -> Tuple[str, Optional[str]]:
    """Split ``text`` into its statement and a trailing TRUE/FALSE verdict.

    Returns ``(stem, token)``, with ``token`` None when there is no trailing verdict.
    A line that is *only* a verdict is left alone — that case is a standalone answer
    line, not a statement, and the caller handles it separately.
    """
    if not text or BARE_BOOL_LINE_RE.match(text):
        return text, None
    match = (TRAILING_BOOL_LOOSE_RE if loose else TRAILING_BOOL_RE).match(text)
    if not match:
        return text, None
    stem = match.group("stem").strip()
    if not stem:
        return text, None
    return stem, normalize_bool_token(match.group("value"))


ANSWER_SINGLE_RE = re.compile(
    rf"\b{_ANSWER_WORD}\s*(?:is|=)?\s*[:=\-–>\)]*\s*\(?\[?([A-Za-z])\]?\)?(?![A-Za-z])",
    re.I,
)

# "Answer: 24.5" / "Ans = Mumbai" — a free-text or numeric answer.
ANSWER_FREE_RE = re.compile(rf"\b{_ANSWER_WORD}\s*(?:is|=)?\s*[:=\-–>]\s*(.+)$", re.I)

# A line that is nothing but a key: "B." / "(C)" / "[3]"
ANSWER_STANDALONE_RE = re.compile(r"^[\(\[]?([A-Za-z0-9])[\)\]]?\s*[\.\)]?\s*$")

ANSWER_SECTION_RE = re.compile(
    r"^\s*(?:answer\s*key|answers?|solutions?|key|answer\s*sheet|correct\s*answers?)\s*[:\-]?\s*$",
    re.I,
)

# Grid entries: "Q1: c" / "1-B" / "1) A" / "12 . d" / "Q3: True".
# The value is captured loosely and filtered in parse_answer_grid, so True/False keys
# are picked up without letting arbitrary prose through.
GRID_ENTRY_RE = re.compile(
    r"(?:^|[\s,;|])(?:Q(?:uestion)?\.?\s*)?(\d{1,4})\s*[\.\):\-–=]\s*\(?([A-Za-z]{1,5})\)?(?![A-Za-z0-9])",
    re.I,
)

EXPLANATION_RE = re.compile(
    r"^\s*(?:explanation|rationale|reason|solution|justification|why|note|hint)\s*[:\-–]\s*(.+)$",
    re.I,
)

MARKS_RE = re.compile(r"[\(\[]\s*(\d+(?:\.\d+)?)\s*(?:marks?|mks?|pts?|points?)\s*[\)\]]", re.I)


# --------------------------------------------------------------------------------------
# Question-type cues
# --------------------------------------------------------------------------------------

TRUE_FALSE_STEM_RE = re.compile(
    r"[\(\[]?\s*\b(?:true|false)\s*(?:/|\s+or\s+|\s*\|\s*)\s*(?:true|false)\b\s*[\)\]]?|"
    r"\b(?:state|say|write|mark)\s+(?:whether|if)\b.*\b(?:true|false)\b|"
    r"\btrue\s+or\s+false\b",
    re.I,
)
TRUE_FALSE_CLEAN_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:true|false)\s*(?:/|\s+or\s+|\s*\|\s*)\s*(?:true|false)\b\s*[\)\]]?\s*",
    re.I,
)

MULTI_SELECT_RE = re.compile(
    r"\b(?:select|choose|mark|tick|pick)\s+(?:all\s+that\s+apply|any\s+two|any\s+three|"
    r"two|three|four|more\s+than\s+one|multiple)\b|\ball\s+that\s+apply\b|"
    r"\b(?:select|choose)\s+\d+\s+(?:options?|answers?|choices?)\b",
    re.I,
)

MATCHING_RE = re.compile(
    r"\bmatch\s+(?:the\s+)?(?:following|columns?|items?|pairs?|list)\b|"
    r"\bcolumn\s*[-\s]?(?:A|B|I|II|1|2)\b|\bmatch\s+list\s*[-\s]?I\b",
    re.I,
)

ASSERTION_RE = re.compile(r"\bassertion\s*[\(\[]?\s*A?\s*[\)\]]?\s*[:\-]", re.I)
REASON_RE = re.compile(r"\breason\s*[\(\[]?\s*R?\s*[\)\]]?\s*[:\-]", re.I)

ORDERING_RE = re.compile(
    r"\b(?:arrange|rearrange|order|sequence|rank)\b.*\b(?:order|sequence|chronolog|ascending|descending)\b|"
    r"\bcorrect\s+(?:order|sequence)\b|\bin\s+the\s+correct\s+order\b",
    re.I,
)

FILL_BLANK_RE = re.compile(r"_{3,}|\bfill\s+in\s+the\s+blanks?\b|\bfill\s+up\s+the\s+blanks?\b", re.I)

NUMERIC_ANSWER_RE = re.compile(r"^[-+]?\d+(?:[.,]\d+)*\s*(?:%|[a-zA-Z/²³°]{1,8})?$")

DIRECTIVE_RE = re.compile(
    r"^\s*(?:directions?|instructions?|note|read\s+the\s+following|"
    r"answer\s+the\s+following|choose\s+the\s+correct|attempt\s+(?:all|any)|"
    r"section\s+[-\w]+|part\s+[-\w]+|time\s+allowed|maximum\s+marks|total\s+marks)\b",
    re.I,
)

# Page furniture we should drop outright.
NOISE_RE = re.compile(
    r"^\s*(?:page\s*\d+(?:\s*(?:of|/)\s*\d+)?|-\s*\d+\s*-|\d+\s*\|\s*p\s*a\s*g\s*e|"
    r"[─-╿=_\-\*~]{4,}|confidential|copyright.*|©.*)\s*$",
    re.I,
)

TRUE_TOKENS = {"TRUE", "T", "YES", "Y", "CORRECT", "RIGHT"}
FALSE_TOKENS = {"FALSE", "F", "NO", "N", "INCORRECT", "WRONG"}


# --------------------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------------------

def match_question_openers(text: str, statement_labels: bool = False
                           ) -> List[Tuple[str, Optional[int], str]]:
    """Every way this line could be read as a question opener, most specific first.

    Returning all candidates rather than the first match lets the engine choose the
    reading that agrees with the document's established convention.

    ``statement_labels`` admits the styles that are otherwise pure option labels
    ("i)", "a)"). Only a True/False worksheet, which numbers statements that way and
    has no choices at all, should pass True.
    """
    found: List[Tuple[str, Optional[int], str]] = []
    seen = set()
    for style, regex, num_group in QUESTION_PATTERNS:
        if not statement_labels and style in STATEMENT_LABEL_QUESTION_STYLES:
            continue
        m = regex.match(text)
        if not m:
            continue
        raw = m.group(num_group)
        rest = m.group(num_group + 1).strip() if m.lastindex and m.lastindex > num_group else ""
        if style in ("roman_up", "roman_low"):
            num = roman_to_int(raw)
            # Cap at XXX: a bare "C." or "D." is far more likely an option label
            # than question number 100 or 500. This also rejects letter runs that
            # merely happen to use roman characters, such as "civil" or "did".
            if num is None or num > 30:
                continue
        elif style == "alpha_paren":
            num = label_to_index(raw)   # "a)" is statement 1, "b)" statement 2, ...
            if num is None:
                continue
        else:
            try:
                num = int(raw)
            except ValueError:
                continue
        if num is not None and num > 2000:
            continue  # almost certainly a year, not a question number
        if style in seen:
            continue
        seen.add(style)
        found.append((style, num, rest))
    return found


def match_question_opener(text: str) -> Optional[Tuple[str, Optional[int], str]]:
    candidates = match_question_openers(text)
    return candidates[0] if candidates else None


def match_option_openers(text: str) -> List[Tuple[str, Optional[int], str]]:
    """Every way this line could be read as an option, most specific first."""
    found: List[Tuple[str, Optional[int], str]] = []
    seen = set()
    for style, regex, kind in OPTION_PATTERNS:
        m = regex.match(text)
        if not m:
            continue
        if kind == "none":
            body = m.group(1).strip()
            if not body or style in seen:
                continue
            seen.add(style)
            found.append((style, None, body))
            continue
        raw = m.group(1)
        body = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
        if kind == "alpha":
            idx = label_to_index(raw)
        elif kind == "num":
            try:
                idx = int(raw)
            except ValueError:
                continue
        else:  # roman
            idx = roman_to_int(raw)
        if not idx or idx > 26 or style in seen:
            continue
        seen.add(style)
        found.append((style, idx, body))
    return found


def match_option_opener(text: str) -> Optional[Tuple[str, Optional[int], str]]:
    candidates = match_option_openers(text)
    return candidates[0] if candidates else None


def parse_answer_grid(text: str) -> List[Tuple[int, str]]:
    """Pull every `<number> -> <answer>` pairing out of a line.

    Accepts single letters (``Q1: c``) and boolean words (``Q3: True``); anything
    else is discarded so ordinary prose does not register as an answer key.
    """
    pairs: List[Tuple[int, str]] = []
    seen = set()
    for m in GRID_ENTRY_RE.finditer(text):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        token = m.group(2)
        if len(token) == 1:
            value = token.upper()
        else:
            value = normalize_bool_token(token)
            if value is None:
                continue
        if num in seen:
            continue
        seen.add(num)
        pairs.append((num, value))
    return pairs


def normalize_bool_token(token: str) -> Optional[str]:
    upper = token.strip().upper().rstrip(".")
    if upper in TRUE_TOKENS:
        return "TRUE"
    if upper in FALSE_TOKENS:
        return "FALSE"
    return None
