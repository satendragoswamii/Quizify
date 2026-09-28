"""Authentication, authorisation, and CSRF for the admin panel.

Sessions ride Flask's signed cookies. The signing key is generated once and stored
in the database, so sessions survive restarts without a key in the environment.
"""

import hmac
import secrets
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import abort, flash, g, redirect, request, session, url_for

from . import repo

SESSION_USER_KEY = "quizify_user_id"
CSRF_SESSION_KEY = "quizify_csrf"
MAX_LOGIN_ATTEMPTS = 5
LOCK_MINUTES = 15
MIN_PASSWORD_LENGTH = 8

# Students sit at rank 0: they never satisfy any staff has_role() check, so the
# admin panel stays closed to them. They use the separate /student portal instead.
ROLE_RANK = {"student": 0, "user": 1, "editor": 2, "admin": 3}


# --------------------------------------------------------------------------------------
# Current user
# --------------------------------------------------------------------------------------

def current_user() -> Optional[Dict[str, Any]]:
    """The signed-in user for this request, cached on ``g``."""
    if "quizify_current_user" in g:
        return g.quizify_current_user

    user = None
    user_id = session.get(SESSION_USER_KEY)
    if user_id is not None:
        user = repo.get_user(int(user_id))
        # A user deactivated or deleted mid-session must not stay signed in.
        if user is None or not user["is_active"]:
            session.pop(SESSION_USER_KEY, None)
            user = None

    g.quizify_current_user = user
    return user


def is_admin(user: Optional[Dict[str, Any]] = None) -> bool:
    user = user if user is not None else current_user()
    return bool(user and user["role"] == "admin")


def has_role(minimum: str, user: Optional[Dict[str, Any]] = None) -> bool:
    user = user if user is not None else current_user()
    if not user:
        return False
    return ROLE_RANK.get(user["role"], 0) >= ROLE_RANK.get(minimum, 99)


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


# --------------------------------------------------------------------------------------
# Sign in / out
# --------------------------------------------------------------------------------------

class LoginError(Exception):
    """Login failed. The message is safe to show the user."""


def login(email: str, password: str) -> Dict[str, Any]:
    """Verify credentials and start a session, or raise :class:`LoginError`.

    The same message is returned for unknown accounts and wrong passwords so the
    form cannot be used to discover which emails exist.
    """
    generic = "Incorrect email or password."
    user = repo.get_user_by_email(email or "")

    if user is None:
        raise LoginError(generic)
    if repo.is_locked(user):
        raise LoginError(
            f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
    if not user["is_active"]:
        raise LoginError("This account is disabled.")
    if not repo.verify_password(user, password or ""):
        repo.note_login_failure(user["id"], MAX_LOGIN_ATTEMPTS, LOCK_MINUTES)
        repo.log("login.failed", target=user["email"], ip=client_ip())
        raise LoginError(generic)

    session.clear()                    # new session id defeats fixation
    session[SESSION_USER_KEY] = user["id"]
    session.permanent = True
    repo.note_login_success(user["id"])
    repo.log("login.success", actor=user, target=user["email"], ip=client_ip())
    g.pop("quizify_current_user", None)
    return user


def logout() -> None:
    user = current_user()
    if user:
        repo.log("logout", actor=user, target=user["email"], ip=client_ip())
    session.clear()
    g.pop("quizify_current_user", None)


def validate_password(password: str, confirm: Optional[str] = None) -> None:
    """Raise :class:`ValueError` when a new password is unacceptable."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if confirm is not None and password != confirm:
        raise ValueError("The two passwords do not match.")
    if password.lower() in {"password", "12345678", "quizify123", "admin123"}:
        raise ValueError("That password is too common. Choose something else.")


# --------------------------------------------------------------------------------------
# Route guards
# --------------------------------------------------------------------------------------

def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if current_user() is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("admin.login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


def role_required(minimum: str) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            user = current_user()
            if user is None:
                if request.path.startswith("/api/"):
                    abort(401)
                return redirect(url_for("admin.login", next=request.full_path))
            if not has_role(minimum, user):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


admin_required = role_required("admin")


# --------------------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------------------

def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def check_csrf() -> None:
    """Abort with 400 when a state-changing request has no valid token."""
    submitted = (request.form.get("csrf_token")
                 or request.headers.get("X-CSRF-Token", ""))
    expected = session.get(CSRF_SESSION_KEY, "")
    if not expected or not submitted or not hmac.compare_digest(submitted, expected):
        abort(400, description="Your session expired. Please reload and try again.")


def csrf_protect(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            check_csrf()
        return view(*args, **kwargs)
    return wrapper


def flash_error(message: str) -> None:
    flash(message, "error")


def flash_ok(message: str) -> None:
    flash(message, "success")
