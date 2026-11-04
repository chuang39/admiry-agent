"""Click-wrap acceptance of the legal texts owners accept in the home conversation (setup.accept; spec 2026-09-25
D28, §5 "Agreement"): the Google Ads Management Agreement (every business) and the Prepayment Addendum (prepaid businesses, A1b-3). Rows are
append-only evidence (business, document, e-mail, version, address, time); "accepted" means a row for the business
and document with the version of the text that ships (legal.current(document)). Linking a Google Ads account — in
the conversation, through connect_google_ads or by an operator — requires the agreement; adding funds requires both
(terms_accepted); a new version asks again."""

from __future__ import annotations

import psycopg

from . import legal

CURRENT_VERSION = legal.current()[0]  # the agreement's; read at call time below, so a test can swap it
ADDENDUM_VERSION = legal.current(legal.ADDENDUM)[0]  # the Prepayment Addendum's, likewise


def version(document: str = legal.AGREEMENT) -> str:
    """The shipping version of `document` (KeyError for an unknown one)."""
    return {legal.AGREEMENT: CURRENT_VERSION, legal.ADDENDUM: ADDENDUM_VERSION}[document]


def accepted(conn: psycopg.Connection, business_id: str, document: str = legal.AGREEMENT) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM agreement_acceptance WHERE business_id = %s AND document = %s AND version = %s LIMIT 1",
            (business_id, document, version(document)),
        ).fetchone()
        is not None
    )


def terms_accepted(conn: psycopg.Connection, business_id: str) -> bool:
    """Both texts a prepaid business needs before it adds funds (D28)."""
    return accepted(conn, business_id) and accepted(conn, business_id, legal.ADDENDUM)


def accept(
    conn: psycopg.Connection, *, business_id: str, email: str, ip: str | None, document: str = legal.AGREEMENT
) -> str:
    """Record the owner's acceptance of the current version of `document`, once per business, document and version;
    returns the version."""
    current = version(document)
    if not accepted(conn, business_id, document):
        conn.execute(
            "INSERT INTO agreement_acceptance (business_id, document, email, version, ip) VALUES (%s, %s, %s, %s, %s)",
            (business_id, document, email.strip().lower(), current, ip),
        )
    return current
