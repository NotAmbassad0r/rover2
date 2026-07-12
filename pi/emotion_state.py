"""Shared emotion/state layer for the ROVER2 face PWA.

A small, backend-computed "how is the robot doing" signal — distinct from the
face's own activity-mode rendering (FOLLOW/SEARCH/etc.), which is derived
client-side from raw telemetry fields. This module is the single source of
truth for the six state names so backend and face never drift apart; it is
serialised into the existing WS telemetry channel (see ws_control.py) as
`emotion_state`. The face PWA only renders whatever value arrives — it does
not need to know why.

Usage: modules call the small `on_*` event functions when the underlying
signal changes (person detected/lost, wake word heard, a tool starting or
finishing, watchdog raising/clearing a concern). `current()` resolves the
single active state by priority: watchdog concern > in-flight tool > ambient
presence.
"""
from __future__ import annotations

IDLE = "idle"
ATTENTIVE = "attentive"
ACKNOWLEDGING = "acknowledging"
WORKING = "working"
CONCERNED = "concerned"
ALERT = "alert"

VALID_STATES = frozenset({IDLE, ATTENTIVE, ACKNOWLEDGING, WORKING, CONCERNED, ALERT})

_ambient: str = IDLE            # IDLE or ATTENTIVE — person detection / wake word
_transient: str | None = None   # ACKNOWLEDGING or WORKING — an in-flight tool call
_concern: str | None = None     # CONCERNED or ALERT — watchdog


def on_person_detected() -> None:
    global _ambient
    _ambient = ATTENTIVE


def on_person_lost() -> None:
    global _ambient
    _ambient = IDLE


def on_wake_word() -> None:
    global _ambient
    _ambient = ATTENTIVE


def on_tool_acknowledging() -> None:
    global _transient
    _transient = ACKNOWLEDGING


def on_tool_working() -> None:
    global _transient
    _transient = WORKING


def on_tool_done() -> None:
    global _transient
    _transient = None


def on_watchdog_concern() -> None:
    global _concern
    _concern = CONCERNED


def on_watchdog_alert() -> None:
    global _concern
    _concern = ALERT


def on_watchdog_clear() -> None:
    global _concern
    _concern = None


def is_valid(state: str) -> bool:
    """True if state is one of the six recognised names."""
    return state in VALID_STATES


def current() -> str:
    """Resolve the single active state: concern > in-flight tool > ambient.

    Defensive against corruption — always returns a value in VALID_STATES.
    """
    if _concern in VALID_STATES:
        return _concern
    if _transient in VALID_STATES:
        return _transient
    return _ambient if _ambient in VALID_STATES else IDLE


def reset() -> None:
    """Test helper — restore defaults between tests."""
    global _ambient, _transient, _concern
    _ambient, _transient, _concern = IDLE, None, None
