"""Per-user feature and limit management.

The admin panel already sets these globally. This module adds a second, narrower layer
so an administrator can grant or withhold them for one account without moving everyone:

    overlay/env (global)  ->  user_settings row (this user)  ->  effective value

A user with no rows simply follows the global setting, which is why an install that
never opens the per-user editor behaves exactly as it did before.

:data:`CATALOGUE` is the single source of truth. It drives the admin form, the
validation of what gets saved, and the values the converter enforces, so a feature
cannot be listed in one place and forgotten in another.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config

log = logging.getLogger("quizify")

INHERIT = ""   # stored value meaning "no override — use the global setting"


@dataclass(frozen=True)
class Feature:
    """One thing an administrator can manage, globally or for a single user."""

    key: str
    label: str
    kind: str                       # bool | int | choice | text
    help: str = ""
    group: str = "Features"
    choices: Tuple[Tuple[str, str], ...] = ()   # (value, label)
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    # Global fallback when nothing is configured anywhere.
    default: str = ""

    def coerce(self, raw: Any) -> Optional[str]:
        """Normalise a submitted value, or None when it is not usable.

        Returning None for junk means a bad form post leaves the user inheriting rather
        than storing something the converter would have to defend against later.
        """
        text = "" if raw is None else str(raw).strip()
        if text == INHERIT:
            return INHERIT

        if self.kind == "bool":
            lowered = text.lower()
            if lowered in config.TRUTHY:
                return "true"
            if lowered in config.FALSY:
                return "false"
            return None

        if self.kind == "int":
            try:
                value = int(text)
            except (TypeError, ValueError):
                return None
            if self.minimum is not None:
                value = max(self.minimum, value)
            if self.maximum is not None:
                value = min(self.maximum, value)
            return str(value)

        if self.kind == "choice":
            allowed = {value for value, _ in self.choices}
            return text if text in allowed else None

        return text[:500]


AI_MODES = (("auto", "Auto — only when the rules are unsure"),
            ("off", "Off — rule-based parsing only"),
            ("always", "Always — send every document"))

CATALOGUE: Tuple[Feature, ...] = (
    Feature("QUIZ_ALLOW_AI", "AI assist", "bool", group="Features",
            help="Let this account use the AI backend at all. With it off, conversions "
                 "always run on rules alone however the form is set.",
            default="true"),
    Feature("QUIZ_DEFAULT_AI_MODE", "Default AI mode", "choice", group="Features",
            choices=AI_MODES, help="Which AI mode the converter form starts on.",
            default="auto"),
    Feature("QUIZ_QUESTION_BANK", "Question bank", "bool", group="Features",
            help="Record this account's questions and warn about ones used before.",
            default="true"),
    Feature("QUIZ_LEARNING", "Save corrections", "bool", group="Features",
            help="Offer the 'remember my corrections' opt-in after editing.",
            default="true"),
    Feature("QUIZ_ALLOW_EDITING", "Edit in preview", "bool", group="Features",
            help="Allow questions to be corrected before download.",
            default="true"),

    Feature("QUIZ_DAILY_QUOTA", "Daily conversion limit", "int", group="Limits",
            minimum=0, maximum=100_000,
            help="0 means unlimited. Kept in step with the field above.",
            default="0"),
    Feature("QUIZ_MAX_UPLOAD_MB", "Maximum upload size (MB)", "int", group="Limits",
            minimum=1, maximum=2_000, help="Applies to this account's uploads.",
            default="25"),
    Feature("QUIZ_DEFAULT_MAX_OPTIONS", "Default option columns", "int", group="Limits",
            minimum=2, maximum=26, default="4"),

    Feature("QUIZ_ALLOWED_FORMATS", "Allowed output formats", "text", group="Limits",
            help="Comma-separated from excel, csv, json. Blank allows all three.",
            default=""),
    Feature("QUIZ_ALLOWED_EXTENSIONS", "Allowed file types", "text", group="Limits",
            help="Comma-separated, e.g. docx,pdf,txt. Blank allows every supported type.",
            default=""),
)

BY_KEY: Dict[str, Feature] = {f.key: f for f in CATALOGUE}


def groups() -> List[Tuple[str, List[Feature]]]:
    """The catalogue arranged for rendering, preserving declaration order."""
    ordered: Dict[str, List[Feature]] = {}
    for feature in CATALOGUE:
        ordered.setdefault(feature.group, []).append(feature)
    return list(ordered.items())


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------

def _db():
    try:
        from .admin import db
        return db
    except Exception:  # pragma: no cover - admin package unavailable
        return None


def overrides_for(user_id: Optional[int]) -> Dict[str, str]:
    """Every override stored for a user, keyed by setting name."""
    db = _db()
    if db is None or not user_id:
        return {}
    try:
        rows = db.query(
            "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,))
        return {row["key"]: row["value"] for row in rows if row["key"] in BY_KEY}
    except Exception:  # pragma: no cover - table missing on an old database
        return {}


def save_overrides(user_id: int, submitted: Dict[str, Any]) -> int:
    """Replace a user's overrides with ``submitted``.

    A value of :data:`INHERIT` (or anything unusable) removes the override, so clearing
    a field in the admin form puts the account back on the global setting. Returns how
    many overrides the user now has.
    """
    db = _db()
    if db is None or not user_id:
        return 0

    for feature in CATALOGUE:
        if feature.key not in submitted:
            continue
        value = feature.coerce(submitted.get(feature.key))
        try:
            if value is None or value == INHERIT:
                db.execute("DELETE FROM user_settings WHERE user_id = ? AND key = ?",
                           (user_id, feature.key))
            else:
                db.execute(
                    "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET "
                    "value = excluded.value, updated_at = datetime('now')",
                    (user_id, feature.key, value))
        except Exception as exc:
            log.warning("Could not save %s for user %s: %s", feature.key, user_id, exc)
    return len(overrides_for(user_id))


def clear_overrides(user_id: int) -> int:
    db = _db()
    if db is None or not user_id:
        return 0
    try:
        return int(db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,)))
    except Exception:  # pragma: no cover
        return 0


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------

def global_value(key: str) -> str:
    """What this setting resolves to for someone with no override."""
    feature = BY_KEY.get(key)
    return config.get(key, feature.default if feature else "") or ""


class Settings:
    """The effective settings for one user (or for an anonymous visitor).

    Resolves each key through the user's overrides, then the global configuration.
    Built once per request in the HTTP layer and passed down, so a single conversion
    always sees one consistent view.
    """

    def __init__(self, user: Optional[Dict[str, Any]] = None):
        self.user = user
        self.user_id = user.get("id") if user else None
        self._overrides = overrides_for(self.user_id)

    # ---- raw access -------------------------------------------------------------------

    def raw(self, key: str) -> str:
        override = self._overrides.get(key)
        if override not in (None, INHERIT):
            return override
        return global_value(key)

    def is_overridden(self, key: str) -> bool:
        return self._overrides.get(key) not in (None, INHERIT)

    # ---- typed access -----------------------------------------------------------------

    def flag(self, key: str, default: bool = True) -> bool:
        value = str(self.raw(key)).strip().lower()
        if value in config.TRUTHY:
            return True
        if value in config.FALSY:
            return False
        return default

    def number(self, key: str, default: int) -> int:
        feature = BY_KEY.get(key)
        try:
            value = int(str(self.raw(key)).strip())
        except (TypeError, ValueError):
            return default
        if feature:
            if feature.minimum is not None:
                value = max(feature.minimum, value)
            if feature.maximum is not None:
                value = min(feature.maximum, value)
        return value

    def csv_set(self, key: str) -> set:
        """A comma-separated setting as a lowercase set. Empty means 'no restriction'."""
        return {p.strip().lower() for p in self.raw(key).split(",") if p.strip()}

    # ---- the questions the converter actually asks ------------------------------------

    def ai_allowed(self) -> bool:
        return self.flag("QUIZ_ALLOW_AI", True)

    def ai_mode(self, requested: str) -> str:
        """The AI mode to use, honouring a withheld-AI account."""
        if not self.ai_allowed():
            return "off"
        return requested

    def question_bank(self) -> bool:
        return self.flag("QUIZ_QUESTION_BANK", True)

    def learning(self) -> bool:
        return self.flag("QUIZ_LEARNING", True)

    def editing(self) -> bool:
        return self.flag("QUIZ_ALLOW_EDITING", True)

    def max_upload_bytes(self) -> int:
        return self.number("QUIZ_MAX_UPLOAD_MB", 25) * 1024 * 1024

    def max_options(self) -> int:
        return self.number("QUIZ_DEFAULT_MAX_OPTIONS", 4)

    def allowed_formats(self, valid: set) -> set:
        wanted = self.csv_set("QUIZ_ALLOWED_FORMATS")
        return (wanted & valid) or valid

    def allowed_extensions(self, supported: set) -> set:
        wanted = {"." + e.lstrip(".") for e in self.csv_set("QUIZ_ALLOWED_EXTENSIONS")}
        return (wanted & supported) or supported

    def daily_quota(self) -> int:
        """0 means unlimited. The user record stays authoritative when it is set."""
        if self.user and self.user.get("daily_quota"):
            return int(self.user["daily_quota"])
        return self.number("QUIZ_DAILY_QUOTA", 0)

    def describe(self) -> Dict[str, Any]:
        """The effective view, for /api/health and the admin summary."""
        return {
            "ai": self.ai_allowed(),
            "question_bank": self.question_bank(),
            "learning": self.learning(),
            "editing": self.editing(),
            "max_upload_mb": self.number("QUIZ_MAX_UPLOAD_MB", 25),
            "max_options": self.max_options(),
            "formats": sorted(self.allowed_formats({"excel", "csv", "json"})),
            "daily_quota": self.daily_quota(),
        }


def for_user(user: Optional[Dict[str, Any]] = None) -> Settings:
    return Settings(user)
