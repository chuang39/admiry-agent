"""Owner-facing times on Settings (spec 2026-09-29-admiry-settings-redesign §5.3): a relative phrase for the page,
the exact UTC time for its title attribute, and a machine form for <time datetime>. Never a raw ISO string on
screen."""

from __future__ import annotations

from datetime import UTC, datetime


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def when(dt: datetime | None, now: datetime | None = None) -> dict:
    if dt is None:
        return {"rel": "never", "abs": "", "iso": ""}
    now = now or datetime.now(UTC)
    dt = dt.astimezone(UTC)
    secs = (now - dt).total_seconds()
    if secs < 60:  # a future time (clock skew between Postgres and the app) reads as "just now" too
        rel = "just now"
    elif secs < 3600:
        rel = _plural(int(secs // 60), "minute")
    elif secs < 86400:
        rel = _plural(int(secs // 3600), "hour")
    elif secs < 2 * 86400:
        rel = "yesterday"
    elif dt.year == now.year:
        rel = f"{dt:%b} {dt.day}"
    else:
        rel = f"{dt:%b} {dt.day}, {dt.year}"
    return {"rel": rel, "abs": f"{dt:%b} {dt.day}, {dt.year}, {dt:%H:%M} UTC", "iso": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
