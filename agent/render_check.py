"""The render check from the agent's side (A3p-3 spec §6.4 item 3, P34, P36; plan
2026-10-04-agent-a3p-3-4-render-check.md R5, R8): the renderer service (`renderer/`, its own container, AGENT_RENDER_URL)
opens a site bundle -- its preview host at upload, its own address once a day after it is live -- and answers the
problems a visitor would meet, `{file, rule: "render", message, fix}` like the static checks', and the home page's
screenshots. Unset, there is no render check: a bundle is judged by its static checks alone."""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from . import config

log = logging.getLogger(__name__)

TIMEOUT_S = 180  # the renderer's own budget is 120 s, plus its browser's start and its two-at-once queue
UNAVAILABLE = "Admiry could not open the site to check it just now."
UNAVAILABLE_FIX = "Upload again in a few minutes with a new link."


class Unavailable(Exception):
    """The renderer did not answer, or answered something other than a render."""


def transport() -> httpx.BaseTransport | None:
    """The renderer is called through this (real network: None); a test monkeypatches it to a MockTransport."""
    return None


def on(settings: config.Settings) -> bool:
    return bool(settings.render_url)


def render(settings: config.Settings, url: str, page_id: str) -> dict:
    """Open `url` (a preview host's home page, or a live bundle's): {"pages", "problems", "shots", "partial"}, the
    problems in the static checks' shape. Unavailable when the renderer cannot be reached, refuses, or answers
    something that is not a render."""
    try:
        with httpx.Client(transport=transport(), timeout=TIMEOUT_S) as client:
            r = client.post(f"{settings.render_url}/render", json={"url": url, "page_id": page_id, "max_pages": 10})
    except httpx.HTTPError as e:
        log.warning("render check: the renderer could not be reached: %s", type(e).__name__)
        raise Unavailable(UNAVAILABLE) from None
    if r.status_code != 200:
        log.warning("render check: the renderer answered %s", r.status_code)
        raise Unavailable(UNAVAILABLE)
    try:
        body = r.json()
    except ValueError:
        raise Unavailable(UNAVAILABLE) from None
    if not isinstance(body, dict) or not isinstance(body.get("problems"), list):
        raise Unavailable(UNAVAILABLE)
    problems = [
        {k: str(p.get(k) or "") for k in ("file", "rule", "message", "fix")}
        for p in body["problems"]
        if isinstance(p, dict) and p.get("message")
    ]
    return {
        "pages": body.get("pages") if isinstance(body.get("pages"), list) else [],
        "problems": problems,
        "shots": body.get("shots") if isinstance(body.get("shots"), dict) else {},
        "partial": bool(body.get("partial")),
    }


def shot(rendered: dict, size: str) -> bytes | None:
    """One screenshot's JPEG bytes (`phone`, `tablet` or `desktop`), or None when the render has none that reads as a JPEG."""
    raw = (rendered.get("shots") or {}).get(size)
    if not isinstance(raw, str):
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if data[:3] == b"\xff\xd8\xff" else None
