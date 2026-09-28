"""Optional AI assist for quiz text the rule engine cannot confidently parse.

Any provider in :mod:`quizify.providers` can serve the request. Claude goes through
the official Anthropic SDK; everything else uses the shared OpenAI-compatible
transport. Providers are tried in order and a failure falls through to the next, so
a rate-limited free tier degrades into the next option rather than into an error.

Nothing here is required — with nothing configured the engine runs on rules alone.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .models import AnswerSource, Option, Question, QuestionType
from . import config
from . import providers as prov
from .providers import Provider, ProviderError

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:  # pragma: no cover
    ANTHROPIC_OK = False


def max_chunks() -> int:
    return config.get_int("QUIZ_AI_MAX_CHUNKS", 12)

_TYPE_VALUES = [t.value for t in QuestionType]

QUESTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question stem, verbatim, without its number prefix.",
                    },
                    "type": {"type": "string", "enum": _TYPE_VALUES},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option texts in order, without A./B./C. labels. Empty if none.",
                    },
                    "answer": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Correct option labels (A, B, ...) or TRUE/FALSE. Empty if unknown.",
                    },
                    "answer_text": {
                        "type": "string",
                        "description": "Free-text or numeric answer when it is not an option label. Empty otherwise.",
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["question", "type", "options", "answer", "answer_text", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract quiz questions from documents into structured data.

Rules:
- Transcribe question and option text exactly as written. Preserve numbers, symbols, units, formulas, and capitalisation. Never paraphrase, translate, or fix typos.
- Strip only the enumeration prefix (Q1., 1., (a), A), i., bullets) — keep everything after it.
- `options` holds option text in document order with labels removed. Use an empty array for questions that have no choices.
- `answer` holds the correct option labels as letters (A = first option, B = second, and so on), or TRUE / FALSE. Use an empty array when the document does not state an answer. Never guess an answer that is not present in the source.
- Use `answer_text` for answers that are not option labels (a number, a word, a phrase).
- Multi-line stems and multi-line options should be joined into a single line.
- A question stem that continues into its options (a sentence completed by each choice) stays as the stem.
- Merge a question with its options even when they are separated by page breaks, headers, or blank lines.
- Do not invent questions. If a region contains instructions, headings, or page furniture rather than questions, return nothing for it."""

# Weaker models need the shape spelled out; schema enforcement alone is not enough.
JSON_INSTRUCTION = """
Reply with a single JSON object and nothing else — no prose, no markdown fences:
{"questions":[{"question":"...","type":"MCQ","options":["...","..."],"answer":["B"],"answer_text":"","explanation":""}]}
`type` must be one of: """ + ", ".join(_TYPE_VALUES)


class AIUnavailable(Exception):
    """Raised when no provider is configured, or every provider failed."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def claude_available() -> bool:
    return ANTHROPIC_OK and bool(
        config.get("ANTHROPIC_API_KEY") or config.get("ANTHROPIC_AUTH_TOKEN")
    )


def available_providers() -> List[Provider]:
    """Configured providers, best first, minus Claude if its SDK is missing."""
    return [p for p in prov.available() if p.kind != "anthropic" or ANTHROPIC_OK]


def backend_name() -> Optional[str]:
    chain = available_providers()
    return chain[0].name if chain else None


def describe_providers() -> List[Dict[str, Any]]:
    rows = prov.describe()
    for row in rows:
        if row["name"] == "anthropic" and not ANTHROPIC_OK:
            row["configured"] = False
            row["note"] = "install the 'anthropic' package"
    return rows


# --------------------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------------------

_BOUNDARY_RE = re.compile(r"^\s*(?:Q(?:uestion|ues)?\.?\s*)?\d{1,4}\s*[\.\):\-]", re.I)


def chunk_text(text: str, limit: int = 12_000) -> List[str]:
    """Split long documents on question boundaries so no question is cut in half."""
    lines = text.split("\n")
    chunks: List[str] = []
    buffer: List[str] = []
    size = 0

    for line in lines:
        if size + len(line) > limit and buffer and _BOUNDARY_RE.match(line):
            chunks.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1

    if buffer:
        chunks.append("\n".join(buffer))

    # A document with no recognisable boundaries still has to be split somewhere.
    if len(chunks) == 1 and len(chunks[0]) > limit * 1.5:
        body = chunks[0]
        chunks = [body[i:i + limit] for i in range(0, len(body), limit)]

    return chunks[:max_chunks()]


# --------------------------------------------------------------------------------------
# JSON recovery
# --------------------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from a model reply.

    Handles clean JSON, markdown fences, JSON with prose around it, and output that
    was cut off mid-object by a token limit — common on free tiers.
    """
    if not text:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"questions": parsed}
    except json.JSONDecodeError:
        pass

    start = body.find("{")
    if start < 0:
        return None
    body = body[start:]

    stack: List[str] = []
    in_string = escaped = False
    complete_at: List[Tuple[int, List[str]]] = []

    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
            if not stack:
                try:
                    parsed = json.loads(body[:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    break
            else:
                complete_at.append((index + 1, list(stack)))

    # Truncated output: close the structure after the last complete element.
    for end, remaining in reversed(complete_at):
        candidate = body[:end] + "".join(reversed(remaining))
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------------------
# Response normalisation
# --------------------------------------------------------------------------------------

def _first(data: Dict[str, Any], *keys: str) -> Any:
    lowered = {str(k).lower().replace(" ", "_"): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _questions_from(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Locate the question list wherever the model chose to put it."""
    for key in ("questions", "items", "data", "results", "quiz", "output"):
        value = payload.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    # A single question returned bare.
    if any(k in payload for k in ("question", "text", "stem", "prompt")):
        return [payload]
    return []


def _option_texts(raw: Any) -> Tuple[List[str], List[int]]:
    """Normalise an options value into texts plus any indices flagged correct."""
    texts: List[str] = []
    correct: List[int] = []
    if isinstance(raw, dict):
        raw = [raw[k] for k in sorted(raw)]
    if not isinstance(raw, list):
        return texts, correct

    for item in raw:
        if isinstance(item, dict):
            body = _first(item, "text", "option", "label", "value", "content", "answer")
            if body is None:
                continue
            if item.get("correct") or item.get("is_correct") or item.get("isCorrect"):
                correct.append(len(texts))
            texts.append(str(body).strip())
        elif item is not None:
            texts.append(str(item).strip())

    # Models sometimes leave the label in place; strip it so labels stay consistent.
    cleaned = []
    for text in texts:
        cleaned.append(re.sub(r"^\s*[\(\[]?[A-Za-z0-9][\)\].:]\s+", "", text).strip())
    return [t for t in cleaned if t], correct


def _answer_labels(raw: Any, options: List[Option]) -> Tuple[List[str], Optional[str]]:
    """Interpret whatever the model returned as an answer."""
    if raw is None:
        return [], None
    values = raw if isinstance(raw, list) else [raw]
    labels: List[str] = []
    leftover: List[str] = []
    valid = {o.label for o in options}

    for value in values:
        if isinstance(value, bool):
            labels.append("TRUE" if value else "FALSE")
            continue
        if isinstance(value, int):
            # Some models index from 0, others from 1; prefer the 1-based reading.
            for index in (value, value + 1):
                if 1 <= index <= len(options):
                    labels.append(options[index - 1].label)
                    break
            continue
        token = str(value).strip()
        if not token:
            continue
        upper = token.upper().strip(" .()[]")
        if upper in ("TRUE", "FALSE", "YES", "NO", "T", "F"):
            labels.append("TRUE" if upper in ("TRUE", "YES", "T") else "FALSE")
        elif len(upper) == 1 and upper in valid:
            labels.append(upper)
        elif upper.isdigit() and 1 <= int(upper) <= len(options):
            labels.append(options[int(upper) - 1].label)
        else:
            # The answer may be the option's text rather than its label.
            normalised = re.sub(r"[^a-z0-9]+", "", token.lower())
            match = [o.label for o in options
                     if re.sub(r"[^a-z0-9]+", "", o.text.lower()) == normalised]
            if len(match) == 1:
                labels.append(match[0])
            else:
                leftover.append(token)

    labels = list(dict.fromkeys(labels))
    return labels, (leftover[0] if leftover and not labels else None)


def _coerce(items: List[Dict[str, Any]], engine: str) -> List[Question]:
    """Turn raw provider output into Question objects, tolerating loose shapes."""
    questions: List[Question] = []
    for position, item in enumerate(items, 1):
        stem = _first(item, "question", "text", "stem", "prompt", "title", "q")
        stem = str(stem).strip() if stem is not None else ""
        if not stem:
            continue

        texts, flagged = _option_texts(
            _first(item, "options", "choices", "alternatives", "answers", "option_list"))
        options = [Option(label=chr(64 + i), text=text)
                   for i, text in enumerate(texts[:26], 1)]

        labels, answer_text = _answer_labels(
            _first(item, "answer", "correct", "correct_answer", "correct_option",
                   "correct_answers", "key", "ans", "solution"),
            options,
        )
        # An options array that flagged its own correct entries is authoritative.
        if not labels and flagged:
            labels = [chr(65 + i) for i in flagged if i < len(options)]

        for option in options:
            option.correct = option.label in labels

        explanation = _first(item, "explanation", "rationale", "reason", "justification")
        explanation = str(explanation).strip() if explanation else None

        raw_type = _first(item, "type", "question_type", "qtype", "format")
        try:
            qtype = QuestionType(str(raw_type).strip())
        except (ValueError, TypeError):
            qtype = QuestionType.MCQ_SINGLE if options else QuestionType.SHORT_ANSWER

        question = Question(
            text=stem,
            options=options,
            qtype=qtype,
            number=position,
            answer_labels=labels,
            answer_text=str(answer_text).strip() if answer_text else None,
            explanation=explanation,
            answer_source=AnswerSource.INLINE if (labels or answer_text) else AnswerSource.NONE,
            confidence=0.88 if (labels or answer_text) else 0.72,
            engine=engine,
        )
        if not question.answer_display:
            question.warnings.append("No answer found.")
        questions.append(question)
    return questions


# --------------------------------------------------------------------------------------
# Claude backend (official Anthropic SDK)
# --------------------------------------------------------------------------------------

def _call_claude(client: "anthropic.Anthropic", provider: Provider,
                 chunk: str, context: str) -> Dict[str, Any]:
    request: Dict[str, Any] = {
        "model": provider.model(),
        "max_tokens": provider.max_tokens,
        "system": [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": QUESTION_SCHEMA},
        },
        "messages": [{
            "role": "user",
            "content": f"{context}Extract every quiz question from the text below.\n\n"
                       f"<document>\n{chunk}\n</document>",
        }],
    }

    # Richest request first, stepping down so an older SDK or an account without a
    # given beta degrades instead of failing the parse.
    attempts = [
        lambda: client.beta.messages.stream(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request),
        lambda: client.messages.stream(**request),
        lambda: client.messages.stream(**{**request, "output_config": {"effort": "medium"}}),
    ]

    message = None
    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            with attempt() as stream:
                message = stream.get_final_message()
            break
        except (anthropic.BadRequestError, TypeError, ValueError) as exc:
            last_error = exc
    if message is None:
        raise ProviderError(f"Claude rejected the request ({last_error}).", provider="anthropic")

    if message.stop_reason == "refusal":
        raise ProviderError("Claude declined to process this content.", provider="anthropic")

    text = next((b.text for b in message.content if b.type == "text"), "")
    payload = _extract_json(text)
    if payload is None:
        raise ProviderError("Claude returned a response that could not be read.",
                            provider="anthropic")
    return payload


def _run_claude(provider: Provider, chunks: List[str], context: str) -> List[Dict[str, Any]]:
    client = anthropic.Anthropic()
    collected: List[Dict[str, Any]] = []
    for chunk in chunks:
        collected.extend(_questions_from(_call_claude(client, provider, chunk, context)))
    return collected


# --------------------------------------------------------------------------------------
# OpenAI-compatible backends
# --------------------------------------------------------------------------------------

def _run_openai(provider: Provider, chunks: List[str], context: str) -> List[Dict[str, Any]]:
    system = SYSTEM_PROMPT + "\n" + JSON_INSTRUCTION
    collected: List[Dict[str, Any]] = []
    model_retried = False

    for chunk in chunks:
        user = (f"{context}Extract every quiz question from the text below.\n\n"
                f"<document>\n{chunk}\n</document>")
        try:
            content = prov.complete(provider, system, user, schema=QUESTION_SCHEMA)
        except ProviderError as exc:
            # A stale free-model id is the most common OpenRouter failure; ask the
            # provider what it currently offers and retry once.
            if model_retried or not _looks_like_bad_model(exc):
                raise
            model_retried = True
            replacement = prov.pick_free_model(provider)
            if not replacement:
                raise
            config.update_overlay(f"QUIZ_MODEL_{provider.name.upper()}", replacement)
            content = prov.complete(provider, system, user, schema=QUESTION_SCHEMA)

        payload = _extract_json(content)
        if payload is None:
            raise ProviderError(f"{provider.label} returned unreadable output.",
                                provider=provider.name)
        collected.extend(_questions_from(payload))
    return collected


def _looks_like_bad_model(error: ProviderError) -> bool:
    message = str(error).lower()
    return any(word in message for word in
               ("model", "not found", "404", "no endpoints", "unavailable", "deprecated"))


# --------------------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------------------

def _chain(preferred: Optional[str]) -> List[Provider]:
    chain = available_providers()
    if not preferred:
        return chain
    wanted = preferred.strip().lower()
    chosen = [p for p in chain if p.name == wanted]
    if not chosen:
        raise AIUnavailable(
            f"Provider '{preferred}' is not configured. "
            f"Available: {', '.join(p.name for p in chain) or 'none'}."
        )
    # Named provider first, then the rest as fallback.
    return chosen + [p for p in chain if p.name != wanted]


def parse_with_ai(text: str, subject: str = "", topic: str = "",
                  provider: Optional[str] = None,
                  examples: str = "") -> Tuple[List[Question], str]:
    """Parse quiz text with the first provider that succeeds.

    ``examples`` is optional prompt text showing questions a human corrected before —
    see :mod:`quizify.learning`. It teaches the model this library's conventions
    without any fine-tuning.

    Returns ``(questions, provider_name)``. Raises :class:`AIUnavailable` when nothing
    is configured or every provider in the chain failed.
    """
    # A companion persona would answer in character rather than return the JSON the
    # parser expects, so chat-only providers never take part in extraction.
    chain = [p for p in _chain(provider) if not p.chat_only]
    if not chain:
        raise AIUnavailable(
            "No AI provider is configured. Set one of ANTHROPIC_API_KEY, "
            "OPENROUTER_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, or run Ollama locally."
        )

    context = ""
    if subject or topic:
        context = (f"Subject: {subject or 'unspecified'}\n"
                   f"Topic: {topic or 'unspecified'}\n\n")
    if examples:
        context += examples

    failures: List[str] = []
    for candidate in chain:
        chunks = [c for c in chunk_text(text, candidate.max_chars) if c.strip()]
        if not chunks:
            break
        try:
            if candidate.kind == "anthropic":
                items = _run_claude(candidate, chunks, context)
            else:
                items = _run_openai(candidate, chunks, context)
        except ProviderError as exc:
            failures.append(str(exc))
            continue
        except Exception as exc:  # unexpected client-side failure — try the next one
            failures.append(f"{candidate.label}: {type(exc).__name__}: {exc}")
            continue

        questions = _coerce(items, candidate.name)
        if questions:
            return questions, candidate.name
        failures.append(f"{candidate.label} returned no questions.")

    raise AIUnavailable("All AI providers failed. " + " | ".join(failures[:3]))


def chat(message: str, system: str, provider: Optional[str] = None) -> Tuple[str, str]:
    """Single-turn chat used by the help assistant. Returns ``(reply, provider_name)``."""
    chain = _chain(provider)
    if not chain:
        raise AIUnavailable("No AI provider is configured.")

    failures: List[str] = []
    for candidate in chain:
        try:
            if candidate.kind == "messengerx":
                from . import messengerx
                reply = messengerx.chat(message)
            elif candidate.kind == "anthropic":
                client = anthropic.Anthropic()
                response = client.messages.create(
                    model=candidate.model(),
                    max_tokens=1024,
                    system=system,
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": message}],
                )
                if response.stop_reason == "refusal":
                    failures.append("Claude declined to answer.")
                    continue
                reply = next((b.text for b in response.content if b.type == "text"), "")
            else:
                reply = prov.complete(candidate, system, message, schema=None,
                                      max_tokens=600, json_mode=False)
                reply = _plain_text(reply)
            if reply.strip():
                return reply.strip(), candidate.name
            failures.append(f"{candidate.label} returned an empty reply.")
        except Exception as exc:
            failures.append(f"{candidate.label}: {exc}")

    raise AIUnavailable("All AI providers failed. " + " | ".join(failures[:3]))


def _plain_text(reply: str) -> str:
    """Providers forced into JSON mode may wrap a chat reply in an object."""
    stripped = reply.strip()
    if not stripped.startswith("{"):
        return reply
    payload = _extract_json(stripped)
    if isinstance(payload, dict):
        for key in ("reply", "answer", "response", "text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return reply
