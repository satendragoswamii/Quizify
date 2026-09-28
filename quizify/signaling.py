"""In-memory WebRTC signaling relay for live exam proctoring.

WebRTC sets up a direct peer-to-peer video connection between the learner (who sends
their camera) and the admin (who watches). To negotiate that connection the two peers
must first swap small text messages — an SDP *offer*/*answer* and ICE *candidates*.
This module is only that message relay; the video itself never touches the server.

A "room" is one exam attempt. The learner joins as ``camera`` and the admin as
``viewer``; anything one side sends is forwarded to the other. Rooms live purely in
memory and disappear when both peers disconnect.
"""

import json
import threading
from typing import Dict, Optional, Set

_LOCK = threading.RLock()
# room key (attempt id, as str) -> role -> set of websocket connections
_ROOMS: Dict[str, Dict[str, Set[object]]] = {}


def _room(attempt_key: str) -> Dict[str, Set[object]]:
    return _ROOMS.setdefault(attempt_key, {"camera": set(), "viewer": set()})


def join(attempt_key: str, role: str, ws: object) -> None:
    with _LOCK:
        _room(attempt_key)[role].add(ws)


def leave(attempt_key: str, role: str, ws: object) -> None:
    with _LOCK:
        room = _ROOMS.get(attempt_key)
        if not room:
            return
        room.get(role, set()).discard(ws)
        if not room["camera"] and not room["viewer"]:
            _ROOMS.pop(attempt_key, None)


def peers(attempt_key: str, other_role: str) -> Set[object]:
    """A snapshot of the connections for the opposite role."""
    with _LOCK:
        room = _ROOMS.get(attempt_key)
        if not room:
            return set()
        return set(room.get(other_role, set()))


def relay(attempt_key: str, from_role: str, message: str) -> int:
    """Forward a raw signaling message to every peer of the opposite role.

    Returns how many peers it reached. Dead sockets are dropped silently.
    """
    other = "viewer" if from_role == "camera" else "camera"
    sent = 0
    for peer in peers(attempt_key, other):
        try:
            peer.send(message)
            sent += 1
        except Exception:
            leave(attempt_key, other, peer)
    return sent


def notify(attempt_key: str, from_role: str, event: str) -> None:
    """Tell the opposite role about a lifecycle event (e.g. a peer joining/leaving)."""
    relay(attempt_key, from_role, json.dumps({"type": event, "role": from_role}))


def camera_online(attempt_key: str) -> bool:
    with _LOCK:
        room = _ROOMS.get(attempt_key)
        return bool(room and room["camera"])
