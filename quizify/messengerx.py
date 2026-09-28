"""MessengerX.io (machaao) integration, reached through its RapidAPI gateway.

MessengerX is a companion platform rather than a completion API. It works in two
distinct steps, and conflating them is the easy mistake here:

    save_companion()   registers a *persona* — name, avatar, system prompt, opening
                       line. It returns the saved companion record. It is setup, not
                       conversation, and calling it with a user's question answers
                       nothing.

    chat()             sends a message to that companion and returns its reply. This
                       is what the help widget needs.

The companion has to exist before it can be talked to, so ``save_companion`` runs once
from the admin panel and ``chat`` runs per message.

Credentials come from configuration, never from source: set ``MESSENGERX_API_KEY``
(the RapidAPI key) in ``.env`` or the admin panel. Nothing here embeds a key, because
these files are what get packaged and deployed.
"""

import logging
from typing import Any, Dict, Optional

from . import config

try:
    import requests
    REQUESTS_OK = True
except ImportError:  # pragma: no cover
    REQUESTS_OK = False

log = logging.getLogger("quizify.messengerx")

DEFAULT_HOST = "messengerx-io.p.rapidapi.com"
SAVE_COMPANION_PATH = "/save-companion"


class MessengerXError(Exception):
    """The gateway could not fulfil the request. The message is safe to show."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def api_key() -> Optional[str]:
    return config.get("MESSENGERX_API_KEY") or config.get("RAPIDAPI_KEY")


def host() -> str:
    return (config.get("MESSENGERX_HOST") or DEFAULT_HOST).strip().strip("/")


def user_id() -> str:
    """The MessengerX account the companion belongs to."""
    return (config.get("MESSENGERX_USER_ID") or "").strip()


def slug() -> str:
    """Which companion to talk to, e.g. 'api-spider-man'."""
    return (config.get("MESSENGERX_SLUG") or "").strip()


def chat_path() -> str:
    """Gateway path that takes a message and returns the companion's reply.

    Left configurable because the RapidAPI gateway documents this separately from
    ``/save-companion`` and the two are not the same call. Until it is set, chat is
    reported unconfigured rather than guessed at — a wrong path would fail on every
    message and look like the assistant is broken.
    """
    return (config.get("MESSENGERX_CHAT_PATH") or "").strip()


def configured() -> bool:
    """Whether a companion can be registered (key + account present)."""
    return bool(api_key() and user_id())


def chat_ready() -> bool:
    """Whether the help widget can actually hold a conversation through MessengerX."""
    return bool(configured() and slug() and chat_path())


def describe() -> Dict[str, Any]:
    """Status for the admin panel, with no key material in it."""
    return {
        "host": host(),
        "has_key": bool(api_key()),
        "user_id": user_id(),
        "slug": slug(),
        "chat_path": chat_path(),
        "configured": configured(),
        "chat_ready": chat_ready(),
    }


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------

def _post(path: str, payload: Dict[str, Any], timeout: int = 30) -> Any:
    if not REQUESTS_OK:
        raise MessengerXError("The 'requests' package is required for MessengerX.")
    key = api_key()
    if not key:
        raise MessengerXError(
            "No MessengerX key configured. Set MESSENGERX_API_KEY in .env or the "
            "admin panel.")

    url = f"https://{host()}{path if path.startswith('/') else '/' + path}"
    try:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-rapidapi-host": host(),
                "x-rapidapi-key": key,
            },
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise MessengerXError(f"MessengerX timed out after {timeout}s.") from exc
    except requests.exceptions.RequestException as exc:
        raise MessengerXError(f"MessengerX is unreachable ({exc}).") from exc

    if response.status_code >= 400:
        detail = (response.text or "")[:300]
        # The key travels in a header, so it is not in the body being echoed back here.
        raise MessengerXError(
            f"MessengerX returned {response.status_code}: {detail}")

    try:
        return response.json()
    except ValueError:
        return response.text


# --------------------------------------------------------------------------------------
# Companion registration
# --------------------------------------------------------------------------------------

def save_companion(*, name: str, description: str, prompt: str,
                   companion_slug: str = "", image_url: str = "",
                   first_message_hint: str = "", image_prompt: str = "",
                   moderation: bool = False,
                   account: str = "") -> Dict[str, Any]:
    """Create or update the companion persona on MessengerX.

    This is the ``/save-companion`` call. It defines who the bot is; it does not carry
    on a conversation. Returns the gateway's response.
    """
    account = account or user_id()
    companion_slug = (companion_slug or slug()).strip()
    if not account:
        raise MessengerXError("Set MESSENGERX_USER_ID before saving a companion.")
    if not companion_slug:
        raise MessengerXError("A companion slug is required, e.g. 'api-spider-man'.")

    payload = {
        "user_id": account,
        "slug": companion_slug,
        "image_url": image_url,
        "name": name,
        "description": description,
        "prompt": prompt,
        "first_message_hint": first_message_hint,
        "image_prompt": image_prompt,
        "moderation": bool(moderation),
    }
    result = _post(SAVE_COMPANION_PATH, payload)
    log.info("Saved MessengerX companion %r for account %r", companion_slug, account)
    return result if isinstance(result, dict) else {"response": result}


# --------------------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------------------

# Where a reply might sit in the response, in the order worth trying. Gateways differ,
# and guessing wrong should fall through rather than return an empty bubble.
_REPLY_KEYS = ("reply", "message", "text", "answer", "response", "content", "output")


def _reply_from(payload: Any) -> str:
    """Pull the companion's text out of whatever shape came back."""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        for item in payload:
            found = _reply_from(item)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""

    # A named reply at this level wins outright.
    for key in _REPLY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Otherwise descend through any envelope — gateways commonly wrap the payload in
    # "data", "result" or similar, and the reply key sits one level further in. The
    # same rule applies at every depth, so this still only returns text found under a
    # recognised reply key rather than the first string it stumbles across.
    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = _reply_from(value)
            if found:
                return found
    return ""


def chat(message: str, *, session: str = "quizify") -> str:
    """Send ``message`` to the configured companion and return its reply.

    Raises :class:`MessengerXError` when MessengerX is not fully configured, so the
    caller can fall through to the next provider instead of showing an empty bubble.
    """
    if not chat_ready():
        missing = [name for name, ok in (
            ("MESSENGERX_API_KEY", bool(api_key())),
            ("MESSENGERX_USER_ID", bool(user_id())),
            ("MESSENGERX_SLUG", bool(slug())),
            ("MESSENGERX_CHAT_PATH", bool(chat_path())),
        ) if not ok]
        raise MessengerXError(
            "MessengerX chat is not configured; missing " + ", ".join(missing) + ". "
            "The chat path is the gateway endpoint that returns a reply — it is a "
            "different endpoint from /save-companion.")

    payload = {
        "user_id": user_id(),
        "slug": slug(),
        "message": message,
        "session_id": session,
    }
    reply = _reply_from(_post(chat_path(), payload))
    if not reply:
        raise MessengerXError("MessengerX returned no reply text.")
    return reply
