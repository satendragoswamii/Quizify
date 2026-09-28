"""Provider-layer tests.

Run with:  python tests/test_providers.py

Uses a stub HTTP transport, so these tests make no network calls and need no API
keys. They cover provider detection and ordering, failover, output-format
step-down, JSON recovery from messy or truncated replies, and the loose response
shapes that weaker free models tend to produce.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "QUIZ_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="quizify-prov-test-"), "test.db"))

# Clear inherited config so tests are deterministic
for k in list(os.environ):
    if k.startswith(("QUIZ_", "ANTHROPIC_", "OPENROUTER_", "GROQ_", "GEMINI_",
                     "GOOGLE_API", "DEEPSEEK_", "MISTRAL_", "TOGETHER_", "CEREBRAS_",
                     "OPENCODE_", "OLLAMA_")):
        del os.environ[k]

from quizify import providers as prov
from quizify import ai

PASS, FAIL = [], []
def ok(n): PASS.append(n)
def bad(n, m): FAIL.append(f"{n}: {m}")


class FakeResponse:
    def __init__(self, status, payload, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload)
    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RealExceptions.RequestException(f"HTTP {self.status_code}")


class Recorder:
    """Stands in for requests.post; scripted per-call responses."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "body": json or {}})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeResponse(200, _chat("{}"))


def _chat(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


QUIZ_JSON = json.dumps({"questions": [
    {"question": "Capital of France?", "type": "MCQ",
     "options": ["Berlin", "Paris", "Rome"], "answer": ["B"],
     "answer_text": "", "explanation": "It is Paris."},
]})


def install(script):
    rec = Recorder(script)
    prov.requests = types.SimpleNamespace(
        post=rec, get=lambda *a, **k: FakeResponse(200, {"data": []}),
        exceptions=RealExceptions)
    return rec


class RealExceptions:
    import requests as _r
    Timeout = _r.exceptions.Timeout
    RequestException = _r.exceptions.RequestException


# ---------- 1. provider detection ----------
os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
chain = ai.available_providers()
expect_names = [p.name for p in chain]
if expect_names == ["openrouter"]:
    ok("detects a single configured provider")
else:
    bad("detects a single configured provider", expect_names)

os.environ["GROQ_API_KEY"] = "gsk-test"
os.environ["GEMINI_API_KEY"] = "gem-test"
names = [p.name for p in ai.available_providers()]
if names[0] == "openrouter" and set(names) == {"openrouter", "groq", "gemini"}:
    ok("orders providers by quality")
else:
    bad("orders providers by quality", names)

if "ollama" not in [p.name for p in ai.available_providers()]:
    ok("keyless local provider stays off until opted in")
else:
    bad("keyless local provider stays off until opted in", "ollama listed")

os.environ["QUIZ_ENABLE_OLLAMA"] = "true"
if "ollama" in [p.name for p in ai.available_providers()]:
    ok("opting in enables the local provider")
else:
    bad("opting in enables the local provider", [p.name for p in ai.available_providers()])
del os.environ["QUIZ_ENABLE_OLLAMA"]

os.environ["QUIZ_AI_PROVIDERS"] = "groq,openrouter"
names = [p.name for p in ai.available_providers()]
if names == ["groq", "openrouter"]:
    ok("QUIZ_AI_PROVIDERS overrides order")
else:
    bad("QUIZ_AI_PROVIDERS overrides order", names)
del os.environ["QUIZ_AI_PROVIDERS"]

# ---------- 2. happy path ----------
rec = install([FakeResponse(200, _chat(QUIZ_JSON))])
qs, used = ai.parse_with_ai("Q1. Capital of France?\nA. Berlin\nB. Paris\nC. Rome")
if used == "openrouter" and len(qs) == 1 and qs[0].answer_display == "B" and qs[0].options[1].correct:
    ok("openai-compatible happy path")
else:
    bad("openai-compatible happy path", f"{used} {[ (q.text,q.answer_display) for q in qs]}")

body = rec.calls[0]["body"]
if body["model"] == "meta-llama/llama-3.3-70b-instruct:free" and body["temperature"] == 0:
    ok("uses configured model")
else:
    bad("uses configured model", body.get("model"))
if rec.calls[0]["headers"].get("Authorization") == "Bearer sk-or-test":
    ok("sends bearer auth")
else:
    bad("sends bearer auth", rec.calls[0]["headers"])
if "openrouter.ai/api/v1/chat/completions" in rec.calls[0]["url"]:
    ok("correct endpoint URL")
else:
    bad("correct endpoint URL", rec.calls[0]["url"])
if rec.calls[0]["headers"].get("X-Title") == "Quizify":
    ok("provider-specific headers sent")
else:
    bad("provider-specific headers sent", rec.calls[0]["headers"])

# ---------- 3. failover between providers ----------
prov.MAX_RETRIES = 2
prov.time = types.SimpleNamespace(sleep=lambda *_: None)  # skip backoff in tests

os.environ["QUIZ_AI_PROVIDERS"] = "openrouter,groq"
rec = install([
    FakeResponse(429, {"error": {"message": "rate limit"}}),   # openrouter attempt 1
    FakeResponse(429, {"error": {"message": "rate limit"}}),   # retry 1
    FakeResponse(429, {"error": {"message": "rate limit"}}),   # retry 2
    FakeResponse(200, _chat(QUIZ_JSON)),                       # groq succeeds
])
qs, used = ai.parse_with_ai("Q1. Capital of France?\nA. Berlin\nB. Paris")
if used == "groq" and len(qs) == 1:
    ok("fails over to the next provider on 429")
else:
    bad("fails over to the next provider on 429", f"{used} {len(qs)}")
del os.environ["QUIZ_AI_PROVIDERS"]

# ---------- 4. response_format step-down ----------
os.environ["QUIZ_AI_PROVIDERS"] = "openrouter"
rec = install([
    FakeResponse(400, {"error": {"message": "response_format json_schema not supported"}}),
    FakeResponse(200, _chat(QUIZ_JSON)),
])
qs, used = ai.parse_with_ai("Q1. Capital?\nA. x\nB. y")
formats = [c["body"].get("response_format", {}).get("type") for c in rec.calls]
if len(qs) == 1 and formats == ["json_schema", "json_object"]:
    ok("steps down from json_schema to json_object")
else:
    bad("steps down from json_schema to json_object", f"{formats} {len(qs)}")

# ---------- 5. truncated JSON repair ----------
truncated = '{"questions":[{"question":"A?","type":"MCQ","options":["x","y"],"answer":["A"],"answer_text":"","explanation":""},{"question":"B?","type":"MCQ","opt'
rec = install([FakeResponse(200, _chat(truncated))])
qs, used = ai.parse_with_ai("Q1. A?\nA. x\nB. y")
if len(qs) == 1 and qs[0].text == "A?":
    ok("recovers truncated JSON")
else:
    bad("recovers truncated JSON", f"{len(qs)} {[q.text for q in qs]}")

# ---------- 6. markdown fences + prose ----------
messy = "Sure! Here is the JSON:\n```json\n" + QUIZ_JSON + "\n```\nHope that helps."
rec = install([FakeResponse(200, _chat(messy))])
qs, _ = ai.parse_with_ai("Q1. Capital?\nA. x\nB. y")
if len(qs) == 1 and qs[0].answer_display == "B":
    ok("strips fences and surrounding prose")
else:
    bad("strips fences and surrounding prose", f"{len(qs)}")

# ---------- 7. loose response shapes from weaker models ----------
loose = json.dumps({"questions": [
    # answer as bare string, options as dicts with correct flags
    {"question": "Q one?", "options": [{"text": "aa", "correct": True}, {"text": "bb"}],
     "answer": "A"},
    # answer as 1-based int, alternate key names
    {"text": "Q two?", "choices": ["cc", "dd", "ee"], "correct_answer": 3,
     "rationale": "because"},
    # answer given as the option's text
    {"question": "Q three?", "options": ["Mercury", "Venus"], "answer": "Venus"},
    # labels left on the option text
    {"question": "Q four?", "options": ["A. one", "B. two"], "answer": ["B"]},
    # boolean answer
    {"question": "Q five?", "options": ["TRUE", "FALSE"], "answer": False},
]})
rec = install([FakeResponse(200, _chat(loose))])
qs, _ = ai.parse_with_ai("Q1. x?\nA. a\nB. b")
got = [(q.text, [o.text for o in q.options], q.answer_display) for q in qs]
want = [
    ("Q one?", ["aa", "bb"], "A"),
    ("Q two?", ["cc", "dd", "ee"], "C"),
    ("Q three?", ["Mercury", "Venus"], "B"),
    ("Q four?", ["one", "two"], "B"),
    ("Q five?", ["TRUE", "FALSE"], "FALSE"),
]
if got == want:
    ok("normalises loose response shapes")
else:
    bad("normalises loose response shapes", f"\n      got  {got}\n      want {want}")
if qs[1].explanation == "because":
    ok("alternate explanation key")
else:
    bad("alternate explanation key", qs[1].explanation)

# ---------- 8. stale free model self-heals ----------
os.environ["QUIZ_MODEL_OPENROUTER"] = "some/removed-model:free"
rec = Recorder([
    FakeResponse(404, {"error": {"message": "model not found"}}),
    FakeResponse(200, _chat(QUIZ_JSON)),
])
prov.requests = types.SimpleNamespace(
    post=rec,
    get=lambda *a, **k: FakeResponse(200, {"data": [
        {"id": "cheap/model", "pricing": {"prompt": "0.5", "completion": "1"}, "context_length": 8000},
        {"id": "good/model:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 64000},
        {"id": "small/model:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 8000},
    ]}),
    exceptions=RealExceptions)
qs, _ = ai.parse_with_ai("Q1. Capital?\nA. x\nB. y")
models_tried = [c["body"]["model"] for c in rec.calls]
if len(qs) == 1 and models_tried == ["some/removed-model:free", "good/model:free"]:
    ok("recovers from a stale free-model id")
else:
    bad("recovers from a stale free-model id", f"{models_tried} qs={len(qs)}")
del os.environ["QUIZ_MODEL_OPENROUTER"]

# ---------- 9. all providers fail ----------
rec = install([FakeResponse(500, {"error": {"message": "boom"}})] * 9)
try:
    ai.parse_with_ai("Q1. x?\nA. a\nB. b")
    bad("raises when every provider fails", "no exception")
except ai.AIUnavailable as e:
    ok("raises when every provider fails")
except Exception as e:
    bad("raises when every provider fails", f"{type(e).__name__}: {e}")

# ---------- 10. unknown provider name ----------
try:
    ai.parse_with_ai("x", provider="nope")
    bad("rejects unknown provider name", "no exception")
except ai.AIUnavailable as e:
    ok("rejects unknown provider name" if "not configured" in str(e) else "x")

# ---------- 11. chat path is not JSON-constrained ----------
rec = install([FakeResponse(200, _chat("Upload a file, then click Process."))])
reply, used = ai.chat("How do I use this?", "You are helpful.")
if reply.startswith("Upload a file") and "response_format" not in rec.calls[0]["body"]:
    ok("chat replies are not forced into JSON mode")
else:
    bad("chat replies are not forced into JSON mode",
        f"{reply!r} fmt={rec.calls[0]['body'].get('response_format')}")

# ---------- 12. chat unwraps a JSON-wrapped reply ----------
rec = install([FakeResponse(200, _chat('{"reply": "Just paste your quiz."}'))])
reply, _ = ai.chat("hi", "sys")
if reply == "Just paste your quiz.":
    ok("chat unwraps a JSON-wrapped reply")
else:
    bad("chat unwraps a JSON-wrapped reply", repr(reply))

# ---------- 13. tool_call arguments carry the JSON ----------
rec = install([FakeResponse(200, {"choices": [{"message": {
    "content": None, "tool_calls": [{"function": {"name": "extract", "arguments": QUIZ_JSON}}]}}]})])
qs, _ = ai.parse_with_ai("Q1. x?\nA. a\nB. b")
if len(qs) == 1 and qs[0].answer_display == "B":
    ok("reads JSON from a tool call")
else:
    bad("reads JSON from a tool call", len(qs))

# ---------- 14. no providers at all ----------
for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(k, None)
if ai.backend_name() is None:
    ok("reports no backend when nothing configured")
else:
    bad("reports no backend when nothing configured", ai.backend_name())
try:
    ai.parse_with_ai("x")
    bad("raises with no providers", "no exception")
except ai.AIUnavailable:
    ok("raises with no providers")

# ---------- 15. legacy QUIZ_API_* still recognised ----------
os.environ.pop("QUIZ_AI_PROVIDERS", None)
os.environ["QUIZ_API_ENDPOINT"] = "https://api.opencode.ai/v1/chat/completions"
os.environ["QUIZ_API_KEY"] = "oc-test"
os.environ["QUIZ_API_MODEL"] = "opencode"
names = [p.name for p in ai.available_providers()]
if "opencode" in names:
    ok("legacy QUIZ_API_* maps onto a known provider")
else:
    bad("legacy QUIZ_API_* maps onto a known provider", names)

os.environ["QUIZ_API_ENDPOINT"] = "https://my-llm.internal/v1"
names = [p.name for p in ai.available_providers()]
if "custom" in names:
    ok("unknown legacy endpoint becomes the custom provider")
else:
    bad("unknown legacy endpoint becomes the custom provider", names)

# ---------- 16. describe() never leaks keys ----------
os.environ["OPENROUTER_API_KEY"] = "sk-or-SECRET"
blob = json.dumps(ai.describe_providers())
if "SECRET" not in blob and "oc-test" not in blob:
    ok("provider description omits key material")
else:
    bad("provider description omits key material", "key leaked")

print(f"\n{'='*70}\nPASS: {len(PASS)}   FAIL: {len(FAIL)}\n{'='*70}")
for p in PASS: print(f"  ok   {p}")
if FAIL:
    print()
    for f in FAIL: print(f"  FAIL {f}")
sys.exit(1 if FAIL else 0)
