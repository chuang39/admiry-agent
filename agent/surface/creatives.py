"""Ads made from the owner's own photos (hackathon spec 2026-11-04 §3 step 3): the public tool over `ad_creatives`."""

from __future__ import annotations

from .registry import dispatch, public_tool


@public_tool(cls="W", scope="manage", commands=("ad_creatives",))
def suggest_ad_creatives(accept_set_id: str | None = None, refresh: bool = False) -> dict:
    """Google ads made from the owner's own job photos and the facts Admiry has about the business: a landscape and a
    square picture per photo, plus headlines, descriptions and short extras, each line checked against those facts.
    Call it when the owner asks what ads Admiry would run, or after they added photos. The first call starts the
    making and answers at once with status "making": call it again in about 30 seconds while status is "making"
    (it takes one to four minutes), and tell the owner which step and model is running (steps). Status
    "needs_photos": give the owner upload_url, where they add their photos from a phone, and call again once they
    say the photos are in. Status "ready": show the owner each image (its url; original_url is their own photo
    before, so they see the work shown is their own), each line with its source (the fact it came from, in the
    owner's words), and the Removed list (removed: what was left out and why). Then ask the owner whether to use
    these ads. Only after the owner says yes, call it with accept_set_id set to the card's set_id: the accepted set
    is what Admiry uses when it creates the Google ads, after the owner approves the launch. Never call it with
    accept_set_id without the owner's yes. Status "failed": say why (error); refresh true starts a new set, with
    the owner's yes."""
    return dispatch("ad_creatives", accept_set_id=accept_set_id, refresh=refresh)
