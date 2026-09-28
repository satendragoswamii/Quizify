"""Data access for the admin panel: users, settings, providers, jobs, audit."""

import json
import secrets
from typing import Any, Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from .. import config
from .. import providers as prov
from . import db

ROLES = ("admin", "editor", "user", "student")

# Settings the admin panel manages, with their defaults. Keys double as the
# environment-variable names, so anything set here overrides the matching env var.
SETTING_DEFAULTS: Dict[str, str] = {
    "APP_NAME": "Quizify",
    "QUIZ_REQUIRE_LOGIN": "false",       # gate the converter itself, not just /admin
    "QUIZ_ALLOW_SIGNUP": "false",
    "QUIZ_DEFAULT_AI_MODE": "auto",
    "QUIZ_DEFAULT_FORMAT": "excel",
    "QUIZ_DEFAULT_MAX_OPTIONS": "4",
    "QUIZ_MAX_UPLOAD_MB": "25",
    "QUIZ_AI_TIMEOUT": "90",
    "QUIZ_AI_RETRIES": "2",
    "QUIZ_AI_MAX_CHUNKS": "12",
    "QUIZ_AI_PROVIDERS": "",
    "QUIZ_LEARNING": "true",             # reuse corrections users chose to save
    "QUIZ_QUESTION_BANK": "true",        # remember downloaded questions and flag repeats
    "QUIZ_JOB_RETENTION_DAYS": "90",
    "QUIZ_ALLOWED_EXTENSIONS": "",        # empty = every format the readers support
}

# Never render these back to the browser.
SECRET_SETTINGS = {"_secret_key"}


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

def all_settings() -> Dict[str, str]:
    stored = {row["key"]: row["value"] for row in db.query("SELECT key, value FROM settings")}
    merged = dict(SETTING_DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k not in SECRET_SETTINGS})
    return merged


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None and row["value"] != "":
        return row["value"]
    return SETTING_DEFAULTS.get(key, default)


def set_setting(key: str, value: Any, actor_id: Optional[int] = None) -> None:
    db.execute(
        """INSERT INTO settings (key, value, updated_at, updated_by)
           VALUES (?, ?, datetime('now'), ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at,
               updated_by = excluded.updated_by""",
        (key, "" if value is None else str(value), actor_id),
    )


def secret_key() -> str:
    """Signing key for sessions — generated once and reused across restarts."""
    row = db.one("SELECT value FROM settings WHERE key = '_secret_key'")
    if row is not None and row["value"]:
        return row["value"]
    generated = secrets.token_hex(32)
    set_setting("_secret_key", generated)
    return generated


def apply_settings_to_config() -> None:
    """Push stored settings and provider config into the runtime overlay."""
    overlay: Dict[str, str] = {}

    for key, value in all_settings().items():
        if value not in (None, ""):
            overlay[key] = str(value)

    order: List[str] = []
    for row in db.query("SELECT * FROM providers ORDER BY priority ASC, name ASC"):
        name = row["name"]
        upper = name.upper()
        if not row["enabled"]:
            # Blanking the key is what makes a provider read as unconfigured.
            for env_name in _provider_key_envs(name):
                overlay.pop(env_name, None)
                overlay[env_name] = ""
            continue
        if row["api_key"]:
            envs = _provider_key_envs(name)
            if envs:
                overlay[envs[0]] = row["api_key"]
        if row["model"]:
            overlay[f"QUIZ_MODEL_{upper}"] = row["model"]
        if row["base_url"]:
            overlay[f"QUIZ_BASE_URL_{upper}"] = row["base_url"]
        order.append(name)

    # An explicit ordering is only meaningful when the admin actually set one.
    if order and not overlay.get("QUIZ_AI_PROVIDERS"):
        overlay["QUIZ_AI_PROVIDERS"] = ",".join(order)

    config.set_overlay(overlay)


def _provider_key_envs(name: str) -> List[str]:
    provider = prov.BY_NAME.get(name)
    return list(provider.key_env) if provider else []


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------

def sync_providers() -> None:
    """Ensure every registry provider has a row, without disturbing saved values."""
    known = {row["name"] for row in db.query("SELECT name FROM providers")}
    for index, provider in enumerate(prov.REGISTRY):
        if provider.name in known or provider.name == "custom":
            continue
        db.execute(
            "INSERT INTO providers (name, enabled, priority) VALUES (?, ?, ?)",
            (provider.name, 1, index),
        )


def provider_rows() -> List[Dict[str, Any]]:
    """Merge stored config with registry metadata and live status, minus secrets."""
    stored = {row["name"]: dict(row) for row in db.query("SELECT * FROM providers")}
    active = {p.name for p in prov.available()}

    rows: List[Dict[str, Any]] = []
    for provider in prov.REGISTRY:
        if provider.name == "custom":
            continue
        saved = stored.get(provider.name, {})
        key = saved.get("api_key") or ""
        rows.append({
            "name": provider.name,
            "label": provider.label,
            "enabled": bool(saved.get("enabled", 1)),
            "has_key": bool(key) or bool(provider.api_key()),
            "key_hint": _mask(key),
            "key_env": provider.key_env[0] if provider.key_env else "",
            "key_from_env": not key and bool(provider.api_key()),
            "model": saved.get("model") or "",
            "effective_model": provider.model(),
            "base_url": saved.get("base_url") or "",
            "effective_base_url": provider.endpoint(),
            "priority": saved.get("priority", 999),
            "free_tier": provider.free_tier,
            "needs_key": provider.needs_key,
            "signup": provider.signup,
            "active": provider.name in active,
        })
    rows.sort(key=lambda r: (r["priority"], r["name"]))
    return rows


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


def save_provider(name: str, *, enabled: bool, api_key: Optional[str],
                  model: str, base_url: str, priority: int) -> None:
    """Persist provider config. ``api_key=None`` leaves the stored key untouched."""
    existing = db.one("SELECT api_key FROM providers WHERE name = ?", (name,))
    key = existing["api_key"] if existing else ""
    if api_key is not None:
        key = api_key.strip()

    db.execute(
        """INSERT INTO providers (name, enabled, api_key, model, base_url, priority, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(name) DO UPDATE SET
               enabled = excluded.enabled,
               api_key = excluded.api_key,
               model = excluded.model,
               base_url = excluded.base_url,
               priority = excluded.priority,
               updated_at = excluded.updated_at""",
        (name, 1 if enabled else 0, key, model.strip(), base_url.strip(), priority),
    )


def clear_provider_key(name: str) -> None:
    db.execute("UPDATE providers SET api_key = '', updated_at = datetime('now') "
               "WHERE name = ?", (name,))


# --------------------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------------------

def user_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users"))


def admin_count(exclude_id: Optional[int] = None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    params: List[Any] = []
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return int(db.scalar(sql, params))


def list_users(search: str = "", role: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM users WHERE 1=1"
    params: List[Any] = []
    if search:
        sql += " AND (email LIKE ? OR name LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term])
    if role in ROLES:
        sql += " AND role = ?"
        params.append(role)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return db.rows_to_dicts(db.query(sql, params))


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),))
    return dict(row) if row else None


def create_user(email: str, password: str, *, name: str = "", role: str = "user",
                is_active: bool = True, daily_quota: int = 0, notes: str = "",
                batch: str = "", roll_no: str = "") -> int:
    if role not in ROLES:
        role = "user"
    return db.execute(
        """INSERT INTO users (email, name, password_hash, role, is_active, daily_quota,
                              notes, batch, roll_no)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (email.strip(), name.strip(), generate_password_hash(password), role,
         1 if is_active else 0, max(0, daily_quota), notes.strip(),
         batch.strip(), roll_no.strip()),
    )


def update_user(user_id: int, **fields: Any) -> None:
    allowed = {"email", "name", "role", "is_active", "daily_quota", "notes",
               "batch", "roll_no"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "role" in updates and updates["role"] not in ROLES:
        updates["role"] = "user"
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0
    assignments = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE users SET {assignments}, updated_at = datetime('now') WHERE id = ?",
               list(updates.values()) + [user_id])


def set_password(user_id: int, password: str) -> None:
    db.execute(
        """UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL,
               updated_at = datetime('now') WHERE id = ?""",
        (generate_password_hash(password), user_id),
    )


def delete_user(user_id: int) -> None:
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def verify_password(user: Dict[str, Any], password: str) -> bool:
    try:
        return check_password_hash(user["password_hash"], password)
    except (ValueError, TypeError):
        return False


def note_login_success(user_id: int) -> None:
    db.execute(
        """UPDATE users SET last_login_at = datetime('now'), failed_logins = 0,
               locked_until = NULL WHERE id = ?""",
        (user_id,),
    )


def note_login_failure(user_id: int, max_attempts: int = 5, lock_minutes: int = 15) -> None:
    """Count the failure and lock the account once the threshold is crossed."""
    db.execute(
        """UPDATE users
              SET failed_logins = failed_logins + 1,
                  locked_until = CASE WHEN failed_logins + 1 >= ?
                                      THEN datetime('now', ?) ELSE locked_until END
            WHERE id = ?""",
        (max_attempts, f"+{lock_minutes} minutes", user_id),
    )


def is_locked(user: Dict[str, Any]) -> bool:
    if not user.get("locked_until"):
        return False
    row = db.one("SELECT datetime('now') < ? AS locked", (user["locked_until"],))
    return bool(row and row["locked"])


def unlock_user(user_id: int) -> None:
    db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?",
               (user_id,))


def usage_today(user_id: int) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND date(created_at) = date('now')",
        (user_id,),
    ))


def quota_exceeded(user: Optional[Dict[str, Any]]) -> bool:
    if not user or not user.get("daily_quota"):
        return False
    return usage_today(user["id"]) >= int(user["daily_quota"])


# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------

def record_job(**fields: Any) -> int:
    columns = ("user_id", "source_name", "source_format", "subject", "topic",
               "output_format", "engine", "questions", "answered", "avg_confidence",
               "duration_ms", "status", "error", "ip")
    values = [fields.get(c) if fields.get(c) is not None else _job_default(c) for c in columns]
    placeholders = ", ".join("?" for _ in columns)
    return db.execute(
        f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})", values)


def _job_default(column: str) -> Any:
    if column == "user_id":
        return None
    if column in ("questions", "answered", "duration_ms"):
        return 0
    if column == "avg_confidence":
        return 0.0
    if column == "status":
        return "ok"
    return ""


def list_jobs(limit: int = 100, offset: int = 0, status: str = "",
              user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = """SELECT jobs.*, users.email AS user_email
               FROM jobs LEFT JOIN users ON users.id = jobs.user_id
              WHERE 1=1"""
    params: List[Any] = []
    if status:
        sql += " AND jobs.status = ?"
        params.append(status)
    if user_id is not None:
        sql += " AND jobs.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY jobs.created_at DESC, jobs.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return db.rows_to_dicts(db.query(sql, params))


def job_count(status: str = "") -> int:
    if status:
        return int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)))
    return int(db.scalar("SELECT COUNT(*) FROM jobs"))


def purge_jobs(older_than_days: int) -> int:
    if older_than_days <= 0:
        return 0
    return db.execute("DELETE FROM jobs WHERE created_at < datetime('now', ?)",
                      (f"-{older_than_days} days",))


def dashboard_stats(user_id: Optional[int] = None) -> Dict[str, Any]:
    """Conversion figures for the dashboard.

    With ``user_id`` every number covers only that account's conversions. The dashboard
    is reachable by anyone signed in, so a plain user must not be shown install-wide
    totals — or, through "recent conversions", the file names other people uploaded.
    """
    scope = " AND user_id = ?" if user_id is not None else ""
    args: List[Any] = [user_id] if user_id is not None else []

    def count(where: str = "", extra: Optional[List[Any]] = None) -> int:
        sql = f"SELECT COUNT(*) FROM jobs WHERE 1=1{scope}{where}"
        return int(db.scalar(sql, args + (extra or [])))

    def total(column: str) -> int:
        return int(db.scalar(
            f"SELECT COALESCE(SUM({column}), 0) FROM jobs WHERE 1=1{scope}", args))

    def grouped(sql: str) -> List[Dict[str, Any]]:
        return db.rows_to_dicts(db.query(sql.replace("{scope}", scope), args))

    stats: Dict[str, Any] = {
        "scoped": user_id is not None,
        "jobs": count(),
        "jobs_today": count(" AND date(created_at) = date('now')"),
        "jobs_week": count(" AND created_at >= datetime('now', '-7 days')"),
        "failed": count(" AND status = ?", ["error"]),
        "questions": total("questions"),
        "answered": total("answered"),
        "avg_confidence": round(float(db.scalar(
            f"SELECT COALESCE(AVG(avg_confidence), 0) FROM jobs "
            f"WHERE status = 'ok'{scope}", args, default=0.0)), 3),
        "by_engine": grouped(
            """SELECT engine, COUNT(*) AS count FROM jobs
                WHERE engine != ''{scope} GROUP BY engine ORDER BY count DESC"""),
        "by_format": grouped(
            """SELECT source_format AS format, COUNT(*) AS count FROM jobs
                WHERE source_format != ''{scope} GROUP BY source_format
                ORDER BY count DESC LIMIT 10"""),
        "daily": grouped(
            """SELECT date(created_at) AS day, COUNT(*) AS count FROM jobs
                WHERE created_at >= datetime('now', '-14 days'){scope}
                GROUP BY day ORDER BY day"""),
    }

    # Install-wide people counts belong only on an unscoped (administrator) view.
    if user_id is None:
        stats.update(
            users=user_count(),
            active_users=int(db.scalar("SELECT COUNT(*) FROM users WHERE is_active = 1")),
            admins=admin_count(),
        )
    return stats


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------

def log(action: str, *, actor: Optional[Dict[str, Any]] = None, target: str = "",
        detail: Any = "", ip: str = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str)[:2000]
    db.execute(
        """INSERT INTO audit (actor_id, actor_email, action, target, detail, ip)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (actor.get("id") if actor else None,
         actor.get("email", "") if actor else "",
         action, str(target)[:200], detail[:2000], ip[:64]),
    )


def list_audit(limit: int = 200, action: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM audit WHERE 1=1"
    params: List[Any] = []
    if action:
        sql += " AND action = ?"
        params.append(action)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return db.rows_to_dicts(db.query(sql, params))


def audit_actions() -> List[str]:
    return [row["action"] for row in
            db.query("SELECT DISTINCT action FROM audit ORDER BY action")]
