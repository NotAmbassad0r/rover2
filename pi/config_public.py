"""Read-only config exposure with secret masking."""

from __future__ import annotations

import copy
import re
from typing import Any

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|private)",
    re.IGNORECASE,
)


def mask_config(data: Any) -> Any:
    """Return a copy of config safe for GET /api/config (secrets redacted)."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if _SECRET_KEY.search(str(key)):
                out[key] = "***"
            else:
                out[key] = mask_config(value)
        return out
    if isinstance(data, list):
        return [mask_config(item) for item in data]
    return data


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """Shallow-safe public view of loaded config.yaml."""
    return mask_config(copy.deepcopy(config))
