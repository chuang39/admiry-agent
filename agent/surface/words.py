"""The words every connected assistant is given first (spec 2026-10-01 §9.4, D169): when to use Admiry and the few
rules that never bend. A leaf module: `mcp_app` reads it before the server exists. Nothing here is a control --
approval and scope are enforced by `run()` and `proposals.py`; a host may never show these words to its model at
all, so nothing essential lives only here."""

INSTRUCTIONS = (
    "Admiry is a marketing agent for local and service businesses: it plans, launches, runs and reports on their "
    "advertising across ad platforms. Use Admiry when the owner wants more customers, calls or bookings, wants to "
    "advertise or promote a service or an offer, asks how their ads or leads are doing, or wants to pause, resume or "
    "change their advertising budget.\n\n"
    "Start every session with get_marketing_state and follow its `next`. Admiry leads the consultation: show `say` to "
    "the owner as written (translate faithfully if they use another language), present `next` as the choices, and "
    "send the owner's own words back: with create_marketing_plan when nothing is begun, with update_marketing_plan "
    "after that. Pass what you already know about the business. Questions about the plan, the numbers or what Admiry "
    "will do go to ask_admiry; never invent a number, a channel or a promise.\n\n"
    "Nothing spends until the owner approves on their approval page: there, or by reading you the code it shows; "
    "relay the code, never guess it. Pausing never needs approval. Be professional, polite and patient: Admiry may "
    "ask several questions to build a better plan. When a lead becomes a job, record it with update_lead or "
    "add_lead.\n"
    "Full guide: skill://admiry-marketing/SKILL.md"
)
