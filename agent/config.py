"""Environment-driven settings (spec 2026-09-24 §4.6, §9 "Secrets and config").

Read once per process by create_app(); tests set the variables before calling it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Meta Model API reasoning_effort values the service accepts: "none" is refused by the API, "max" is
# Standard-tier only and a long non-streaming call (the API's gateway timeout is undocumented)
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

LINK_MODES = ("api", "manual")  # GOOGLE_ADS_LINK_MODE (spec 2026-09-25 §5, §7)
ADS_EXECUTION_MODES = ("off", "validate", "live")  # AGENT_ADS_EXECUTION (spec 2026-09-28 D49), AGENT_EXEC_<CHANNEL>
ROLES_MODES = ("off", "validate", "on")  # AGENT_ROLES (spec 2026-10-07-admiry-capability-layer-design §8.3)
DIGEST_MODES = ("off", "operator", "on")  # AGENT_DIGEST (spec 2026-09-30-admiry-daily-digest-design §12)
# the switch per channel (spec 2026-09-29 D81): AGENT_EXEC_GOOGLE_SEARCH, AGENT_EXEC_NEWSBREAK, ...
EXEC_PREFIX = "AGENT_EXEC_"
KEY_MISSING = "AGENT_KEY_ENCRYPTION_KEY is not set; an owner token cannot be stored"
_CUSTOMER_ID = re.compile(r"[0-9]{10}")
_HEX_KEY = re.compile(r"(?:[0-9a-fA-F]{2}){32,}")  # what `openssl rand -hex 32` prints: at least 32 bytes as hex


@dataclass(frozen=True)
class Settings:
    database_url: str
    public_base_url: str
    web_base_url: str  # AGENT_WEB_BASE_URL (spec 2026-09-29 §3): browser pages, links, cookies, Google sign-in
    # AGENT_PAGES_DOMAIN (A3p-2 H1): the wildcard host hosted pages serve on, e.g. business.admiry.ai. Unset =
    # hosting off -- preview_page still works (previews live on the agent host), publish_page refuses.
    pages_domain: str | None
    port: int
    build_sha: str
    env: str
    llm_api_key: str | None
    llm_model: str
    llm_effort: str | None
    llm_base_url: str
    places_api_key: str | None
    places_daily_budget: int
    start_daily_budget: int
    google_client_id: str | None
    google_client_secret: str | None = field(repr=False)  # also mints the MCC access tokens
    google_ads_mcc_id: str | None  # Admiry's manager account, 10 digits
    google_ads_developer_token: str | None = field(repr=False)  # never printed with the settings
    google_ads_refresh_token: str | None = field(repr=False)
    stripe_secret_key: str | None = field(repr=False)  # A1b-3 top-ups and refunds; never printed
    stripe_webhook_secret: str | None = field(repr=False)
    topup_min_usd: int
    topup_max_usd: int
    # D92 (channels spec rev. 4): what each way of paying costs, passed on at cost -- operator constants mirroring
    # Stripe's list prices, never per business
    card_cost_bps: int
    card_cost_fixed_cents: int
    ach_cost_bps: int
    ach_cost_cap_cents: int
    ach_topups: bool  # AGENT_ACH_TOPUPS: bank transfers offered beside the card (off until bank returns are handled)
    # AGENT_CARD_LINK: Link by Stripe beside the card, at the card's price -- off until Link is activated in the
    # account's Stripe dashboard (Stripe refuses a Checkout naming a payment method type the account has not)
    card_link: bool
    anon_turns: int  # AGENT_ANON_TURNS (A1b-4c D39): 0 (default) means no anonymous-answer wall
    resend_api_key: str | None = field(repr=False)  # A3p-1 Task 2: the page kit e-mail, shared with website's Apply
    mail_from: str  # AGENT_MAIL_FROM, else "Admiry <email_address>": one sender for all Admiry mail (D59)
    # A1b-5 e-mail channel (spec 2026-09-28 §8): off = the webhook answers 404 and nothing is sent. Its Resend key is
    # resend_api_key above (A3p-1's; one key, one client -- plan P16)
    email_inbound: bool
    resend_webhook_secret: str | None = field(repr=False)
    email_address: str  # hello@agent.admiry.ai; replies go to hello+<thread token>@…
    ops_email: str | None  # where the operator queue's notices go
    email_per_sender_hour: int
    email_per_sender_day: int
    email_per_thread_hour: int
    # AGENT_KEY_ENCRYPTION_KEY (M0): seals the owner-side tokens in channel_account.secret_enc (agent/secretbox.py)
    key_encryption_key: str | None = field(repr=False)
    google_ads_link_mode: str = "manual"
    ads_execution: str = "off"  # AGENT_ADS_EXECUTION (A3 D49): off | validate | live
    ads_ops_daily_budget: int = 2300  # ADS_OPS_DAILY_BUDGET (A3 ruling S4): Google Ads operations the jobs send a day
    # AGENT_EXEC_<CHANNEL> (M0, spec 2026-09-29 D81): {channel id: off | validate | live} for the switches that are set.
    # Read through agent.channels.registry.exec_mode(), which also answers for google_search from ads_execution above.
    exec_modes: dict[str, str] = field(default_factory=dict)
    # AGENT_LAUNCH_ALLOWLIST (2026-10-08): owners' e-mails, lower-cased; set, only their businesses launch while a switch
    # is on (registry.exec_mode_for), every other business runs as if it were off. Empty = no list: all follow the switch
    launch_allowlist: frozenset[str] = field(default_factory=frozenset)
    # spec 2026-09-30 support-and-feedback (D144): tickets, the Help button (spec 2026-10-12), contact_support and the plan
    # thumbs; off = today's behaviour everywhere (to_operator keeps its own notice)
    support: bool = False
    support_reply_to: str | None = None  # AGENT_SUPPORT_REPLY_TO: where ticket replies go while the channel is off
    feedback_plan_page_rate: float = 0.2  # AGENT_FEEDBACK_PLAN_PAGE_RATE: the share of /plan pages with the thumbs
    # M1 (channels spec D88, §9): Admiry's NewsBreak Business API token (the Access-Token header of every call; never
    # printed with the settings) and Admiry's NewsBreak Organization, under which every business's ad account is made
    newsbreak_access_token: str | None = field(default=None, repr=False)
    newsbreak_org_id: str | None = None
    # AGENT_RENDER_URL (A3p-3 batch 4): the render check's own service (renderer/, e.g. http://renderer:8099). Unset = a
    # site bundle is judged by its static checks alone, as before the render check
    render_url: str | None = None
    # AGENT_PAGESPEED_API_KEY (A3p-6 batch 1 Q8): Google's PageSpeed Insights key, for the speed test of an industry
    # site's preview (agent/pagespeed.py; never printed with the settings). Unset = no speed test
    pagespeed_api_key: str | None = field(default=None, repr=False)
    # A3p-5 batch 2 (plan Q8, spec §8 item 1): the address an owner adds as a helper on their site's platform for Admiry's
    # team (AGENT_SITE_HELPER_EMAIL; unset = the help reply names none), and when the team answers (AGENT_SITE_HELP_PROMISE,
    # e.g. "within one business day", a promise kept only once the founder staffs it; unset = "soon")
    site_helper_email: str | None = None
    site_help_promise: str | None = None
    # AGENT_SITES (A3p-6 batch 2 Q22): on (or 1) = a home-services business or a dental practice gets Admiry's own
    # site (the look, the facts, the site built and published); off, 0 or unset = everything as before batch 2: every
    # business gets the one-page starter page, and build_site, get_site and get_site_templates say the sites are not on
    sites: bool = False
    # AGENT_ROLES (spec 2026-10-07 capability layer §8.3): off (unset) = no background role runs; validate = the roles
    # run and store what they found and change nothing; on = the same in K1 (K2's automatic actions read it)
    roles_mode: str = "off"
    # AGENT_KNOWLEDGE (capability layer K3, plan 2026-10-08-agent-k3 P9): on lets a plan draft and the answerer read
    # Admiry's curated knowledge (agent/knowledge); off (unset) leaves both as before K3. The resources serve either way.
    knowledge: bool = False
    # AGENT_HARNESS (capability layer K6 slice 1, spec 2026-10-10): on lets the home's free text while a consultation is
    # open or ready go through the harness (agent/harness: the code fast path, one Turn call, its dispatch); off (unset)
    # leaves the consultation's free text as before K6. Dev sets on for the founder's walk; prod is born off.
    harness: bool = False
    # The daily digest (spec 2026-09-30-admiry-daily-digest-design §12; plan 2026-10-07-agent-k2 Task 1): off (unset) =
    # no digest; operator = previews to AGENT_OPS_EMAIL only; on = the owners' mail. The link secrets sign the
    # unsubscribe links (the previous one keeps sent links working through a rotation); the events secret verifies
    # Resend's bounce and complaint webhook. None of the three is ever printed with the settings.
    digest: str = "off"
    digest_link_secret: str | None = field(default=None, repr=False)
    digest_link_secret_previous: str | None = field(default=None, repr=False)
    digest_run_seconds: int = 2400  # AGENT_DIGEST_RUN_SECONDS: a run stops starting new businesses after this long
    resend_events_webhook_secret: str | None = field(default=None, repr=False)
    # The Analyst (capability layer K2, plan R3/R7): its reasoning effort (spec §5.1 step 5: medium for the take) and
    # the model calls its looks may make a day, all businesses together (0 = none)
    analyst_llm_effort: str = "medium"
    analyst_max_per_day: int = 2000
    # The founder's grading page (capability layer K5, agent/eval_web.py): the Google sign-in e-mails that may grade the
    # golden set, lower-cased; empty = no page. Dev only: AGENT_ENV=prod with it set refuses to boot
    eval_graders: frozenset[str] = field(default_factory=frozenset)
    # AGENT_LAUNCH_REVIEW (L1, spec 2026-10-10): the owner's yes sends a business's first launch to Admiry's review
    # (launch_review), whose pass approves it; off (unset) approves at the yes, as before
    launch_review: bool = False
    # AGENT_ADMIN_EMAILS (L1): who may open /ops/review and run the admin MCP; empty: neither exists
    admin_emails: frozenset[str] = field(default_factory=frozenset)
    # AGENT_REFUSAL_RULES (Part B phase 1, spec 2026-10-12 §4): on lets the active refusal rules act -- a block rule's
    # line left out of every Google build, warn/block items on the checklist, the rules' words in the plan writer's
    # prompt, the Reviewer's stamp; off (unset) = no rule acts anywhere. The daily summary runs either way, and the
    # business's own refused lines (§2.1) make their item either way.
    refusal_rules: bool = False

    @property
    def payments_ready(self) -> bool:
        """Both Stripe keys: without the webhook secret a payment could be taken but never credited."""
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)

    @property
    def newsbreak_ready(self) -> bool:
        """Both NewsBreak values: every call carries the token, and every ad account lives under the Organization."""
        return bool(self.newsbreak_access_token and self.newsbreak_org_id)


def _link_settings(client_id: str | None, client_secret: str | None) -> tuple[str | None, str | None, str | None, str]:
    """(mcc_id, developer_token, refresh_token, link_mode). Unset mode = api when every key the api needs is present
    (the three Google Ads keys and the OAuth client that exchanges the refresh token), else manual. An explicit api
    without them, an unknown mode or a malformed MCC id refuses to boot."""
    mcc = (os.environ.get("GOOGLE_ADS_MCC_ID") or "").replace("-", "").strip() or None
    if mcc is not None and not _CUSTOMER_ID.fullmatch(mcc):
        raise ValueError("GOOGLE_ADS_MCC_ID must be the 10-digit Customer ID of Admiry's Google Ads manager account")
    developer = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN") or None
    refresh = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN") or None
    ready = all((mcc, developer, refresh, client_id, client_secret))
    mode = os.environ.get("GOOGLE_ADS_LINK_MODE") or ("api" if ready else "manual")
    if mode not in LINK_MODES:
        raise ValueError(f"GOOGLE_ADS_LINK_MODE must be api or manual or unset, not {mode!r}")
    if mode == "api" and not ready:
        raise ValueError(
            "GOOGLE_ADS_LINK_MODE=api needs GOOGLE_ADS_MCC_ID, GOOGLE_ADS_DEVELOPER_TOKEN, "
            "GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET"
        )
    return mcc, developer, refresh, mode


def _email_settings(api_key: str | None) -> dict:
    """The e-mail channel's settings (A1b-5). `on` without both Resend values (`api_key` is load()'s one parse of
    RESEND_API_KEY, Settings.resend_api_key -- plan P16), an unknown value, an address with a +tag or more than one
    @, and a limit below 1 all refuse to boot, like AGENT_LLM_EFFORT."""
    inbound = (os.environ.get("AGENT_EMAIL_INBOUND") or "off").lower()
    if inbound not in ("on", "off"):
        raise ValueError(f"AGENT_EMAIL_INBOUND must be on or off or unset, not {inbound!r}")
    webhook_secret = os.environ.get("RESEND_WEBHOOK_SECRET") or None
    if inbound == "on" and not (api_key and webhook_secret):
        raise ValueError("AGENT_EMAIL_INBOUND=on needs RESEND_API_KEY and RESEND_WEBHOOK_SECRET")
    address = (os.environ.get("AGENT_EMAIL_ADDRESS") or "hello@agent.admiry.ai").strip().lower()
    if address.count("@") != 1 or "+" in address or not address.split("@")[0]:
        raise ValueError("AGENT_EMAIL_ADDRESS must be one plain address such as hello@agent.admiry.ai (no +tag)")
    return {
        "email_inbound": inbound == "on",
        "resend_webhook_secret": webhook_secret,
        "email_address": address,
        "ops_email": (os.environ.get("AGENT_OPS_EMAIL") or "").strip().lower() or None,
        "email_per_sender_hour": _whole("AGENT_EMAIL_PER_SENDER_HOUR", 20),
        "email_per_sender_day": _whole("AGENT_EMAIL_PER_SENDER_DAY", 100),
        "email_per_thread_hour": _whole("AGENT_EMAIL_PER_THREAD_HOUR", 10),
    }


def _exec_modes() -> dict[str, str]:
    """Every AGENT_EXEC_<CHANNEL> in the environment as {channel id: mode}. An empty value counts as unset (compose
    passes "" for a variable the box's .env does not set); a mode other than off, validate or live refuses to boot,
    like AGENT_ADS_EXECUTION. A name no channel answers to is harmless: nothing reads it, so nothing switches on."""
    modes: dict[str, str] = {}
    for name, value in sorted(os.environ.items()):
        channel = name.removeprefix(EXEC_PREFIX).lower() if name.startswith(EXEC_PREFIX) else ""
        if not channel or not value:
            continue
        if value not in ADS_EXECUTION_MODES:
            raise ValueError(f"{name} must be one of {', '.join(ADS_EXECUTION_MODES)} or unset, not {value!r}")
        modes[channel] = value
    return modes


def _launch_allowlist() -> frozenset[str]:
    """AGENT_LAUNCH_ALLOWLIST as lower-cased e-mails, split on commas; blanks dropped, so unset and compose's empty
    value are both no list. An entry without an @ refuses to boot (a switch's value pasted into the wrong line)."""
    raw = os.environ.get("AGENT_LAUNCH_ALLOWLIST") or ""
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    if any("@" not in e for e in emails):
        raise ValueError("AGENT_LAUNCH_ALLOWLIST must be owners' e-mail addresses separated by commas")
    return emails


def _encryption_key() -> str | None:
    """AGENT_KEY_ENCRYPTION_KEY, or None when unset or empty. Set to anything but a hex string of at least 32 bytes
    it refuses to boot (a weak or mistyped key would only show when the first owner token is stored); the error never
    quotes the value."""
    key = (os.environ.get("AGENT_KEY_ENCRYPTION_KEY") or "").strip()
    if key and not _HEX_KEY.fullmatch(key):
        raise ValueError("AGENT_KEY_ENCRYPTION_KEY must be a hex string of at least 32 bytes (openssl rand -hex 32)")
    return key or None


def _support_settings(api_key: str | None, email: dict) -> dict:
    """Tickets and feedback (spec 2026-09-30 §11, D144). AGENT_SUPPORT=on needs RESEND_API_KEY and AGENT_OPS_EMAIL (the
    acknowledgement and the ops notice), and AGENT_SUPPORT_REPLY_TO while AGENT_EMAIL_INBOUND is off (an owner's reply
    must reach a person, §5.2). An unknown value, a malformed address or a rate outside [0, 1] refuses to boot, like
    AGENT_LLM_EFFORT."""
    support = (os.environ.get("AGENT_SUPPORT") or "off").lower()
    if support not in ("on", "off"):
        raise ValueError(f"AGENT_SUPPORT must be on or off or unset, not {support!r}")
    reply_to = (os.environ.get("AGENT_SUPPORT_REPLY_TO") or "").strip().lower() or None
    if reply_to is not None and (reply_to.count("@") != 1 or not all(reply_to.split("@"))):
        raise ValueError("AGENT_SUPPORT_REPLY_TO must be one address such as support@admiry.ai")
    if support == "on" and not api_key:
        raise ValueError("AGENT_SUPPORT=on needs RESEND_API_KEY")
    if support == "on" and not email["ops_email"]:
        raise ValueError("AGENT_SUPPORT=on needs AGENT_OPS_EMAIL")
    if support == "on" and not email["email_inbound"] and reply_to is None:
        raise ValueError("AGENT_SUPPORT=on with AGENT_EMAIL_INBOUND=off needs AGENT_SUPPORT_REPLY_TO")
    raw = os.environ.get("AGENT_FEEDBACK_PLAN_PAGE_RATE") or "0.2"
    try:
        rate = float(raw)
    except ValueError:
        rate = -1.0
    if not 0 <= rate <= 1:  # NaN fails this comparison too
        raise ValueError(f"AGENT_FEEDBACK_PLAN_PAGE_RATE must be a number between 0 and 1, not {raw!r}")
    return {"support": support == "on", "support_reply_to": reply_to, "feedback_plan_page_rate": rate}


def _site_help_settings() -> dict:
    """Admiry's team putting Admiry's code on an owner's site (A3p-5 batch 2, plan Q8): the helper's address, one address
    in lower case (a malformed one refuses to boot, as AGENT_SUPPORT_REPLY_TO does: the owner would be told to add it);
    and the promise, the words the help reply says in place of "soon", its closing full stop dropped (the reply closes
    the sentence). Empty counts as unset."""
    helper = (os.environ.get("AGENT_SITE_HELPER_EMAIL") or "").strip().lower() or None
    if helper is not None and (
        helper.count("@") != 1 or not all(helper.split("@")) or any(c.isspace() for c in helper)
    ):
        raise ValueError("AGENT_SITE_HELPER_EMAIL must be one address such as help@admiry.ai")
    promise = (os.environ.get("AGENT_SITE_HELP_PROMISE") or "").strip().rstrip(".").strip() or None
    return {"site_helper_email": helper, "site_help_promise": promise}


def _sites() -> bool:
    """AGENT_SITES: on (or 1) on; off, 0 or unset (or compose's empty value) off, in any case, as its sibling switches
    read; anything else refuses to boot, like AGENT_ACH_TOPUPS."""
    raw = (os.environ.get("AGENT_SITES") or "off").strip().lower()
    if raw not in ("on", "off", "1", "0"):
        raise ValueError(f"AGENT_SITES must be on or off (or 1 or 0) or unset, not {raw!r}")
    return raw in ("on", "1")


def _knowledge() -> bool:
    """AGENT_KNOWLEDGE: on (or 1) on; off, 0 or unset (or compose's empty value) off, in any case, as AGENT_SITES
    reads; anything else refuses to boot."""
    raw = (os.environ.get("AGENT_KNOWLEDGE") or "off").strip().lower()
    if raw not in ("on", "off", "1", "0"):
        raise ValueError(f"AGENT_KNOWLEDGE must be on or off (or 1 or 0) or unset, not {raw!r}")
    return raw in ("on", "1")


def _harness() -> bool:
    """AGENT_HARNESS: on (or 1) on; off, 0 or unset (or compose's empty value) off, in any case, as AGENT_KNOWLEDGE
    reads; anything else refuses to boot."""
    raw = (os.environ.get("AGENT_HARNESS") or "off").strip().lower()
    if raw not in ("on", "off", "1", "0"):
        raise ValueError(f"AGENT_HARNESS must be on or off (or 1 or 0) or unset, not {raw!r}")
    return raw in ("on", "1")


def _launch_review() -> bool:
    """AGENT_LAUNCH_REVIEW: on (or 1) on; off, 0 or unset (or compose's empty value) off, in any case, as AGENT_HARNESS
    reads; anything else refuses to boot."""
    raw = (os.environ.get("AGENT_LAUNCH_REVIEW") or "off").strip().lower()
    if raw not in ("on", "off", "1", "0"):
        raise ValueError(f"AGENT_LAUNCH_REVIEW must be on or off (or 1 or 0) or unset, not {raw!r}")
    return raw in ("on", "1")


def _refusal_rules() -> bool:
    """AGENT_REFUSAL_RULES: on (or 1) on; off, 0 or unset (or compose's empty value) off, in any case, as
    AGENT_LAUNCH_REVIEW reads; anything else refuses to boot."""
    raw = (os.environ.get("AGENT_REFUSAL_RULES") or "off").strip().lower()
    if raw not in ("on", "off", "1", "0"):
        raise ValueError(f"AGENT_REFUSAL_RULES must be on or off (or 1 or 0) or unset, not {raw!r}")
    return raw in ("on", "1")


def _admin_emails() -> frozenset[str]:
    """AGENT_ADMIN_EMAILS as lower-cased e-mails, split on commas, blanks dropped (unset and compose's empty value are no
    list: the admin page answers 404 and the admin MCP refuses to start). An entry that is not one address refuses to
    boot. Unlike AGENT_EVAL_GRADERS, prod reads it: the review queue is prod's too."""
    raw = os.environ.get("AGENT_ADMIN_EMAILS") or ""
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    if any(e.count("@") != 1 or not all(e.split("@")) for e in emails):
        raise ValueError("AGENT_ADMIN_EMAILS must be e-mail addresses separated by commas")
    return emails


def _roles_mode() -> str:
    """AGENT_ROLES: off, validate or on, in any case; unset or compose's empty value = off; anything else refuses to
    boot, like AGENT_ADS_EXECUTION."""
    raw = (os.environ.get("AGENT_ROLES") or "off").strip().lower()
    if raw not in ROLES_MODES:
        raise ValueError(f"AGENT_ROLES must be one of {', '.join(ROLES_MODES)} or unset, not {raw!r}")
    return raw


def _link_secret(name: str) -> str | None:
    """A digest link secret: unset (or compose's empty value) = None; else a hex key of at least 32 bytes, refused at
    boot otherwise without quoting it (it is a secret)."""
    raw = (os.environ.get(name) or "").strip() or None
    if raw is not None and not _HEX_KEY.fullmatch(raw):
        raise ValueError(f"{name} must be a hex string of at least 32 bytes (openssl rand -hex 32)")
    return raw


def _digest_settings(*, api_key: str | None, ops_email: str | None) -> dict:
    """The daily digest (spec 2026-09-30 §12; plan 2026-10-07-agent-k2 Task 1, R3: the note's own switches are the
    Analyst's now). AGENT_DIGEST operator or on needs RESEND_API_KEY, AGENT_OPS_EMAIL and AGENT_DIGEST_LINK_SECRET; an
    unknown mode, a malformed link secret or a run length under 1 refuses to boot."""
    mode = (os.environ.get("AGENT_DIGEST") or "off").strip().lower()
    if mode not in DIGEST_MODES:
        raise ValueError(f"AGENT_DIGEST must be one of {', '.join(DIGEST_MODES)} or unset, not {mode!r}")
    link = _link_secret("AGENT_DIGEST_LINK_SECRET")
    previous = _link_secret("AGENT_DIGEST_LINK_SECRET_PREVIOUS")
    if mode != "off" and not (api_key and ops_email and link):
        raise ValueError(f"AGENT_DIGEST={mode} needs RESEND_API_KEY, AGENT_OPS_EMAIL and AGENT_DIGEST_LINK_SECRET")
    return {
        "digest": mode,
        "digest_link_secret": link,
        "digest_link_secret_previous": previous,
        "digest_run_seconds": _whole("AGENT_DIGEST_RUN_SECONDS", 2400),
        "resend_events_webhook_secret": (os.environ.get("RESEND_EVENTS_WEBHOOK_SECRET") or "").strip() or None,
    }


def _analyst_settings() -> dict:
    """The Analyst's effort (unset = medium, one of EFFORTS) and its day's model calls (unset = 2000, a whole number of
    0 or more); anything else refuses to boot, like AGENT_LLM_EFFORT."""
    effort = (os.environ.get("AGENT_ANALYST_LLM_EFFORT") or "medium").strip()
    if effort not in EFFORTS:
        raise ValueError(f"AGENT_ANALYST_LLM_EFFORT must be one of {', '.join(EFFORTS)} or unset, not {effort!r}")
    raw = (os.environ.get("AGENT_ANALYST_MAX_PER_DAY") or "2000").strip()
    if not raw.isdigit():
        raise ValueError(f"AGENT_ANALYST_MAX_PER_DAY must be a whole number of 0 or more, not {raw!r}")
    return {"analyst_llm_effort": effort, "analyst_max_per_day": int(raw)}


def _eval_graders(env: str) -> frozenset[str]:
    """AGENT_EVAL_GRADERS as lower-cased e-mails, split on commas, blanks dropped (unset and compose's empty value are
    no list). An entry that is not one address refuses to boot; so does any list on prod, where the grading page never
    runs (K5 P7) -- prod's compose never passes the key, so this only catches a hand edit."""
    raw = os.environ.get("AGENT_EVAL_GRADERS") or ""
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    if any(e.count("@") != 1 or not all(e.split("@")) for e in emails):
        raise ValueError("AGENT_EVAL_GRADERS must be e-mail addresses separated by commas")
    if emails and env == "prod":
        raise ValueError("AGENT_EVAL_GRADERS is for the dev box: the grading page never runs on prod")
    return emails


def _whole(name: str, default: int) -> int:
    raw = os.environ.get(name) or str(default)
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if n < 1:
        raise ValueError(f"{name} must be at least 1, not {n}")
    return n


def _bounded(name: str, default: int, high: int) -> int:
    """A whole number in 0..high from the environment (the payment-cost constants, D92); refused at boot otherwise."""
    raw = os.environ.get(name) or str(default)
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if not 0 <= n <= high:
        raise ValueError(f"{name} must be between 0 and {high}, not {n}")
    return n


def load() -> Settings:
    # measured on dev 2026-09-25: at the model's own default effort a `start` (two calls) took 76 s, at
    # "low" 27 s with the same plan quality on the sample; MCP clients time out well before 76 s
    effort = os.environ.get("AGENT_LLM_EFFORT") or "low"
    if effort not in EFFORTS:  # refused at boot, never sent to the API
        raise ValueError(f"AGENT_LLM_EFFORT must be one of {', '.join(EFFORTS)} or unset, not {effort!r}")
    env = os.environ.get("AGENT_ENV") or "local"
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or None
    mcc, developer, refresh, link_mode = _link_settings(client_id, client_secret)
    topup_min = int(os.environ.get("AGENT_TOPUP_MIN_USD") or "200")  # D30: operator-adjustable, whole dollars
    topup_max = int(os.environ.get("AGENT_TOPUP_MAX_USD") or "5000")
    if not 0 < topup_min <= topup_max:  # refused at boot, like AGENT_LLM_EFFORT
        raise ValueError(
            f"AGENT_TOPUP_MIN_USD / AGENT_TOPUP_MAX_USD must satisfy 0 < min <= max, not {topup_min} / {topup_max}"
        )
    ach = os.environ.get("AGENT_ACH_TOPUPS") or "off"
    if ach not in ("off", "on"):
        raise ValueError(f"AGENT_ACH_TOPUPS must be off or on, not {ach!r}")
    card_link = os.environ.get("AGENT_CARD_LINK") or "off"
    if card_link not in ("off", "on"):
        raise ValueError(f"AGENT_CARD_LINK must be off or on, not {card_link!r}")
    anon_turns_raw = os.environ.get("AGENT_ANON_TURNS") or "0"
    try:
        anon_turns = int(anon_turns_raw)
    except ValueError:
        raise ValueError(f"AGENT_ANON_TURNS must be a whole number, not {anon_turns_raw!r}") from None
    if anon_turns < 0:  # refused at boot, like AGENT_LLM_EFFORT
        raise ValueError(f"AGENT_ANON_TURNS must not be negative, not {anon_turns}")
    ads_execution = os.environ.get("AGENT_ADS_EXECUTION") or "off"
    if ads_execution not in ADS_EXECUTION_MODES:  # refused at boot, like AGENT_LLM_EFFORT
        raise ValueError(
            f"AGENT_ADS_EXECUTION must be one of {', '.join(ADS_EXECUTION_MODES)} or unset, not {ads_execution!r}"
        )
    ops_budget_raw = os.environ.get("ADS_OPS_DAILY_BUDGET") or "2300"
    try:
        ads_ops_daily_budget = int(ops_budget_raw)
    except ValueError:
        raise ValueError(f"ADS_OPS_DAILY_BUDGET must be a whole number, not {ops_budget_raw!r}") from None
    if ads_ops_daily_budget <= 0:  # refused at boot, like AGENT_ANON_TURNS
        raise ValueError(f"ADS_OPS_DAILY_BUDGET must be positive, not {ads_ops_daily_budget}")
    public_base = (os.environ.get("AGENT_PUBLIC_BASE_URL") or "http://localhost:8097").rstrip("/")
    resend_api_key = os.environ.get("RESEND_API_KEY") or None  # read once: Settings' field and the channel's check
    email = _email_settings(resend_api_key)
    support = _support_settings(resend_api_key, email)
    return Settings(
        # every value: an empty string (compose passes "" for an unset var) counts as unset
        database_url=os.environ.get("AGENT_DATABASE_URL") or "postgresql:///bidx_agent",
        public_base_url=public_base,
        # the host browsers use (admiry.ai since 2026-09-29); MCP and the OAuth issuer stay on AGENT_PUBLIC_BASE_URL.
        # Unset = one host for both.
        web_base_url=(os.environ.get("AGENT_WEB_BASE_URL") or public_base).rstrip("/"),
        # the pages domain (A3p-2 H1): lowercased, with any leading/trailing dots trimmed; "" (compose's
        # unset marker, or a domain typed as just dots) counts as unset, like every other value here.
        pages_domain=(os.environ.get("AGENT_PAGES_DOMAIN") or "").strip().lower().strip(".") or None,
        port=int(os.environ.get("PORT") or "8097"),
        build_sha=os.environ.get("BIDX_BUILD_SHA") or "dev",
        env=env,
        # the Business Agent's model: Meta's Model API, Contributor tier (user decisions 2026-09-25: Meta may
        # train on prompts and completions; disclosed in the privacy policy)
        llm_api_key=os.environ.get("META_MODEL_API_KEY") or None,
        llm_model=os.environ.get("AGENT_LLM_MODEL")
        or "muse-spark-1.3-contributor",  # compose passes "" for an unset var
        llm_effort=effort,  # unset = "low"
        llm_base_url=(os.environ.get("AGENT_LLM_BASE_URL") or "https://api.meta.ai/v1").rstrip("/"),
        places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY") or None,
        places_daily_budget=int(os.environ.get("AGENT_PLACES_DAILY_BUDGET") or "200"),
        start_daily_budget=int(os.environ.get("AGENT_START_DAILY_BUDGET") or "500"),  # anonymous `start`, all addresses
        # /connect Google sign-in (A1b part 1); both unset = the page says sign-in is not configured
        google_client_id=client_id,
        google_client_secret=client_secret,
        # Admiry's own Google Ads manager account (A1b-2): owners link their accounts to it by Customer ID
        google_ads_mcc_id=mcc,
        google_ads_developer_token=developer,
        google_ads_refresh_token=refresh,
        # Stripe for prepaid (managed) businesses (A1b-3); either key unset = /pay and the balance tools say payments
        # are not set up
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY") or None,
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
        topup_min_usd=topup_min,
        topup_max_usd=topup_max,
        card_cost_bps=_bounded("AGENT_CARD_COST_BPS", 330, 2000),  # 2.9 % + Chargeback Protection's 0.4 %
        card_cost_fixed_cents=_bounded("AGENT_CARD_COST_FIXED_CENTS", 30, 10000),
        ach_cost_bps=_bounded("AGENT_ACH_COST_BPS", 80, 2000),
        ach_cost_cap_cents=_bounded("AGENT_ACH_COST_CAP_CENTS", 500, 10000),
        ach_topups=ach == "on",
        card_link=card_link == "on",
        anon_turns=anon_turns,
        # A3p-1 Task 2: get_page_kit's owner e-mail, sent through Resend; unset = nothing is sent and callers say so.
        # On prod shared with the website's Apply-form mail (one RESEND_API_KEY); the dev box's website reads its own
        # WEBSITE_RESEND_API_KEY, so this key never arms dev's Apply form (deploy/*/docker-compose.yml).
        resend_api_key=resend_api_key,
        # e-mail channel spec 2026-09-28 D59: every Admiry mail, the page kit and lead notices too, comes from hello@
        # (AGENT_EMAIL_ADDRESS), which takes replies -- main spec D12's no-reply@ is retired; AGENT_MAIL_FROM overrides
        mail_from=os.environ.get("AGENT_MAIL_FROM") or f"Admiry <{email['email_address']}>",
        **email,
        key_encryption_key=_encryption_key(),
        google_ads_link_mode=link_mode,
        # A3 (D49): off (no Google write anywhere except agent.ops ads-probe) | validate (every mutate is
        # validateOnly) | live. Prod stays off until the pilot passes on dev.
        ads_execution=ads_execution,
        # A3 ruling S4: the ads-launch and ads-sync jobs stop for the day once they have sent this many operations
        # (Explorer access allows 2,880 a day on real accounts; the default leaves room for approvals and ads-probe).
        ads_ops_daily_budget=ads_ops_daily_budget,
        exec_modes=_exec_modes(),
        launch_allowlist=_launch_allowlist(),
        **support,
        # M1: unset (or empty, compose's marker) = NewsBreak not configured: nothing calls it. Blanks around a value
        # (a stray newline in the box's .env) would break the header, so they are dropped.
        newsbreak_access_token=(os.environ.get("NEWSBREAK_ACCESS_TOKEN") or "").strip() or None,
        newsbreak_org_id=(os.environ.get("NEWSBREAK_ORG_ID") or "").strip() or None,
        render_url=(os.environ.get("AGENT_RENDER_URL") or "").strip().rstrip("/") or None,
        pagespeed_api_key=(os.environ.get("AGENT_PAGESPEED_API_KEY") or "").strip() or None,
        **_site_help_settings(),
        sites=_sites(),
        roles_mode=_roles_mode(),
        knowledge=_knowledge(),
        harness=_harness(),
        **_digest_settings(api_key=resend_api_key, ops_email=email["ops_email"]),
        **_analyst_settings(),
        eval_graders=_eval_graders(env),
        launch_review=_launch_review(),
        admin_emails=_admin_emails(),
        refusal_rules=_refusal_rules(),
    )
