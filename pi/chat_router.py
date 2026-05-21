"""NL command router — map free-text to rover actions.

Pattern-matched first (zero LLM cost). Only 'describe' triggers VLM.
Order matters: more specific patterns before generic ones.
"""
from __future__ import annotations

import re
from typing import Any

# (regex, action) — first match wins
_PATTERNS: list[tuple[str, str]] = [
    (r"\b(stop follow(?:ing)?|unfollow|stop tracking|cancel follow)\b", "unfollow"),
    (r"\b(stop detect(?:ing)?|detection off|stop looking)\b", "undetect"),
    (r"\b(stop|halt|freeze|stand still|don.?t move)\b", "stop"),
    (r"\b(forward|go forward|move forward|advance|ahead)\b", "forward"),
    (r"\b(back(?:ward)?|reverse|go back)\b", "backward"),
    (r"\b(?:turn )?left\b", "left"),
    (r"\b(?:turn )?right\b", "right"),
    (r"\b(follow me|follow|track me|come (?:here|to me))\b", "follow"),
    (r"\b(detect(?:ion)?|start detect(?:ing)?|look for (?:a )?person)\b", "detect"),
    (r"\b(what (?:do you|can you) see|describe|look around|what.?s (?:there|in front|around))\b",
     "describe"),
    (r"\b(open grip(?:per)?|grip(?:per)? open)\b", "grip_open"),
    (r"\b(close grip(?:per)?|grip(?:per)? close)\b", "grip_close"),
    (r"\b(arm up|lift(?: arm)?)\b", "arm_up"),
    (r"\b(arm down|lower(?: arm)?)\b", "arm_down"),
]

_REPLIES: dict[str | None, str | None] = {
    "stop":      "Stopping.",
    "forward":   "Moving forward.",
    "backward":  "Reversing.",
    "left":      "Turning left.",
    "right":     "Turning right.",
    "follow":    "Following you.",
    "unfollow":  "Stopped following.",
    "detect":    "Detection on.",
    "undetect":  "Detection off.",
    "describe":  None,      # reply is the VLM response
    "grip_open": "Opening gripper.",
    "grip_close": "Closing gripper.",
    "arm_up":    "Arm moving up.",
    "arm_down":  "Arm moving down.",
    None:        "Unrecognised command. Try: forward, stop, left, right, follow me, describe.",
}


def route(text: str) -> dict[str, Any]:
    """Return routing info for *text*.

    Keys:
      action       — str or None
      reply        — canned reply str, or None (VLM/caller fills it in)
      needs_camera — True only for 'describe'
    """
    lower = text.lower().strip()
    for pattern, action in _PATTERNS:
        if re.search(pattern, lower):
            return {
                "action": action,
                "reply": _REPLIES.get(action),
                "needs_camera": action == "describe",
            }
    return {"action": None, "reply": _REPLIES[None], "needs_camera": False}
