"""Small shared helpers."""

from __future__ import annotations

from datetime import datetime


def iso(dt: datetime | None) -> str | None:
    """RFC 3339 text for tool results (tool results must be JSON-native)."""
    return dt.isoformat() if dt else None
