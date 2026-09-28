"""Admin panel for Quizify.

``init_admin(app)`` wires everything up: it creates the database, loads stored
settings into the runtime config overlay, registers the ``/admin`` blueprint, and
configures session cookies. Call it once, right after the Flask app is created.
"""

import datetime as _dt
import logging
from typing import Any, Dict, Optional

from flask import Flask

from .. import config
from . import auth, db, repo
from .routes import bp as admin_blueprint

log = logging.getLogger("quizify.admin")

__all__ = ["init_admin", "auth", "db", "repo", "record_job", "admin_blueprint"]


def init_admin(app: Flask, *, db_path: Optional[str] = None) -> None:
    if db_path:
        db.set_path(db_path)

    db.init()
    repo.sync_providers()
    repo.apply_settings_to_config()

    app.secret_key = config.get("QUIZ_SECRET_KEY") or repo.secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set QUIZ_SECURE_COOKIES=true once the app is served over HTTPS.
        SESSION_COOKIE_SECURE=config.get_bool("QUIZ_SECURE_COOKIES", False),
        PERMANENT_SESSION_LIFETIME=_dt.timedelta(
            hours=config.get_int("QUIZ_SESSION_HOURS", 12)),
    )

    app.register_blueprint(admin_blueprint)
    log.info("Admin panel ready at /admin (database: %s)", db.path())


def record_job(**fields: Any) -> None:
    """Log a conversion. Never let bookkeeping break a user's download."""
    try:
        repo.record_job(**fields)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not record job: %s", exc)


def current_user() -> Optional[Dict[str, Any]]:
    """The signed-in user, or None. Safe to call outside a request context."""
    try:
        return auth.current_user()
    except RuntimeError:
        return None
