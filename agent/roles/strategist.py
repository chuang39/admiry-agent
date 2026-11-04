"""The Strategist's knowledge (capability layer K3; spec 2026-10-07 §4's Strategist row, §6.1; plan
2026-10-08-agent-k3-knowledge-v1 P3, P4, P7, P8): which curated files a plan draft reads -- its consultation tree's,
for the channels the plan may run on and the business's trade, within the retrieval budget -- which of the ids the
draft names are kept (only ids it was given, each once, in its order), and what the answerer reads of a plan's
knowledge (the files it cites). The Strategist stays inline (recommend, a rewrite, the setup card's redraft) and
makes no model call of its own. Nothing here reads a file while AGENT_KNOWLEDGE is off."""

from __future__ import annotations

from .. import config, knowledge_index, plans, plans_channels, profile, trades

# A home-services business's trade, read from words (agent/trades.py, the table the industry sites read too): the first
# trade named in what the business does (`category_detail`), else in the `trade` the owner set, else in the service its
# consultation leads with, else in the services it names. The first batch of knowledge files has one per trade here
# (spec §6.1); a business with no trade here reads its vertical's files alone.
TRADES: tuple[str, ...] = trades.NAMES
COMPLIANCE, SEARCH, NEWSBREAK, OFFERS = "compliance", "google-search", "newsbreak", "offers"
GOOGLE = "google_search"


def _joined(values) -> str:
    """The text among a stored list's items, joined: knowledge is as a model or an owner's patch wrote it, so an item
    that is no text, or a list that is none, names nothing."""
    return " ".join(v for v in values if isinstance(v, str)) if isinstance(values, list) else ""


def trade(knowledge: dict, consultation: dict) -> str | None:
    """The trade the business's words name: what it does, then the `trade` the owner set (`plumbing` or
    `home_services/plumbing`, as the sites, the consultation's options and managed Google read it), then the service
    its consultation leads with, then every service the consultation names, then every service the business lists;
    the first text that names one decides."""
    texts = (
        knowledge.get("category_detail"),
        knowledge.get("trade"),
        consultation.get("service"),
        _joined(consultation.get("services")),
        " ".join(s["name"] for s in profile.services(knowledge)),
    )
    return next((found for text in texts if (found := trades.trade_in(text))), None)


def channels(settings: config.Settings, flags: set[str], previous: dict | None) -> tuple[str, ...]:
    """Where the plan's ad text may run: Google Search always (the draft writes its words; the allocator copies them
    to the other channels), and NewsBreak while its switch is on and either the service fits it (`newsbreak_fit`) or
    the version this one starts from funds or names it."""
    prev = previous or {}
    named = "newsbreak" in plans.pins(prev) or any(c.get("channel") == "newsbreak" for c in plans.funded(prev))
    if plans_channels.switched_on(settings, "newsbreak") and ("newsbreak_fit" in flags or named):
        return (GOOGLE, "newsbreak")
    return (GOOGLE,)


def topics(trade_name: str | None, chans: tuple[str, ...]) -> tuple[str, ...]:
    """The order a draft needs them in (plan P7): what the ads must not say, the trade's own, Google Search, NewsBreak
    when the plan may run there, then offers -- the budget drops the last first."""
    return (
        COMPLIANCE,
        *([trade_name] if trade_name else []),
        SEARCH,
        *([NEWSBREAK] if "newsbreak" in chans else []),
        OFFERS,
    )


def knowledge_for(
    settings: config.Settings,
    vertical: str,
    knowledge: dict,
    consultation: dict,
    flags: set[str],
    *,
    previous: dict | None = None,
) -> knowledge_index.Retrieved | None:
    """What a draft reads: `vertical` is the consultation's tree id, `consultation` its facts (consult.facts), `flags`
    the chosen service's fit flags (consult.service_flags), `previous` the sections a rewrite starts from. None while
    the switch is off or when the tree has no reviewed file to give."""
    if not settings.knowledge:
        return None
    chans = channels(settings, flags, previous)
    found = knowledge_index.retrieve(vertical, channels=chans, topics=topics(trade(knowledge, consultation), chans))
    return found if found.ids else None


def kept(ids: list[str], given: knowledge_index.Retrieved | None) -> list[str]:
    """The ids the draft names that it was given, each once, in its order (plan P4)."""
    allowed = set(given.ids) if given else set()
    out: list[str] = []
    for raw in ids:
        knowledge_id = raw.strip()
        if knowledge_id in allowed and knowledge_id not in out:
            out.append(knowledge_id)
    return out


def for_answer(settings: config.Settings, rationale: dict | None) -> str | None:
    """What the answerer reads of a plan's knowledge (plan P8): the files the plan cites (`rationale.knowledge_ids`),
    in its order, within the budget; None while the switch is off or when the plan cites none still on file."""
    if not settings.knowledge:
        return None
    ids = (rationale or {}).get("knowledge_ids") or []
    if not ids:
        return None
    return knowledge_index.cited(ids).text or None
