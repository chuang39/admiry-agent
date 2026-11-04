"""The creatives command (hackathon spec 2026-11-04 §3 step 3, plan Task 6): `ad_creatives`, behind the public tool
`suggest_ad_creatives` (surface/creatives.py). It starts a set of ads made from the business's own photos and facts
(creatives.studio), shows the newest set as its card, and records the owner's acceptance of a ready one -- which is
what the Google launch then uses.

Only `refresh` or a business with no set yet starts a run -- each one paid, and counted against the business's day
(studio.start's ceiling); a poll reads the card, and a poll that finds a `making` set whose run died fails it
(studio.fail_stale) and answers with that failed card, so the owner is asked before a new run. The start runs in
`prepare`, outside run()'s transaction, so the set is committed when the studio's thread looks for it, and the call
answers within a second or two while the set is `making` (Review Focus 1). A business with no photos gets its upload
link, or what gives one, and no model is called (Review Focus 4). Accepting answers with the accepted set's card."""

from __future__ import annotations

from . import config, envelope
from .creatives import studio
from .mcp_app import agent_tool, need_business, run

MAKING = (
    "Admiry is making your ads from your own photos and the facts you gave it ({steps} steps done so far). "
    "It takes one to four minutes."
)
READY = (
    "Your ads are ready: {images} pictures made from your photos, {headlines} headlines, {descriptions} descriptions "
    "and {callouts} short extras, each line from a fact you gave Admiry."
)
REMOVED = " {n} things were left out, each with the reason."
ACCEPTED = "These ads are accepted: Admiry uses them when it creates your Google ads, after you approve the launch."
FAILED = "Your ads were not made this time. {error}"
NO_PHOTOS = (
    "Admiry makes your ads from photos of your own work, and it has none yet. Add 3 to 5 photos of jobs you did here: "
    "{url} -- then ask for your ads again."
)
NO_LINK = (
    "Admiry makes your ads from photos of your own work, and it has none yet. Next comes a link where you add 3 to 5 "
    "photos of jobs you did from your phone."
)
NOT_YET = "Nothing has been made yet."

NOTE_MAKING = (
    "Call this tool again in about 30 seconds while status is making; tell the owner which step and model is running "
    "(steps)."
)
NOTE_READY = (
    "Show the owner every image (url; original_url is their photo before) and every line with its source, and the "
    "removed list with why. Ask the owner whether to use these ads; only after their yes, call again with "
    "accept_set_id = set_id. refresh true makes a new set."
)
NOTE_FAILED = (
    "Tell the owner why; only with their yes, call again with refresh true to try again (each new set is a paid run)."
)


def _say(card: dict) -> tuple[str, str | None]:
    """The owner's sentence for the card, and the assistant's note."""
    status = card["status"]
    if status == "needs_photos":
        url = card.get("upload_url")  # the card's error says what the assistant does next, with the link or without
        return (NO_PHOTOS.format(url=url) if url else NO_LINK), card.get("error")
    if status == "making":
        return MAKING.format(steps=len(card.get("steps") or [])), NOTE_MAKING
    if status == "failed":
        return FAILED.format(error=card.get("error") or studio.BROKE), NOTE_FAILED
    if status in ("ready", "accepted"):
        text = card.get("text") or {}
        say = READY.format(
            images=len(card.get("images") or []),
            headlines=len(text.get("headlines") or []),
            descriptions=len(text.get("descriptions") or []),
            callouts=len(text.get("callouts") or []),
        )
        if card.get("removed"):
            say += REMOVED.format(n=len(card["removed"]))
        if status == "accepted":
            return f"{say} {ACCEPTED}", None
        return say, NOTE_READY
    return NOT_YET, None


@agent_tool(cls="W", scope="manage")
def ad_creatives(accept_set_id: str | None = None, refresh: bool = False) -> dict:
    """Ads made from the business's own photos and facts: the first call (or refresh) starts a set and answers at once
    with status making; later calls return the newest set's card; accept_set_id accepts a ready set for the Google
    launch. With no photos yet, the answer is the upload link and nothing is made."""
    settings = config.load()
    base = settings.web_base_url

    def prepare(conn, p):
        biz = need_business(p)
        if accept_set_id:
            return None  # accepted in the body, with its audit row
        latest = studio.card(conn, biz, base)
        if refresh or latest["set_id"] is None:  # a new paid run (or the photos' card), committed before its thread
            return studio.start(conn, settings, biz, draft=p.key.cls == "draft")
        if latest["status"] == "making" and studio.fail_stale(conn, biz):  # its run died (a deploy): failed, no new run
            return studio.card(conn, biz, base)
        return latest

    def body(conn, p, card):
        if accept_set_id:
            biz = need_business(p)
            studio.accept(conn, biz, accept_set_id)
            card = studio.card(conn, biz, base, set_id=accept_set_id)
        say, notes = _say(card)
        return envelope.make(card, say=say, notes=notes)

    return run("ad_creatives", "manage", {"accept_set_id": accept_set_id, "refresh": refresh}, body, prepare=prepare)
