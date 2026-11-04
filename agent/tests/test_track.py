import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import landing, mail, track
from agent.channels import registry
from agent.tests.fixtures import business_with_key

CORS = "access-control-allow-origin"
NAVIGATION = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"  # a browser's own form post
SCRIPT = Path(__file__).parents[1] / "static" / "admiry.js"
# every click id the snippet keeps, with the channel a lead that carries it belongs to (spec 2026-09-29 §8)
CLICKS = [
    ("gclid", "google_search"),
    ("gbraid", "google_search"),
    ("wbraid", "google_search"),
    ("msclkid", "microsoft_search"),
    ("fbclid", "meta"),
    ("ndclid", "nextdoor"),
    ("nb_cid", "newsbreak"),
    ("rdt_cid", "reddit"),
    ("oppref", "chatgpt_ads"),
]


def _resend(monkeypatch) -> list:
    """Mail on, answered by a fake Resend; the requests it saw."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent = []
    handler = httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=handler))
    return sent


def _page(conn):
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    return key, page


class _Spy:
    """A connection that keeps the SQL it is asked to run."""

    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def execute(self, query, params=None, **kwargs):
        self.sql.append(str(query))
        return self.conn.execute(query, params, **kwargs)


def test_the_page_a_beacon_names_is_read_by_the_columns_it_uses_never_the_wide_ones(conn):
    """Every visitor's beacon reads the page row: an industry site's draft (A3p-6 batch 2) is KBs of JSON it would
    carry for nothing, so the beacon and the form read the three columns they use."""
    _key, page = _page(conn)
    conn.execute("UPDATE page SET site_draft = %s WHERE id = %s", (Jsonb({"facts": {"about": "x" * 400}}), page["id"]))
    spy = _Spy(conn)
    live = track._live_page(spy, page["id"])
    assert live is not None and set(live) == {"id", "business_id", "status"} and live["id"] == page["id"]
    assert spy.sql and not any("site_draft" in q or "sections" in q or "html" in q for q in spy.sql)
    conn.execute("UPDATE page SET status = 'unpublished' WHERE id = %s", (page["id"],))
    assert track._live_page(spy, page["id"]) is None and track._live_page(spy, "x" * 41) is None


def test_the_script_is_served_small_and_cross_origin(client):
    r = client.get("/t/admiry.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers[CORS] == "*" and "max-age=300" in r.headers["cache-control"]
    assert len(r.content) <= 6 * 1024
    js = r.text
    for token in ("/t/event", "/t/form/", "sendBeacon", "calendly.event_scheduled", "cookie_domain"):
        assert token in js, token
    for click_id, _channel in CLICKS:
        assert click_id in js, click_id
    assert "document.cookie" not in js and "innerHTML" not in js
    # C3: a sent form is counted next to its lead, never by a beacon; the Google conversion only for a real lead --
    # the one the answer names (A3p-3 P25: an appointment request's own, else the form's)
    assert 'beacon("form")' not in js and 'if (j.lead) conversion(j.conversion || "form")' in js
    # A3p-3 P29: what brought the visitor is kept in the site's own storage (90 days), not the tab's
    assert "localStorage" in js and "sessionStorage" not in js and "90 * 864e5" in js
    # I4: only the forms meant for Admiry are sent -- action <base>/t/form/…, or marked -- never a method="get" one
    assert 'getAttribute("data-admiry") === "form"' in js and '.toLowerCase() === "get"' in js


def test_events_are_recorded_with_the_click_id(client, conn):
    _key, page = _page(conn)
    r = client.post(
        "/t/event",
        data={"page": page["id"], "kind": "call", "gclid": "Cj0abc", "url": "https://brightsmile.example/invisalign"},
        headers={"Origin": "https://brightsmile.example"},
    )
    assert r.status_code == 204 and r.headers[CORS] == "*"
    row = conn.execute(
        "SELECT kind, click_id, click_id_kind FROM page_event WHERE page_id = %s", (page["id"],)
    ).fetchone()
    assert (row["kind"], row["click_id"], row["click_id_kind"]) == ("call", "Cj0abc", "gclid")
    assert (
        client.options(
            "/t/event", headers={"Origin": "https://x.example", "Access-Control-Request-Method": "POST"}
        ).status_code
        == 204
    )


@pytest.mark.parametrize(("kind", "channel"), CLICKS)
def test_every_click_id_kind_is_stored_with_the_event_and_the_lead_and_names_the_channel(client, conn, kind, channel):
    """The beacon sends the id under its own name, the form under an underscore (the snippet's hidden input); the
    lead's channel is the one the kind belongs to (spec 2026-09-29 §8)."""
    _key, page = _page(conn)
    assert client.post("/t/event", data={"page": page["id"], "kind": "call", kind: "abc123"}).status_code == 204
    r = client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199", "_" + kind: "abc123"})
    assert r.json()["lead"] is True
    events = conn.execute("SELECT kind, click_id, click_id_kind FROM page_event ORDER BY kind").fetchall()
    assert [(e["kind"], e["click_id"], e["click_id_kind"]) for e in events] == [
        ("call", "abc123", kind),
        ("form", "abc123", kind),
    ]
    lead = conn.execute("SELECT click_id, click_id_kind, channel FROM lead").fetchone()
    assert (lead["click_id"], lead["click_id_kind"], lead["channel"]) == ("abc123", kind, channel)


def test_a_visitor_carrying_two_click_ids_is_credited_to_the_first_in_the_registry_order(client, conn):
    _key, page = _page(conn)
    url = f"/t/form/{page['id']}"
    both = {"name": "Ana", "phone": "6505550199", "_nb_cid": "nb1", "_fbclid": "fb1"}
    client.post(url, data=both)
    client.post(url, data={**both, "_gclid": "g1"})
    client.post(url, data={"name": "Ana", "phone": "6505550199", "_nb_cid": "nb1"})
    rows = conn.execute("SELECT click_id_kind, channel FROM lead ORDER BY created_at").fetchall()
    assert [(r["click_id_kind"], r["channel"]) for r in rows] == [
        ("fbclid", "meta"),
        ("gclid", "google_search"),
        ("nb_cid", "newsbreak"),
    ]


def test_a_click_id_is_kept_up_to_200_characters_and_not_beyond(client, conn):
    _key, page = _page(conn)
    url = f"/t/form/{page['id']}"
    client.post(url, data={"name": "Ana", "phone": "6505550199", "_nb_cid": "x" * 200})
    client.post(url, data={"name": "Bo", "phone": "6505550198", "_nb_cid": "x" * 201})
    rows = conn.execute("SELECT click_id, click_id_kind, channel FROM lead ORDER BY created_at").fetchall()
    assert [(len(r["click_id"] or ""), r["click_id_kind"], r["channel"]) for r in rows] == [
        (200, "nb_cid", "newsbreak"),
        (0, None, None),
    ]


def test_a_lead_without_a_click_id_has_no_channel(client, conn):
    _key, page = _page(conn)
    client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199"})
    lead = conn.execute("SELECT click_id, click_id_kind, channel FROM lead").fetchone()
    assert (lead["click_id"], lead["click_id_kind"], lead["channel"]) == (None, None, None)


# The snippet run under Node with a stub page (test_site_js.py's way): it reads the address's query, the referrer and
# the site's storage (A3p-3 P29: what brought the visitor, kept 90 days), then a form is submitted and, when asked,
# Calendly's message arrives twice. Prints what reached the beacons, the storage, the form's hidden inputs and gtag.
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const cfg = JSON.parse(process.argv[2]);
const store = new Map(Object.entries(cfg.storage || {})), beacons = [], hidden = [], on = {}, gtag = [];
const stub = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
stub("localStorage", { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k) });
Date.now = () => cfg.now;
stub("location", { search: cfg.search, hostname: "shop.example", href: "https://shop.example/landing" + cfg.search });
stub("navigator", { sendBeacon: (url, body) => (beacons.push([url, body.toString()]), true) });
const win = { addEventListener: (type, fn) => { on["window:" + type] = fn; } };
stub("window", win);
stub("fetch", () => new Promise(() => {}));
stub("FormData", class {});
stub("document", {
  currentScript: { dataset: { page: "pg_1", ...(cfg.dataset || {}) }, src: "https://agent.example/t/admiry.js" },
  referrer: cfg.referrer || "",
  addEventListener: (type, fn) => { on[type] = fn; },
  createElement: () => ({}),
  head: { appendChild() {} },
});
eval(src);
if (win.gtag) for (const call of win.dataLayer) gtag.push(Array.from(call).map((x) => (x instanceof Date ? "date" : x)));
const attrs = { method: "post", action: "https://agent.example/t/form/pg_1", "data-admiry-intent": cfg.intent };
const form = {
  tagName: "FORM", elements: {}, querySelector: () => null,
  getAttribute: (n) => attrs[n] ?? null,
  appendChild: (input) => hidden.push([input.name, input.value]),
};
on.submit({ target: form, preventDefault() {} });
if (cfg.calendly) for (const origin of ["https://calendly.com", "https://calendly.com", "https://evil.example"])
  on["window:message"]({ origin, data: { event: "calendly.event_scheduled" } });
console.log(JSON.stringify({ beacons, storage: [...store], hidden, gtag }));
"""
NOW = 1_800_000_000_000  # the stubbed Date.now(), in milliseconds
DAY = 86_400_000


def _run(**cfg) -> dict:
    """What the snippet did on a page with `cfg`: search, storage, now, referrer, dataset, intent, calendly."""
    out = subprocess.run(
        ["node", "-e", HARNESS, str(SCRIPT), json.dumps({"search": "", "now": NOW, **cfg})],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _visit(storage: list) -> dict | None:
    found = dict(storage).get("admiry_visit")
    return json.loads(found) if found else None


def _load_page(search: str, kept: dict) -> dict:
    """What the snippet did on a page whose address ends in `search` on a site that kept the click ids `kept` (as
    {"admiry_<kind>": id}, the form the tab's session had them in before A3p-3): `session` is the ids the site keeps
    after, in that same form."""
    ids = {k.removeprefix("admiry_"): v for k, v in kept.items()}
    storage = {"admiry_visit": json.dumps({"ids": ids, "utm": {}, "ref": "", "t": NOW - DAY})} if ids else {}
    out = _run(search=search, storage=storage)
    visit = _visit(out["storage"]) or {"ids": {}}
    return {**out, "session": [[f"admiry_{k}", v] for k, v in visit["ids"].items()]}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="drives admiry.js with node")


@needs_node
def test_the_script_keeps_a_newsbreak_click_id_in_the_storage_the_beacon_and_the_form():
    first = _load_page("?nb_cid=abc", {})
    assert dict(first["session"]) == {"admiry_nb_cid": "abc"}
    assert parse_qs(first["beacons"][0][1]) == {
        "page": ["pg_1"],
        "kind": ["visit"],
        "url": ["https://shop.example/landing?nb_cid=abc"],
        "nb_cid": ["abc"],
    }
    assert first["hidden"] == [["_nb_cid", "abc"]]
    # the visitor moves on to a page whose address carries nothing: the site's storage still has it
    later = _load_page("", dict(first["session"]))
    assert parse_qs(later["beacons"][0][1])["nb_cid"] == ["abc"]
    assert later["hidden"] == [["_nb_cid", "abc"]]


@needs_node
def test_the_last_click_wins_a_new_click_id_drops_the_other_kinds_the_tab_kept():
    """A Task 1 review item (M0 Task 6): a tab that arrived from Google and later from NewsBreak credits NewsBreak --
    an address that carries a click id drops the stored ids of every other kind, from the session, the beacon and
    the form; an address without one keeps what the tab has."""
    google = _load_page("?gclid=g1", {})
    assert dict(google["session"]) == {"admiry_gclid": "g1"}
    later = _load_page("?nb_cid=n1", dict(google["session"]))
    assert dict(later["session"]) == {"admiry_nb_cid": "n1"}
    beacon = parse_qs(later["beacons"][0][1])
    assert "gclid" not in beacon and beacon["nb_cid"] == ["n1"] and later["hidden"] == [["_nb_cid", "n1"]]
    both = _load_page("?gclid=g2&gbraid=b2", dict(later["session"]))  # one click can carry two of Google's kinds
    assert dict(both["session"]) == {"admiry_gclid": "g2", "admiry_gbraid": "b2"}
    plain = _load_page("", dict(both["session"]))  # the next page of the same visit
    assert dict(plain["hidden"]) == {"_gclid": "g2", "_gbraid": "b2"}


@needs_node
def test_the_script_keeps_every_click_id_the_registry_knows():
    ids = {kind: f"v_{kind}" for kind in registry.CLICK_ID_KINDS}
    page = _load_page("?" + "&".join(f"{k}={v}" for k, v in ids.items()), {})
    assert dict(page["session"]) == {f"admiry_{k}": v for k, v in ids.items()}
    beacon = parse_qs(page["beacons"][0][1])
    assert {k: beacon[k] for k in ids} == {k: [v] for k, v in ids.items()}
    assert dict(page["hidden"]) == {f"_{k}": v for k, v in ids.items()}


@needs_node
def test_newsbreaks_callback_click_id_is_kept_as_the_nb_cid_kind():
    """M1 Task 6 (T6-1): NewsBreak's Conversion API wants the value of its `__CALLBACK_PARAM__` macro, which the ads
    carry as `newsbreak_cid` (`nb_cid` is NewsBreak's Pixel's own): the script keeps it as the `nb_cid` kind -- in the
    tab's session, the visit beacon and the form -- so the lead lands on NewsBreak with the click id NewsBreak takes back,
    and nothing else in M0's data model changes."""
    page = _load_page("?src=nb&newsbreak_cid=nvss_abc", {})
    assert dict(page["session"]) == {"admiry_nb_cid": "nvss_abc"}
    beacon = parse_qs(page["beacons"][0][1])
    assert beacon["nb_cid"] == ["nvss_abc"] and "newsbreak_cid" not in beacon
    assert page["hidden"] == [["_nb_cid", "nvss_abc"]]
    later = _load_page("", dict(page["session"]))  # the next page of the visit
    assert later["hidden"] == [["_nb_cid", "nvss_abc"]]


@needs_node
def test_the_callback_click_id_wins_over_the_pixels_nb_cid_and_nb_cid_is_the_fallback():
    """T6-1: an address carrying both takes `newsbreak_cid` (what the postback needs); one carrying the Pixel's `nb_cid`
    alone still keeps it, the same kind, as before."""
    both = _load_page("?nb_cid=pix_1&newsbreak_cid=nvss_1", {})
    assert dict(both["session"]) == {"admiry_nb_cid": "nvss_1"} and both["hidden"] == [["_nb_cid", "nvss_1"]]
    pixel = _load_page("?nb_cid=pix_2", {})
    assert dict(pixel["session"]) == {"admiry_nb_cid": "pix_2"} and pixel["hidden"] == [["_nb_cid", "pix_2"]]


@needs_node
def test_a_newsbreak_click_is_the_last_click_and_drops_what_the_tab_kept():
    """T6-1, last click wins as today: an address with `newsbreak_cid` drops the ids of every other kind the tab kept,
    and replaces an earlier NewsBreak click's."""
    google = _load_page("?gclid=g1", {})
    later = _load_page("?newsbreak_cid=nvss_2", dict(google["session"]))
    assert dict(later["session"]) == {"admiry_nb_cid": "nvss_2"}
    assert "gclid" not in parse_qs(later["beacons"][0][1]) and later["hidden"] == [["_nb_cid", "nvss_2"]]
    again = _load_page("?newsbreak_cid=nvss_3", dict(later["session"]))
    assert dict(again["session"]) == {"admiry_nb_cid": "nvss_3"}


@needs_node
def test_a_click_id_macro_newsbreak_left_unfilled_is_no_click_id():
    """Final review Minor 4: NewsBreak fills its `__CALLBACK_PARAM__` macro in with the click id; an address that still
    carries the macro as written carries none -- never kept as a click id, never dropping what the tab kept -- and the
    Pixel's `nb_cid` beside it is still the fallback."""
    page = _load_page("?src=nb&newsbreak_cid=__CALLBACK_PARAM__", {})
    assert dict(page["session"]) == {} and page["hidden"] == []
    assert "nb_cid" not in parse_qs(page["beacons"][0][1])
    kept = _load_page("?src=nb&newsbreak_cid=__CALLBACK_PARAM__", {"admiry_gclid": "g1"})
    assert dict(kept["session"]) == {"admiry_gclid": "g1"} and kept["hidden"] == [["_gclid", "g1"]]
    pixel = _load_page("?newsbreak_cid=__CALLBACK_PARAM__&nb_cid=pix_1", {})
    assert dict(pixel["session"]) == {"admiry_nb_cid": "pix_1"} and pixel["hidden"] == [["_nb_cid", "pix_1"]]


def test_events_for_unknown_pages_are_dropped_silently(client, conn):
    r = client.post("/t/event", data={"page": "pg_nope", "kind": "visit"})
    assert r.status_code == 204
    assert conn.execute("SELECT count(*) AS n FROM page_event").fetchone()["n"] == 0
    r = client.post("/t/event", data={"page": "pg_nope", "kind": "weird"})
    assert r.status_code == 204


def test_form_without_a_way_to_reach_back_is_refused(client, conn):
    _key, page = _page(conn)
    r = client.post(f"/t/form/{page['id']}", data={"name": "Ana"})
    assert r.status_code == 400 and r.json() == {
        "ok": False,
        "say": "Please add a phone number or an e-mail so they can reach you.",
    }
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0


def test_honeypot_and_limits_are_silent(client, conn, monkeypatch):
    """The honeypot and an over-the-limit form get the same thank-you as a lead, with `lead: false` so the
    script reports no Google conversion for them."""
    _key, page = _page(conn)
    r = client.post(f"/t/form/{page['id']}", data={"name": "Bot", "phone": "1234567890", "website2": "http://spam"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["lead"] is False
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0
    monkeypatch.setattr("agent.track.FORMS_PER_HOUR", 1)
    first = client.post(f"/t/form/{page['id']}", data={"name": "A", "phone": "1234567890"})
    second = client.post(f"/t/form/{page['id']}", data={"name": "B", "phone": "1234567891"})
    # A3p-3 P25: a stored lead's answer also names its one conversion; a dropped one has none to fire
    assert first.json()["lead"] is True and first.json()["conversion"] == "form"
    assert second.json() == {**{k: v for k, v in first.json().items() if k != "conversion"}, "lead": False}
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1


def test_form_beacons_are_stored_nowhere_and_never_use_up_the_form_limit(client, conn, monkeypatch):
    """C3 (R14): /t/event is open to anyone, so a `kind=form` beacon is answered and dropped, and the form limit
    counts stored leads: FORMS_PER_HOUR such beacons leave a real form still a lead with its e-mail, recorded as
    exactly one `form` event, by the form handler."""
    _key, page = _page(conn)
    sent = _resend(monkeypatch)
    for _ in range(track.FORMS_PER_HOUR):
        assert client.post("/t/event", data={"page": page["id"], "kind": "form"}).status_code == 204
    assert conn.execute("SELECT count(*) AS n FROM page_event").fetchone()["n"] == 0
    r = client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199"})
    assert r.status_code == 200 and r.json()["lead"] is True
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1 and len(sent) == 1
    assert [e["kind"] for e in conn.execute("SELECT kind FROM page_event").fetchall()] == ["form"]


def test_a_form_posted_without_the_script_gets_a_thank_you_page(client, conn):
    """I4: a form built from the kit, posted with scripts off, navigates to /t/form -- the answer is a small HTML
    page with the thank-you (no scripts), not raw JSON; the script's own fetch still gets JSON."""
    _key, page = _page(conn)
    url = f"/t/form/{page['id']}"
    r = client.post(url, data={"name": "Ana", "phone": "6505550199"}, headers={"Accept": NAVIGATION})
    assert r.status_code == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
    assert "Thanks — the business will call you back shortly." in r.text and "<script" not in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    bad = client.post(url, data={"name": "Ana"}, headers={"Accept": NAVIGATION})
    assert bad.status_code == 400 and track.NO_CONTACT in bad.text
    j = client.post(url, data={"name": "Bo", "phone": "6505550198"}, headers={"Accept": "application/json"})
    assert j.headers["content-type"] == "application/json" and j.json()["ok"] is True and j.json()["lead"] is True


def test_the_kit_row_takes_leads_before_set_landing_page_and_an_unpublished_row_nothing(client, conn):
    """R11 (I5): the kit's snippet carries its preview row's id and works from the start -- a form posted to it
    before set_landing_page is a lead; only an unpublished row is dead (Review Focus 5: a form 404s, a beacon is
    answered 204 and stored nowhere)."""
    business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    preview = landing.ensure_page_for_kit(conn, "biz_1")
    assert preview["status"] == "preview" and preview["url"] == ""
    r = client.post(f"/t/form/{preview['id']}", data={"name": "Ana", "phone": "6505550199"})
    assert r.status_code == 200 and r.json()["lead"] is True
    assert conn.execute("SELECT page_id FROM lead").fetchone()["page_id"] == preview["id"]
    conn.execute("UPDATE page SET status = 'unpublished' WHERE id = %s", (preview["id"],))
    gone = client.post(f"/t/form/{preview['id']}", data={"name": "Bo", "phone": "6505550198"})
    assert gone.status_code == 404 and gone.json() == {"ok": False, "say": track.NOT_CONNECTED}
    assert client.post("/t/event", data={"page": preview["id"], "kind": "visit"}).status_code == 204
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM page_event WHERE kind = 'visit'").fetchone()["n"] == 0


def test_the_public_endpoints_refuse_file_parts_and_oversized_bodies(client, conn):
    """I8: a file part is a 400 (it was a 500), a body over 64 KB a 413 before anything is parsed, more than 32
    fields a 400 (20 before A3p-3 added the UTM tags, the referrer and the intent) -- on both public endpoints,
    nothing stored; a value is kept to MAX_FIELD characters."""
    _key, page = _page(conn)
    url = f"/t/form/{page['id']}"
    r = client.post(url, files={"name": ("n.txt", b"Ana")}, data={"phone": "6505550199"})
    assert r.status_code == 400 and r.json() == {"ok": False, "say": track.UNREADABLE} and r.headers[CORS] == "*"
    assert client.post(url, data={"name": "Ana", "phone": "6505550199", "message": "x" * 100_000}).status_code == 413
    many = {f"f{i}": "x" for i in range(31)} | {"name": "Ana", "phone": "6505550199"}
    assert client.post(url, data=many).status_code == 400
    event = client.post("/t/event", files={"page": ("p.txt", page["id"].encode())}, data={"kind": "visit"})
    assert event.status_code == 400
    assert client.post("/t/event", data={"page": page["id"], "kind": "visit", "url": "x" * 100_000}).status_code == 413
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM page_event").fetchone()["n"] == 0
    client.post(url, data={"name": "Ana", "phone": "6505550199", "message": "m" * 3000})
    assert len(conn.execute("SELECT fields FROM lead").fetchone()["fields"]["message"]) == track.MAX_FIELD


def test_the_form_takes_the_older_field_names_too(client, conn):
    """I9: the recipes now name the fields the handler stores; a page built before the rename still sends
    `timing` (home services) and `matter_type` (attorneys), which land in preferred_time and service."""
    _key, page = _page(conn)
    form = {"name": "Ana", "phone": "6505550199", "timing": "this week", "matter_type": "Divorce"}
    assert client.post(f"/t/form/{page['id']}", data=form).json()["lead"] is True
    fields = conn.execute("SELECT fields FROM lead").fetchone()["fields"]
    assert fields["preferred_time"] == "this week" and fields["service"] == "Divorce"


def test_a_visitor_name_cannot_break_the_lead_mail_subject(client, conn, monkeypatch):
    """I10: the visitor's name goes into the subject; CR/LF (a header injection) and the other control
    characters become single spaces, and the name is cut at 60 characters."""
    _key, page = _page(conn)
    sent = _resend(monkeypatch)
    name = "Ana\r\nBcc: victim@example.com\r\n" + "x" * 100
    client.post(f"/t/form/{page['id']}", data={"name": name, "phone": "6505550199"})
    subject = json.loads(sent[0].read())["subject"]
    assert "\r" not in subject and "\n" not in subject
    assert subject == "New lead from your page: Ana Bcc: victim@example.com " + "x" * 30


# ---------------------------------------------------------------- A3p-3 batch 2: intents, the source, booked online


@pytest.mark.parametrize(
    ("sent", "intent", "conversion"),
    [
        ("appointment", "appointment", "appointment"),
        ("quote", "quote", "form"),
        ("bogus", "consult", "form"),
        ("", "consult", "form"),
    ],
)
def test_an_appointment_form_records_its_intent_and_one_conversion(client, conn, sent, intent, conversion):
    """Review Focus 1 (P25): the form's intent is kept on the lead (consult unless it is one of the three), and the
    answer names the one conversion the script fires -- an appointment request's own, else the form's."""
    _key, page = _page(conn)
    form = {"name": "Ana", "phone": "6505550199"} | ({"_intent": sent} if sent else {})
    answer = client.post(f"/t/form/{page['id']}", data=form).json()
    assert answer["lead"] is True and answer["conversion"] == conversion
    assert conn.execute("SELECT intent FROM lead").fetchone()["intent"] == intent


def test_the_source_is_kept_on_the_lead_and_the_event(client, conn):
    """Review Focus 3 (P28): the click's channel first, then the tagged link, then the referrer's kind, then direct;
    the tags and the referring host are kept beside it."""
    _key, page = _page(conn)
    url = f"/t/form/{page['id']}"
    base = {"name": "Ana", "phone": "6505550199"}
    client.post(url, data=base | {"_gclid": "g1", "_utm_source": "newsletter", "_ref": "www.google.com"})
    client.post(url, data=base | {"_utm_source": "newsletter", "_utm_medium": "email", "_ref": "www.google.com"})
    client.post(url, data=base | {"_ref": "www.google.com"})
    client.post(url, data=base)
    rows = conn.execute("SELECT came_from, utm, referrer FROM lead ORDER BY created_at").fetchall()
    assert [(r["came_from"], r["utm"], r["referrer"]) for r in rows] == [
        ("google_search", {"utm_source": "newsletter"}, "www.google.com"),
        ("utm:newsletter", {"utm_source": "newsletter", "utm_medium": "email"}, "www.google.com"),
        ("search", None, "www.google.com"),
        ("direct", None, None),
    ]
    client.post("/t/event", data={"page": page["id"], "kind": "call", "ref": "l.facebook.com"})
    event = conn.execute("SELECT came_from FROM page_event WHERE kind = 'call'").fetchone()
    assert event["came_from"] == "social"
    forms = conn.execute("SELECT came_from FROM page_event WHERE kind = 'form' ORDER BY id").fetchall()
    assert [r["came_from"] for r in forms] == ["google_search", "utm:newsletter", "search", "direct"]


def test_a_booked_online_beacon_records_a_booking_lead(client, conn, monkeypatch):
    """Review Focus 2 (P27): Calendly's own message makes one `booked_online` event and one `booking` lead with no
    contact (the provider has it), reported by the page, on the channel its click came from; no lead e-mail; the
    page's event limit applies."""
    _key, page = _page(conn)
    sent = _resend(monkeypatch)
    beacon = {"page": page["id"], "kind": "booked_online", "gclid": "g1", "utm_source": "google"}
    assert client.post("/t/event", data=beacon).status_code == 204
    event = conn.execute("SELECT kind, click_id, came_from FROM page_event").fetchone()
    assert (event["kind"], event["click_id"], event["came_from"]) == ("booked_online", "g1", "google_search")
    lead = conn.execute("SELECT kind, fields, channel, came_from, intent, notified_at FROM lead").fetchone()
    assert (lead["kind"], lead["fields"], lead["channel"], lead["came_from"]) == (
        "booking",
        {"reported_by": "page", "provider": "calendly"},
        "google_search",
        "google_search",
    )
    assert lead["intent"] is None and lead["notified_at"] is None and sent == []
    monkeypatch.setattr("agent.track.EVENTS_PER_HOUR", 1)
    client.post("/t/event", data=beacon)
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1  # over the limit: dropped, silently


def test_a_booking_lead_reads_as_one_in_the_leads_list(client, conn):
    from agent.tests.conftest import call_tool, data

    key, page = _page(conn)
    client.post("/t/event", data={"page": page["id"], "kind": "booked_online", "ref": "www.google.com"})
    client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199", "_intent": "appointment"})
    found = data(call_tool(client, "list_leads", {}, key=key))["leads"]
    assert sorted((lead["kind"], lead["intent"], lead["came_from"]) for lead in found) == [
        ("booking", None, "search"),
        ("form", "appointment", "direct"),
    ]
    only = data(call_tool(client, "list_leads", {"kind": "booking"}, key=key))["leads"]
    assert [lead["kind"] for lead in only] == ["booking"]


@needs_node
def test_the_script_keeps_the_click_90_days():
    """Review Focus 4 (P29): a click kept 89 days ago still travels with a later visit's beacon and form; one kept 91
    days ago is forgotten, and a direct visit then keeps nothing."""
    kept = {"ids": {"gclid": "g1"}, "utm": {"utm_source": "google"}, "ref": "www.google.com"}
    fresh = _run(storage={"admiry_visit": json.dumps({**kept, "t": NOW - 89 * DAY})})
    beacon = parse_qs(fresh["beacons"][0][1])
    assert (beacon["gclid"], beacon["utm_source"], beacon["ref"]) == (["g1"], ["google"], ["www.google.com"])
    assert dict(fresh["hidden"]) == {"_gclid": "g1", "_utm_source": "google", "_ref": "www.google.com"}
    stale = _run(storage={"admiry_visit": json.dumps({**kept, "t": NOW - 91 * DAY})})
    assert set(parse_qs(stale["beacons"][0][1])) == {"page", "kind", "url"} and stale["hidden"] == []


@needs_node
def test_a_tagged_link_or_a_referring_site_is_kept_until_a_click_and_never_over_one():
    """P28/P29: the link's UTM tags and the referring host travel with the visit and are kept; a later tagged link
    does not replace a kept ad click (the last click wins, a click over anything else); a later click replaces it all;
    the site's own pages never count as the referrer."""
    tagged = _run(search="?utm_source=newsletter&utm_medium=email", referrer="https://mail.example/inbox")
    assert _visit(tagged["storage"]) == {
        "ids": {},
        "utm": {"utm_source": "newsletter", "utm_medium": "email"},
        "ref": "mail.example",
        "t": NOW,
    }
    clicked = _run(search="?gclid=g1", storage=dict(tagged["storage"]))
    assert _visit(clicked["storage"])["ids"] == {"gclid": "g1"} and _visit(clicked["storage"])["utm"] == {}
    later = _run(search="?utm_source=fb", referrer="https://l.facebook.com/", storage=dict(clicked["storage"]))
    assert _visit(later["storage"]) == _visit(clicked["storage"])  # the click is kept
    inside = _run(referrer="https://shop.example/other-page")
    assert inside["storage"] == [] and "ref" not in parse_qs(inside["beacons"][0][1])


@needs_node
def test_the_form_sends_its_intent():
    """P25: the form's data-admiry-intent travels as _intent; a form without one sends none (the server reads
    consult)."""
    assert ["_intent", "appointment"] in _run(intent="appointment")["hidden"]
    assert all(name != "_intent" for name, _value in _run()["hidden"])


@needs_node
def test_a_calendly_booking_is_counted_once_a_page_view_and_only_from_calendly():
    """P27: Calendly's own message from https://calendly.com sends one booked_online beacon however often it comes;
    the same message from anywhere else sends nothing."""
    out = _run(calendly=True)
    kinds = [parse_qs(body)["kind"][0] for _url, body in out["beacons"]]
    assert kinds == ["visit", "booked_online"]


@needs_node
def test_the_call_from_the_site_asks_google_for_its_forwarding_number():
    """P26: with the account's call_site label and the page's phone, the tag is set to swap the number before its
    config; without the label, or without the phone, nothing is set."""
    dataset = {
        "gads": "AW-555",
        "labels": json.dumps({"call_site": "CallLbl", "form": "FormLbl"}),
        "phone": "650-555-0100",
    }
    calls = _run(dataset=dataset)["gtag"]
    names = [c[0] for c in calls]
    assert names[:3] == ["js", "set", "config"]
    assert calls[1][1] == {"phone_conversion_number": "650-555-0100", "phone_conversion_ids": ["AW-555/CallLbl"]}
    assert calls[2] == ["config", "AW-555", {"cookie_domain": "shop.example"}]
    plain = _run(dataset={**dataset, "labels": json.dumps({"form": "FormLbl"})})["gtag"]
    assert [c[0] for c in plain][:2] == ["js", "config"]
