"""AI provider registry and the OpenAI-compatible transport.

Most hosted LLM services expose the same ``POST /chat/completions`` shape, so one
transport covers OpenRouter, Groq, DeepSeek, Together, Mistral, Cerebras, Gemini,
a local Ollama, and any custom endpoint. Claude is the exception — it goes through
the official Anthropic SDK in :mod:`quizify.ai`.

Adding a provider is a single :class:`Provider` entry. Everything else — key
detection, ordering, failover, model overrides — follows from the registry.
"""

import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from . import config

try:
    import requests
    REQUESTS_OK = True
except ImportError:  # pragma: no cover
    REQUESTS_OK = False


def default_timeout() -> int:
    """Seconds to wait on a single provider request.

    QUIZ_API_TIMEOUT is the older name that ships alongside the custom-endpoint
    settings, so it still applies when the current key is unset — otherwise a
    timeout configured that way is silently ignored and every request waits 90s.
    """
    return config.get_int("QUIZ_AI_TIMEOUT", 0) or config.get_int("QUIZ_API_TIMEOUT", 90)


def max_retries() -> int:
    return config.get_int("QUIZ_AI_RETRIES", 2)


class ProviderError(Exception):
    """A provider could not fulfil the request. Callers may try the next one."""

    def __init__(self, message: str, *, retryable: bool = False, provider: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


@dataclass(frozen=True)
class Provider:
    name: str                       # short id used in config and the API
    label: str                      # human-readable name
    kind: str = "openai"            # "openai" (chat/completions) or "anthropic" (SDK)
    base_url: str = ""              # OpenAI-compatible root, no trailing slash
    key_env: Tuple[str, ...] = ()   # env vars checked for an API key, in order
    default_model: str = ""
    signup: str = ""                # where to get a key
    free_tier: bool = False
    json_schema: bool = False       # supports response_format json_schema
    json_object: bool = True        # supports response_format json_object
    max_chars: int = 12_000         # per-request document slice
    max_tokens: int = 8_000
    headers: Dict[str, str] = field(default_factory=dict)
    needs_key: bool = True
    # Keyless providers (a local server) would otherwise always look "configured", so
    # they must be switched on deliberately via enable_env or QUIZ_AI_PROVIDERS.
    opt_in: bool = False
    enable_env: Tuple[str, ...] = ()
    # For self-hosted services the base URL is derived from a host variable, so it
    # stays adjustable at runtime rather than being fixed when the module loads.
    host_env: str = ""
    host_suffix: str = ""
    quality: int = 50               # ordering hint; higher runs first
    # A conversational persona is not a document parser. Providers marked chat_only are
    # offered to the help widget but never to quiz extraction, where a companion would
    # answer in character instead of returning the JSON the parser expects.
    chat_only: bool = False

    # ---- configuration -------------------------------------------------------------

    def is_enabled(self, requested: Tuple[str, ...] = ()) -> bool:
        if not self.opt_in:
            return True
        if self.name in requested:
            return True
        return any(config.get(name) for name in self.enable_env)

    def api_key(self) -> Optional[str]:
        for name in self.key_env:
            value = config.get(name)
            if value:
                return value
        return None

    def endpoint(self) -> str:
        base = config.get(f"QUIZ_BASE_URL_{self.name.upper()}")
        if not base and self.host_env:
            host = config.get(self.host_env)
            if host:
                base = host.rstrip("/") + self.host_suffix
        return (base or self.base_url).rstrip("/")

    def model(self) -> str:
        return (config.get(f"QUIZ_MODEL_{self.name.upper()}")
                or self.default_model)

    def is_configured(self) -> bool:
        if not self.endpoint() and self.kind == "openai":
            return False
        if not self.needs_key:
            return True
        return bool(self.api_key())


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------
# Model ids on free tiers change over time. Every default below can be overridden with
# QUIZ_MODEL_<NAME>, and OpenRouter additionally self-heals (see pick_free_openrouter_model).

REGISTRY: Tuple[Provider, ...] = (
    # Stays out of the chain until MESSENGERX_CHAT_PATH is set. A key alone is not
    # enough: /save-companion registers the persona but cannot answer a message, so
    # joining early would cost a failed attempt on every question asked.
    Provider(
        name="messengerx", label="MessengerX companion", kind="messengerx",
        key_env=("MESSENGERX_API_KEY", "RAPIDAPI_KEY"),
        signup="https://rapidapi.com/machaao-inc-machaao-inc-default/api/messengerx-io",
        chat_only=True, needs_key=True, quality=20,
        opt_in=True, enable_env=("MESSENGERX_CHAT_PATH",),
    ),
    Provider(
        name="anthropic", label="Claude (Anthropic)", kind="anthropic",
        key_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        default_model="claude-opus-5",
        signup="https://console.anthropic.com/",
        json_schema=True, max_chars=12_000, max_tokens=32_000, quality=100,
    ),
    Provider(
        name="openrouter", label="OpenRouter", base_url="https://openrouter.ai/api/v1",
        key_env=("OPENROUTER_API_KEY",),
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        signup="https://openrouter.ai/keys",
        free_tier=True, json_schema=True, max_chars=10_000, quality=80,
        headers={"HTTP-Referer": "https://github.com/quizify", "X-Title": "Quizify"},
    ),
    Provider(
        name="groq", label="Groq", base_url="https://api.groq.com/openai/v1",
        key_env=("GROQ_API_KEY",),
        default_model="llama-3.3-70b-versatile",
        signup="https://console.groq.com/keys",
        free_tier=True, json_object=True, max_chars=8_000, quality=75,
    ),
    Provider(
        name="gemini", label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_model="gemini-2.0-flash",
        signup="https://aistudio.google.com/apikey",
        free_tier=True, json_object=True, max_chars=12_000, quality=78,
    ),
    Provider(
        name="cerebras", label="Cerebras", base_url="https://api.cerebras.ai/v1",
        key_env=("CEREBRAS_API_KEY",),
        default_model="llama-3.3-70b",
        signup="https://cloud.cerebras.ai/",
        free_tier=True, json_object=True, max_chars=8_000, quality=70,
    ),
    Provider(
        name="deepseek", label="DeepSeek", base_url="https://api.deepseek.com/v1",
        key_env=("DEEPSEEK_API_KEY",),
        default_model="deepseek-chat",
        signup="https://platform.deepseek.com/",
        json_object=True, max_chars=12_000, quality=72,
    ),
    Provider(
        name="mistral", label="Mistral", base_url="https://api.mistral.ai/v1",
        key_env=("MISTRAL_API_KEY",),
        default_model="mistral-large-latest",
        signup="https://console.mistral.ai/",
        free_tier=True, json_object=True, max_chars=10_000, quality=68,
    ),
    Provider(
        name="together", label="Together AI", base_url="https://api.together.xyz/v1",
        key_env=("TOGETHER_API_KEY",),
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        signup="https://api.together.ai/settings/api-keys",
        json_object=True, max_chars=10_000, quality=65,
    ),
    Provider(
        name="opencode", label="OpenCode", base_url="https://api.opencode.ai/v1",
        key_env=("OPENCODE_API_KEY",),
        default_model="opencode",
        signup="https://opencode.ai/",
        json_object=True, max_chars=10_000, quality=60,
    ),
    Provider(
        name="ollama", label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        host_env="OLLAMA_HOST", host_suffix="/v1",
        key_env=(),
        default_model="llama3.1",
        signup="https://ollama.com/download",
        free_tier=True, json_object=True, needs_key=False,
        opt_in=True, enable_env=("OLLAMA_HOST", "QUIZ_ENABLE_OLLAMA"),
        max_chars=6_000, quality=30,
    ),
    # Escape hatch: any other OpenAI-compatible service.
    Provider(
        name="custom", label="Custom endpoint", base_url="",
        key_env=("QUIZ_API_KEY",),
        default_model="",
        json_object=True, max_chars=10_000, quality=40,
    ),
)

BY_NAME: Dict[str, Provider] = {p.name: p for p in REGISTRY}


def _custom_provider() -> Optional[Provider]:
    """Build the custom provider from the legacy QUIZ_API_* settings, if present."""
    endpoint = (config.get("QUIZ_API_ENDPOINT") or "").strip()
    if not endpoint:
        return None
    # Legacy config pointed at the full chat/completions URL; accept either form.
    base = re.sub(r"/chat/completions/?$", "", endpoint.rstrip("/"))
    template = BY_NAME["custom"]
    # A legacy endpoint that matches a known provider should use that provider's rules,
    # with QUIZ_API_KEY accepted as its key. Scoping the key here rather than on the
    # provider itself stops QUIZ_API_KEY from making that provider look configured when
    # the endpoint actually points somewhere else.
    for provider in REGISTRY:
        if provider.base_url and base.rstrip("/") == provider.base_url.rstrip("/"):
            return replace(
                provider,
                key_env=provider.key_env + ("QUIZ_API_KEY",),
                default_model=config.get("QUIZ_API_MODEL") or provider.default_model,
            )
    return Provider(
        name="custom", label=f"Custom ({base})", base_url=base,
        key_env=template.key_env,
        default_model=config.get("QUIZ_API_MODEL", ""),
        json_object=True, max_chars=template.max_chars, quality=template.quality,
    )


def available() -> List[Provider]:
    """Every provider that is configured, best first.

    Order comes from ``QUIZ_AI_PROVIDERS`` when set (comma-separated names), and
    otherwise from each provider's quality hint.
    """
    preference = (config.get("QUIZ_AI_PROVIDERS") or "").strip()
    wanted = tuple(n.strip().lower() for n in preference.split(",") if n.strip())

    candidates: List[Provider] = []
    for provider in REGISTRY:
        if provider.name == "custom":
            continue
        if provider.is_enabled(wanted) and provider.is_configured():
            candidates.append(provider)

    custom = _custom_provider()
    if custom is not None and custom.is_configured():
        # Do not list the same service twice if the legacy config points at a known one.
        if not any(p.name == custom.name for p in candidates):
            candidates.append(custom)

    if wanted:
        # Explicit configuration wins; names that are not usable are skipped.
        return [p for name in wanted for p in candidates if p.name == name]

    return sorted(candidates, key=lambda p: p.quality, reverse=True)


def describe() -> List[Dict[str, Any]]:
    """Config snapshot for /api/health — never includes key material."""
    active = {p.name for p in available()}
    rows: List[Dict[str, Any]] = []
    for provider in REGISTRY:
        if provider.name == "custom":
            continue
        rows.append({
            "name": provider.name,
            "label": provider.label,
            "configured": provider.name in active,
            "free_tier": provider.free_tier,
            "model": provider.model(),
            "key_env": provider.key_env[0] if provider.key_env else None,
            "signup": provider.signup,
        })
    custom = _custom_provider()
    if custom is not None and custom.name == "custom":
        rows.append({
            "name": "custom", "label": custom.label,
            "configured": custom.is_configured(), "free_tier": False,
            "model": custom.model(), "key_env": "QUIZ_API_KEY", "signup": "",
        })
    return rows


# --------------------------------------------------------------------------------------
# OpenAI-compatible transport
# --------------------------------------------------------------------------------------

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 522, 524}


def _response_format(provider: Provider, schema: Optional[Dict[str, Any]],
                     json_mode: bool) -> Optional[Dict[str, Any]]:
    """Strongest output constraint this provider supports."""
    if not json_mode:
        return None  # prose reply — constraining it would produce JSON instead
    if schema and provider.json_schema:
        return {
            "type": "json_schema",
            "json_schema": {"name": "quiz_extraction", "strict": True, "schema": schema},
        }
    if provider.json_object:
        return {"type": "json_object"}
    return None


def _post(provider: Provider, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    key = provider.api_key()
    headers = {"Content-Type": "application/json", **provider.headers}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    url = f"{provider.endpoint()}/chat/completions"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise ProviderError(f"{provider.label} timed out after {timeout}s.",
                            retryable=True, provider=provider.name) from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderError(f"{provider.label} is unreachable ({exc}).",
                            retryable=True, provider=provider.name) from exc

    if response.status_code >= 400:
        detail = _error_detail(response)
        raise ProviderError(
            f"{provider.label} returned {response.status_code}: {detail}",
            retryable=response.status_code in _RETRYABLE_STATUS,
            provider=provider.name,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{provider.label} returned a non-JSON response.",
                            provider=provider.name) from exc


def _error_detail(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]


def _content_from(result: Dict[str, Any]) -> str:
    """Pull the assistant text out of a chat-completions response."""
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        # Some gateways return content as a list of parts.
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            if any(parts):
                return "".join(parts)
        # A model that answered via a tool call still carries usable JSON.
        for call in message.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments")
            if arguments:
                return arguments
    if "questions" in result:  # endpoint already returns our shape
        return json.dumps(result)
    raise ProviderError("The response contained no message content.")


def complete(provider: Provider, system: str, user: str,
             schema: Optional[Dict[str, Any]] = None,
             max_tokens: Optional[int] = None,
             json_mode: bool = True,
             timeout: Optional[int] = None) -> str:
    """Run one chat completion and return the raw assistant text.

    Retries retryable failures with backoff, and steps down the output constraint
    if the provider rejects it (weaker services often advertise more than they support).
    """
    if not REQUESTS_OK:
        raise ProviderError("The 'requests' package is required for HTTP providers.",
                            provider=provider.name)
    timeout = timeout or default_timeout()
    retries = max_retries()
    model = provider.model()
    if not model:
        raise ProviderError(f"No model configured for {provider.label}. "
                            f"Set QUIZ_MODEL_{provider.name.upper()}.",
                            provider=provider.name)

    base_payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": max_tokens or provider.max_tokens,
    }

    # Strongest constraint first, then progressively weaker ones.
    formats: List[Optional[Dict[str, Any]]] = []
    strongest = _response_format(provider, schema, json_mode)
    if strongest:
        formats.append(strongest)
        if strongest["type"] == "json_schema" and provider.json_object:
            formats.append({"type": "json_object"})
    formats.append(None)

    last_error: Optional[ProviderError] = None
    for response_format in formats:
        payload = dict(base_payload)
        if response_format:
            payload["response_format"] = response_format

        for attempt in range(retries + 1):
            try:
                result = _post(provider, payload, timeout)
                return _content_from(result)
            except ProviderError as exc:
                last_error = exc
                if exc.retryable and attempt < retries:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                break  # try a weaker response_format, or give up

        # Only step down when the failure looks like a format rejection.
        message = str(last_error).lower()
        if not any(word in message for word in
                   ("response_format", "json_schema", "schema", "not supported",
                    "unsupported", "invalid", "400")):
            break

    raise last_error or ProviderError("Request failed.", provider=provider.name)


# --------------------------------------------------------------------------------------
# OpenRouter free-model discovery
# --------------------------------------------------------------------------------------

def list_models(provider: Provider, timeout: int = 20) -> List[Dict[str, Any]]:
    if not REQUESTS_OK:
        return []
    key = provider.api_key()
    headers = {**provider.headers}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = requests.get(f"{provider.endpoint()}/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json().get("data")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def pick_free_model(provider: Provider) -> Optional[str]:
    """Find a usable free model, so a stale default id self-heals.

    Free model ids churn frequently. When the configured one is rejected we ask the
    provider what it currently offers and pick the largest-context free option.
    """
    models = list_models(provider)
    free: List[Tuple[int, str]] = []
    for entry in models:
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        pricing = entry.get("pricing") or {}
        is_free = model_id.endswith(":free") or (
            str(pricing.get("prompt", "1")) in ("0", "0.0", "-0")
            and str(pricing.get("completion", "1")) in ("0", "0.0", "-0")
        )
        if not is_free:
            continue
        context = entry.get("context_length") or 0
        try:
            context = int(context)
        except (TypeError, ValueError):
            context = 0
        # Instruction-following matters more than raw size for extraction work.
        if any(bad in model_id.lower() for bad in ("vision", "image", "embed", "rerank")):
            continue
        free.append((context, model_id))

    if not free:
        return None
    free.sort(reverse=True)
    return free[0][1]
