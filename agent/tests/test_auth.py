import pytest
from fastmcp.exceptions import ToolError

from agent import audit, auth, keys


def _headers(monkeypatch, headers: dict):
    monkeypatch.setattr(auth, "get_http_headers", lambda include=None, include_all=False: headers)


def _business_key(conn, scopes=("read",), cls="business"):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1') ON CONFLICT DO NOTHING")
    return keys.mint(conn, business_id="biz_1", cls=cls, scopes=scopes, label="muse")


def test_bearer_and_x_api_key_and_ip_parsing():
    assert auth.bearer({"authorization": "Bearer ak_abc"}) == "ak_abc"
    assert auth.bearer({"authorization": "bearer ak_abc "}) == "ak_abc"
    assert auth.bearer({"x-api-key": "ak_xyz"}) == "ak_xyz"
    assert auth.bearer({}) is None
    assert auth.client_ip({"x-forwarded-for": "203.0.113.9, 10.0.0.1"}) == "203.0.113.9"
    assert auth.client_ip({}) == "local"


def test_require_missing_key_names_the_connect_page(conn, monkeypatch):
    _headers(monkeypatch, {})
    with pytest.raises(ToolError, match="missing key.*http://testserver/connect"):
        auth.require(conn, "read", web_base_url="http://testserver")


def test_require_unknown_revoked_expired_and_scope(conn, monkeypatch):
    plaintext, rec = _business_key(conn, scopes=("read",))
    _headers(monkeypatch, {"authorization": "Bearer ak_" + "0" * 40})
    with pytest.raises(ToolError, match="unknown key"):
        auth.require(conn, "read")
    _headers(monkeypatch, {"authorization": f"Bearer {plaintext}"})
    with pytest.raises(ToolError, match="lacks the 'manage' scope"):
        auth.require(conn, "manage")
    p = auth.require(conn, "read")
    assert p.business_id == "biz_1" and p.actor == f"key:{rec.id}" and p.ip == "local"
    assert conn.execute("SELECT last_used_at FROM api_key WHERE id = %s", (rec.id,)).fetchone()["last_used_at"]
    conn.execute("UPDATE api_key SET expires_at = now() - interval '1 minute' WHERE id = %s", (rec.id,))
    with pytest.raises(ToolError, match="expired"):
        auth.require(conn, "read")
    conn.execute("UPDATE api_key SET expires_at = NULL, revoked_at = now() WHERE id = %s", (rec.id,))
    with pytest.raises(ToolError, match="revoked"):
        auth.require(conn, "read")


def test_denials_carry_the_refused_key_or_none(conn, monkeypatch):
    plaintext, rec = _business_key(conn, scopes=("read",))
    for headers in ({}, {"authorization": "Bearer ak_" + "0" * 40}):  # missing, unknown
        _headers(monkeypatch, headers)
        with pytest.raises(auth.Denied) as nobody:
            auth.require(conn, "read")
        assert nobody.value.key is None  # nothing to attribute the call to
    _headers(monkeypatch, {"authorization": f"Bearer {plaintext}"})
    with pytest.raises(auth.Denied) as scope:
        auth.require(conn, "manage")
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s", (rec.id,))
    with pytest.raises(auth.Denied) as revoked:
        auth.require(conn, "read")
    assert scope.value.key.id == revoked.value.key.id == rec.id


def test_the_places_budget_counts_every_call_to_google_of_the_day_and_nothing_else(conn):
    """Walk fixes 1, Task 4 round 1, final review A2: AGENT_PLACES_DAILY_BUDGET reads every call to Google of the day,
    one `places.lookup` row each -- start's lookup of the business (for no business on the homepage's first turn) and
    each address put on the map, for every business -- and nothing else: a `start` row, refused or not, is no call. A
    business's own cap reads only its lookups. Other tools and yesterday's calls are not counted."""
    for tool, business_id, ago in (
        ("start", None, "0 seconds"),
        ("start", "biz_1", "0 seconds"),
        ("places.lookup", None, "0 seconds"),
        ("places.lookup", "biz_1", "0 seconds"),
        ("places.lookup", "biz_2", "0 seconds"),
        ("answer", "biz_1", "0 seconds"),
        ("places.lookup", "biz_1", "1 day"),
    ):
        conn.execute(
            "INSERT INTO agent_action (business_id, actor, tool, args_hash, ts) "
            "VALUES (%s, 'x', %s, 'h', now() - %s::interval)",
            (business_id, tool, ago),
        )
    assert auth.places_today(conn) == 3
    assert (auth.places_lookups_today(conn, "biz_1"), auth.places_lookups_today(conn, "biz_2")) == (1, 1)
    assert auth.starts_today(conn) == 2  # the anonymous start's own ceiling still counts starts alone


def test_rate_limit_per_minute_reports_retry_after(conn, monkeypatch):
    plaintext, rec = _business_key(conn)
    _headers(monkeypatch, {"authorization": f"Bearer {plaintext}"})
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, api_key_id, tool, args_hash, ts) "
        "SELECT 'biz_1', %s, %s, 'whoami', 'h', now() - (g * interval '1 millisecond') FROM generate_series(1, 60) g",
        (f"key:{rec.id}", rec.id),
    )
    with pytest.raises(auth.RateLimited, match=r"60 calls per minute; retry_after=\d+"):
        auth.require(conn, "read")


def test_rate_limit_per_day(conn, monkeypatch):
    plaintext, rec = _business_key(conn)
    _headers(monkeypatch, {"authorization": f"Bearer {plaintext}"})
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, api_key_id, tool, args_hash, ts) "
        "SELECT 'biz_1', %s, %s, 'whoami', 'h', now() - interval '2 hours' FROM generate_series(1, 2000) g",
        (f"key:{rec.id}", rec.id),
    )
    with pytest.raises(auth.RateLimited, match=r"2000 calls per day; retry_after=\d+"):
        auth.require(conn, "read")


def test_rate_limit_per_day_reports_retry_after_from_the_oldest_call_in_the_window(conn, monkeypatch):
    plaintext, rec = _business_key(conn)
    _headers(monkeypatch, {"authorization": f"Bearer {plaintext}"})
    monkeypatch.setattr(auth, "RATE_PER_DAY", 5)
    monkeypatch.setattr(auth, "RATE_PER_MINUTE", 1000)  # don't let the per-minute check trip first
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, api_key_id, tool, args_hash, ts) "
        "SELECT 'biz_1', %s, %s, 'whoami', 'h', now() - interval '2 hours' FROM generate_series(1, 5) g",
        (f"key:{rec.id}", rec.id),
    )
    with pytest.raises(auth.RateLimited, match=r"5 calls per day; retry_after=(\d+)") as exc:
        auth.require(conn, "read")
    retry_after = int(str(exc.value).rsplit("retry_after=", 1)[1])
    # the oldest call is ~2h old, so retry_after is ~22h -- nowhere near the old hard-coded 3600
    assert 3600 < retry_after <= 86400


def test_a_site_build_is_one_of_the_days_model_calls_only_when_it_wrote_words(conn):
    """A3p-6 batch 2 Task 4 fix round 1: build_site is one of the model tools (one build of a business at a time), but
    its rows count toward the day only when the model wrote the site's words -- the call's audit carries `model` then;
    a question, a look asked or a refusal spends nothing (`answer`'s `free_text` shape)."""
    _plaintext, rec = _business_key(conn, scopes=("read", "manage"), cls="draft")
    principal = auth.Principal(key=rec, ip="local")

    def row(args: dict) -> None:
        audit.record(
            conn,
            tool="build_site",
            args=args,
            result={},
            ok=True,
            business_id="biz_1",
            actor=principal.actor,
            actor_class="draft",
            api_key_id=rec.id,
            ip="local",
        )

    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT):
        row({"style": "solid", "accent": None, "facts": None, "wordmark": None})
    auth.check_llm_cap(conn, principal)  # ten builds, none wrote words: nothing spent
    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT - 1):
        row({"style": None, "accent": None, "facts": ["warranty"], "wordmark": None, "model": True})
    auth.check_llm_cap(conn, principal)  # nine words written: one left
    row({"style": None, "accent": None, "facts": ["years"], "wordmark": None, "model": True})
    with pytest.raises(ToolError, match="today's limit of 10 drafts for this business is reached"):
        auth.check_llm_cap(conn, principal)


def test_the_pages_own_command_that_started_a_site_counts_only_when_it_wrote_words(conn):
    """A3p-6 batch 2 Task 5: for a home-services business or a dental practice `create_page` is the site's first step
    (Q14) and its row says `site`; like `build_site`'s it counts toward the day only when the model wrote the words
    (`model`). A starter page's row, which says neither, counts as it always did."""
    _plaintext, rec = _business_key(conn, scopes=("read", "manage"), cls="draft")
    principal = auth.Principal(key=rec, ip="local")

    def row(args: dict) -> None:
        audit.record(
            conn,
            tool="create_page",
            args=args,
            result={},
            ok=True,
            business_id="biz_1",
            actor=principal.actor,
            actor_class="draft",
            api_key_id=rec.id,
            ip="local",
        )

    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT):
        row({"template": None, "brief": None, "site": True})
    auth.check_llm_cap(conn, principal)  # ten times the look asked: nothing spent
    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT - 1):
        row({"template": None, "brief": None, "site": True, "model": True})
    auth.check_llm_cap(conn, principal)  # nine times the words written: one left
    row({"template": None, "brief": None})  # a starter page is a draft, whatever it says
    with pytest.raises(ToolError, match="today's limit of 10 drafts for this business is reached"):
        auth.check_llm_cap(conn, principal)


def test_a_quick_change_refused_on_a_site_is_no_draft_of_the_day(conn):
    """A3p-6 batch 2 Task 6 (Task 5's review): revise_page and update_page refuse on a site (NO_CHANGES_YET) before the
    model is asked, and their rows say `site`. revise_page is one of the model tools, but such a row spent nothing, so
    it never counts toward the day; a starter page's revise_page, whose row says no such thing, counts as it always
    did."""
    _plaintext, rec = _business_key(conn, scopes=("read", "manage"), cls="draft")
    principal = auth.Principal(key=rec, ip="local")

    def row(tool: str, args: dict) -> None:
        audit.record(
            conn,
            tool=tool,
            args=args,
            result={"error": "refused"},
            ok=False,
            business_id="biz_1",
            actor=principal.actor,
            actor_class="draft",
            api_key_id=rec.id,
            ip="local",
        )

    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT):
        row("revise_page", {"instruction": "make it warmer", "site": True})
        row("update_page", {"section": "headline", "text": "Roofing in Charlotte", "site": True})
    auth.check_llm_cap(conn, principal)  # ten refusals of each: nothing spent
    for _n in range(auth.LLM_CALLS_PER_DAY_DRAFT - 1):
        row("revise_page", {"instruction": "make it warmer"})
    auth.check_llm_cap(conn, principal)  # nine starter-page revisions: one left
    row("revise_page", {"instruction": "shorter paragraphs"})
    with pytest.raises(ToolError, match="today's limit of 10 drafts for this business is reached"):
        auth.check_llm_cap(conn, principal)
