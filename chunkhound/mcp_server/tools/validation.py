"""Parameter validation helpers for MCP tools."""

from typing import Any


def require_param(name: str, value: Any) -> dict[str, str] | None:
    """Return an error dict if value is missing or empty string, None otherwise."""
    if value is None or (isinstance(value, str) and not value):
        return {
            "error": "missing_parameter",
            "message": f"{name} parameter is required",
        }
    return None


def clamp(value: int, min_val: int, max_val: int) -> int:
    """Clamp value to [min_val, max_val]."""
    return max(min_val, min(value, max_val))
