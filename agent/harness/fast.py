"""The code paths of a home turn that spend no model call (spec 2026-10-10 K6 slice 1 §4.1): the words that start
over, whole; what a skip picks on a choice question. turn.run tries them before any material is assembled."""

from __future__ import annotations

import re

from .. import profile

# Words that, whole, ask to start over (the founder's walk 2026-10-09: "i wanna restart" at the third question was read
# as its answer), after profile.norm: an optional lead-in, the phrase, an optional thanks -- never a word inside a longer
# answer ("restart my membership" answers the services question).
START_OVER_WORDS = re.compile(
    r"(?:(?:i|we)\s+(?:want|wanna|need|would\s+like|d\s+like)\s+(?:to\s+)?|(?:can|could)\s+(?:we|i)\s+|let\s*s\s+"
    r"|let\s+me\s+|please\s+)?"
    r"(?:restart|start\s+(?:over|again|afresh|from\s+(?:the\s+)?(?:scratch|beginning|top|start))|begin\s+again)"
    r"(?:\s+(?:please|thanks|thank\s+you))?"
)
SKIP_OPTION_IDS = ("any", "estimate")  # the option a skip picks on a choice question that offers one (D5)
CHOICE_INPUTS = ("single", "multi", "amount", "radius")


def says_start_over(text: str) -> bool:
    """Whether the whole message asks to start over."""
    return START_OVER_WORDS.fullmatch(profile.norm(text or "")) is not None


def skip_option(question: dict, options: list[dict]) -> str | None:
    """The option a skip picks on a choice question ("Any of these", "Not sure, estimate for me"); None without one,
    for a question answered in words (text, address, website) and for the Google Maps place, which words never skip."""
    if question.get("input") not in CHOICE_INPUTS or question.get("options_from") == "listing":
        return None
    return next((o["id"] for o in options if o.get("id") in SKIP_OPTION_IDS), None)
