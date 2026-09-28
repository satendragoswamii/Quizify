"""Runtime configuration with an admin-managed overlay.

Resolution order is **overlay → environment → default**. The overlay is populated
from the database by the admin panel, so a setting changed in the UI takes effect
immediately without editing ``.env`` or restarting. With no admin database present
this falls through to plain environment variables, which is how the app behaved
before the admin panel existed.
"""

import os
import threading
from typing import Any, Dict, Optional

_lock = threading.RLock()
_overlay: Dict[str, str] = {}

TRUTHY = {"1", "true", "yes", "on", "enabled"}
FALSY = {"0", "false", "no", "off", "disabled", ""}


def set_overlay(values: Dict[str, Any]) -> None:
    """Replace the overlay wholesale (called after settings are saved)."""
    with _lock:
        _overlay.clear()
        for key, value in (values or {}).items():
            if value is None:
                continue
            _overlay[str(key)] = str(value)


def update_overlay(key: str, value: Optional[Any]) -> None:
    with _lock:
        if value is None or value == "":
            _overlay.pop(key, None)
        else:
            _overlay[key] = str(value)


def clear_overlay() -> None:
    with _lock:
        _overlay.clear()


def overlay_keys() -> Dict[str, str]:
    with _lock:
        return dict(_overlay)


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a setting. An empty value at any layer means 'not set'."""
    with _lock:
        value = _overlay.get(name)
    if value:
        return value
    return os.environ.get(name) or default


def get_bool(name: str, default: bool = False) -> bool:
    raw = get(name)
    if raw is None:
        return default
    lowered = str(raw).strip().lower()
    if lowered in TRUTHY:
        return True
    if lowered in FALSY:
        return False
    return default


def get_int(name: str, default: int) -> int:
    try:
        return int(str(get(name, str(default))).strip())
    except (TypeError, ValueError):
        return default
