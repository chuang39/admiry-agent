"""The channel table (spec 2026-09-29-admiry-channels-design §5.1, §8, §9; plan 2026-09-29-agent-m0-channel-framework
P6, P7): which adapters exist, what the owner calls each place the ads run, the order the cards show them, which click
id belongs to which channel, the execution switch per channel, and a business's account on each (`channel_account`).
Adapters register themselves when `agent.channels` imports their sub-package; nothing else in the code names a
channel except through here."""

from __future__ import annotations

import logging

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from .. import config, secretbox
from ..config import KEY_MISSING
from .base import Account, Adapter

log = logging.getLogger(__name__)

# D76: the names the owner sees. Company names (Microsoft Advertising, Meta) stay in Settings' small print and the
# agreement.
LABELS = {
    "google_search": "Google Search",
    "microsoft_search": "Bing Search",
    "meta": "Facebook & Instagram",
    "nextdoor": "Nextdoor",
    "lsa": "Google Local Services",
    "newsbreak": "NewsBreak",
    "reddit": "Reddit",
    "chatgpt_ads": "ChatGPT",
}
ORDER = ("google_search", "newsbreak", "microsoft_search", "meta", "nextdoor", "lsa", "reddit", "chatgpt_ads")
# The click id a visit carries -> the channel it came from (§8). track._click looks for them in this order, so a
# visitor who carries two is credited to the first: Google's three come first.
CLICK_ID_KINDS = {
    "gclid": "google_search",
    "gbraid": "google_search",
    "wbraid": "google_search",
    "msclkid": "microsoft_search",
    "fbclid": "meta",
    "ndclid": "nextdoor",
    "nb_cid": "newsbreak",
    "rdt_cid": "reddit",
    "oppref": "chatgpt_ads",
}
CHANNELS: dict[str, Adapter] = {}  # channel id -> its adapter, filled as `agent.channels` imports the shipped ones
# Where a business's account on a channel stands (`channel_account.status`, written only through `save_account`, which
# refuses any other word): get_connected_accounts names these, and the public contract lists them
ACCOUNT_STATUSES = ("none", "pending", "active", "refused", "inactive", "needs_attention")


def register(adapter: Adapter) -> Adapter:
    """Make `adapter` the one for its `channel`; returns it, so a module can end with `ADAPTER = register(...)`.
    ValueError for an id LABELS does not name (`label` would show the owner the raw id) and for a channel that
    already has an adapter (two modules claiming one channel is a bug, not a replacement)."""
    channel = adapter.channel
    if channel not in LABELS:
        raise ValueError(f"channel {channel!r} has no owner name in LABELS")
    if channel in CHANNELS:
        raise ValueError(f"channel {channel!r} already has an adapter")
    CHANNELS[channel] = adapter
    return adapter


def adapter(channel: str) -> Adapter:
    try:
        return CHANNELS[channel]
    except KeyError:
        raise ToolError("unknown channel") from None


def known(channel: str) -> bool:
    """Whether an adapter is registered for `channel`, that is, whether `adapter(channel)` answers."""
    return channel in CHANNELS


def label(channel: str) -> str:
    """The name the owner sees (D76); a channel this code does not name is shown by its id, never an error."""
    return LABELS.get(channel, channel)


def channel_for_click(kind: str | None) -> str | None:
    """The channel a click id kind (gclid, nb_cid, ...) belongs to; None for no kind or one nobody uses."""
    return CLICK_ID_KINDS.get(kind) if kind else None


def exec_mode(settings: config.Settings, channel: str) -> str:
    """off | validate | live | live_paused for one channel (D81, P7; live_paused: N4, Google Search only, never turned
    on -- agent/ads_run.py): its AGENT_EXEC_<CHANNEL>; for google_search alone, and for one
    release, the old AGENT_ADS_EXECUTION when that is unset; else off. An explicit `off` is a value and wins over the
    alias, so a box can switch Google off without touching the old variable."""
    mode = settings.exec_modes.get(channel)
    if mode is None and channel == "google_search":
        mode = settings.ads_execution
    return mode or "off"


def exec_mode_for(conn: psycopg.Connection, settings: config.Settings, business_id: str, channel: str) -> str:
    """exec_mode for one business: `off` while AGENT_LAUNCH_ALLOWLIST is set and the business's owner is not on it
    (a business with no owner never is). Read where a launch is decided -- the approval and the launch step -- so a
    business off the list is approved, queued and stepped exactly as under `off`."""
    mode = exec_mode(settings, channel)
    if mode == "off" or not settings.launch_allowlist:
        return mode
    row = conn.execute("SELECT lower(owner_email) AS email FROM business WHERE id = %s", (business_id,)).fetchone()
    return mode if row and row["email"] in settings.launch_allowlist else "off"


def _open(sealed: bytes | None, business_id: str, channel: str) -> str | None:
    """The stored token in the clear, or None: none stored, no key on this box, or a key that cannot open it (a
    rotated key strands what the old one sealed; the owner reconnects). One warning line, never the bytes."""
    if sealed is None:
        return None
    key = config.load().key_encryption_key
    if key is None:
        log.warning(
            "channel_account %s/%s holds a token, but AGENT_KEY_ENCRYPTION_KEY is not set", business_id, channel
        )
        return None
    try:
        return secretbox.decrypt(bytes(sealed), key)
    except secretbox.Undecryptable:
        log.warning(
            "channel_account %s/%s holds a token that AGENT_KEY_ENCRYPTION_KEY cannot open", business_id, channel
        )
        return None


def account(conn: psycopg.Connection, business_id: str, channel: str) -> Account | None:
    """The business's account on a channel, its token decrypted with AGENT_KEY_ENCRYPTION_KEY; None without a row."""
    row = conn.execute(
        "SELECT mode, status, external, secret_enc FROM channel_account WHERE business_id = %s AND channel = %s",
        (business_id, channel),
    ).fetchone()
    if row is None:
        return None
    return Account(
        business_id=business_id,
        channel=channel,
        mode=row["mode"],
        status=row["status"],
        external=row["external"],
        secret=_open(row["secret_enc"], business_id, channel),
    )


def save_account(
    conn: psycopg.Connection,
    business_id: str,
    channel: str,
    *,
    mode: str,
    status: str,
    external: dict,
    secret: str | None = None,
    last_error: str | None = None,
) -> None:
    """Insert or update the business's account on a channel. Everything given replaces what was stored -- `last_error`
    too, so None clears it -- except `secret` (an owner-side token): it is sealed with AGENT_KEY_ENCRYPTION_KEY
    before it reaches the database, and None leaves the stored one as it is. A token with no key configured raises
    ValueError(KEY_MISSING) and writes nothing, and so does a `status` outside ACCOUNT_STATUSES. Tokens never go in
    `external`."""
    if status not in ACCOUNT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ACCOUNT_STATUSES)}, not {status!r}")
    sealed = None
    if secret is not None:
        key = config.load().key_encryption_key
        if key is None:
            raise ValueError(KEY_MISSING)
        sealed = secretbox.encrypt(secret, key)
    conn.execute(
        "INSERT INTO channel_account (business_id, channel, mode, status, external, secret_enc, last_error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (business_id, channel) DO UPDATE SET mode = EXCLUDED.mode, status = EXCLUDED.status, "
        "external = EXCLUDED.external, secret_enc = COALESCE(EXCLUDED.secret_enc, channel_account.secret_enc), "
        "last_error = EXCLUDED.last_error, updated_at = now()",
        (business_id, channel, mode, status, Jsonb(external), sealed, last_error),
    )


def managed_channels(conn: psycopg.Connection, business_id: str) -> list[str]:
    """The channels Admiry runs from its own account for this business (mode = 'managed'), in card order; one whose
    id this code does not know comes last. The one question every money path asks about which channels are managed
    (whether the business pays through Admiry at all is `ads_mode.current`)."""
    rows = conn.execute(
        "SELECT channel FROM channel_account WHERE business_id = %s AND mode = 'managed'", (business_id,)
    ).fetchall()
    return sorted((r["channel"] for r in rows), key=lambda c: (ORDER.index(c) if c in ORDER else len(ORDER), c))
