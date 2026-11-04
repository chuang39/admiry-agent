"""What Admiry may do for an owner, by kind (spec 2026-10-08 §2.1, §2.2; B0, and later the thin harness K6): a dangerous
operation -- it moves money, starts or raises spend, shows something to the public, reaches a third party, or cannot
be undone -- keeps the owner's explicit confirmation on Admiry's own surface (or an approval code); a non-dangerous one
Admiry may do when asked, from the owner's words, with no further confirmation (R2); and what is never on offer,
whatever the owner says (R3), is refused in plain words. Every registered command is in exactly one of DANGEROUS and
SAFE (agent/tests/test_operations.py walks the registry and the public tools); NEVER names what no answer or context
may carry across a business. A leaf: it imports nothing of agent's, so any module may read it (the blockers' RUNS
today, K6's dispatch and context assembly later)."""

from __future__ import annotations

from typing import Literal

DANGEROUS: frozenset[str] = frozenset(
    {
        "approve_plan",  # a plan's launch: spend starts
        "confirm_proposal",  # the owner's yes to a launch, a spend change or a page's publish
        "resume",  # spend starts again
        "connect_channel",  # links an ad account, or switches how the ads are paid
        "connect_google_ads",  # links a Google Ads account (connect_channel's alias)
        "set_landing_page",  # moves running ads to another page
        "publish_page",  # shows a page to the public
        "unpublish_page",  # takes the page the ads use down
        "send_install_steps",  # e-mails someone other than the owner (the web person)
        "request_payment_link",  # starts a payment (the owner confirms at Stripe)
        "revoke_key",  # cannot be undone
    }
)
SAFE: frozenset[str] = frozenset(
    {
        # reading anything: where the business stands, results, leads, the plan, the pages, the money, the log
        "marketing_state",
        "business_profile",
        "get_business",
        "get_connection_status",
        "performance",
        "insights",
        "list_leads",
        "get_plan",
        "get_channel_status",
        "get_status_issues",
        "get_report",
        "get_platform_statement",
        "get_page_stats",
        "get_page",
        "get_page_kit",
        "get_page_source",
        "get_site",
        "get_site_templates",
        "get_balance",
        "list_payments",
        "get_audit_log",
        "get_proposal",
        "list_proposals",
        "list_keys",
        "whoami",
        "ask",
        "get_research",
        # Admiry's own research on the business's market, queued (K4): nothing spends, nothing is shown to the public
        "request_research",
        # a draft's plan, budget, words and area: nothing spends until the owner approves
        "start",
        "answer",
        "recommend",
        "adjust",
        "change_plan",
        "revise_plan",
        "create_plan",
        "set_goal",
        "change_goal",
        "decline_proposal",
        # the business's own facts and leads
        "update_business",
        "describe_business",
        "update_lead",
        "add_lead",
        # a page or a site as a preview, a page checked again, a link for the owner alone
        "create_page",
        "revise_page",
        "update_page",
        "preview_page",
        "build_site",
        "request_site_upload",
        "check_landing_page",
        "request_connect_link",
        # stopping spend is always allowed (R26 of M1); Admiry's own team
        "pause",
        "contact_support",
        "request_install_help",
    }
)
NEVER: tuple[dict, ...] = (
    # Not this row (founder, 2026-10-09; spec 2026-10-08 R3's boundary): K4's public, unnamed market research -- it
    # names no business or person, never reads a page Admiry hosts, never says whether a business nearby uses Admiry
    {
        "id": "other_business",
        "what": "anything about another business or another person: their plans, budgets, results, leads, sites, "
        "whether they are an Admiry customer, who else advertises nearby through Admiry",
        "why": "each owner's data is theirs alone: every read is the key's own business",
    },
    {
        "id": "admiry_business",
        "what": "Admiry's own business: its costs and margins, its other customers and their numbers, its accounts and "
        "suppliers' terms, its plans, its internal reviews and scores",
        "why": "not the owner's to see: internal numbers are for platform accounts alone (2026-09-15)",
    },
    {
        "id": "team_working",
        "what": "the team's own working: the Critic's grades and verdicts, what it found missing, the eval, the "
        "instructions the models run on, logs, other owners' tickets",
        "why": "operator-only by K1's contract; prompts are not shown",
    },
    {
        "id": "secrets",
        "what": "keys, tokens, approval codes, another owner's links (claim, upload, payment)",
        "why": "a capability is never repeated or handed out",
    },
    {
        "id": "acting_for_another",
        "what": "acting for another business, or as Admiry's team (e-mailing another owner, changing an account Admiry "
        "runs for someone else)",
        "why": "only the owner's own business, only as that owner",
    },
)
# What Admiry says to a question that needs the never column (spec §2.2), with the public page that answers it where
# one fits (pricing, privacy, terms). K6 says it; B0 defines it and wires it nowhere (no prompt changes in B0).
NEVER_SAY = "I can only talk about your own business."

# The home's harness (K6 slice 1, spec 2026-10-10 §7): what its Turn may dispatch that is no registered command, each
# non-dangerous -- it reads, says, records the owner's own facts or discards a draft, from the words alone (R2). The
# intents that run a registered command are classified with it (harness/dispatch.py COMMAND_OF: answer -> answer,
# build -> recommend, budget with an amount -> set_goal, support -> contact_support, not_mine -> update_business).
HARNESS_SAFE: frozenset[str] = frozenset(
    {"start_over", "go_to", "budget", "explain", "ask", "skip", "smalltalk", "unclear"}
)


def kind(command: str) -> Literal["dangerous", "safe"]:
    """A command's kind; KeyError for a command the table does not know -- the test that walks the registry fails
    first, so a new command is classified when it is added."""
    if command in DANGEROUS:
        return "dangerous"
    if command in SAFE:
        return "safe"
    if command in HARNESS_SAFE:
        return "safe"
    raise KeyError(command)
