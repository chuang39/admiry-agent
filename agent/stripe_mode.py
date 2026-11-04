"""Which Stripe keys a business pays with (Task 8b, the founder's ruling of 2026-11-04; rulings R17, R18).

The "sandbox users" are exactly the owners on AGENT_LAUNCH_ALLOWLIST (`settings.launch_allowlist`, lower-cased
e-mails; no address is ever written here): they pay with Stripe's sandbox (test cards) through
STRIPE_SANDBOX_SECRET_KEY / STRIPE_SANDBOX_WEBHOOK_SECRET, when both are set. Every other owner -- and an allowlisted
one while either sandbox key is unset -- uses the regular STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET, exactly as before
(unset on prod today, so their payment step keeps saying payments are not set up). The payment step is offered when
the owner's keys are `ready` (both present: without the webhook secret a payment could be taken but never credited).
/hooks/stripe (pay.py) verifies with the regular secret, else the sandbox one, and applies an event only the sandbox
secret verified only when its business's owner is a sandbox user (`sandbox_user`, R18)."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from . import config


@dataclass(frozen=True)
class StripeKeys:
    secret_key: str | None = field(repr=False)  # never printed
    webhook_secret: str | None = field(repr=False)
    sandbox: bool

    @property
    def ready(self) -> bool:
        """Both keys: the payment step is offered (config.Settings.payments_ready, per owner)."""
        return bool(self.secret_key and self.webhook_secret)


def sandbox_user(settings: config.Settings, owner_email: str | None) -> bool:
    """Whether the owner is on AGENT_LAUNCH_ALLOWLIST (R17), compared lower-cased and trimmed as the list is."""
    return bool(owner_email) and owner_email.strip().lower() in settings.launch_allowlist


def for_owner(settings: config.Settings, owner_email: str | None) -> StripeKeys:
    """The sandbox keys when the owner is a sandbox user and both sandbox keys are set; the regular keys otherwise."""
    if (
        sandbox_user(settings, owner_email)
        and settings.stripe_sandbox_secret_key
        and settings.stripe_sandbox_webhook_secret
    ):
        return StripeKeys(settings.stripe_sandbox_secret_key, settings.stripe_sandbox_webhook_secret, sandbox=True)
    return StripeKeys(settings.stripe_secret_key, settings.stripe_webhook_secret, sandbox=False)


def for_business(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> StripeKeys:
    """`for_owner` of the business's owner (business.owner_email); no such business, or no owner yet: the regular
    keys."""
    row = conn.execute("SELECT owner_email FROM business WHERE id = %s", (business_id,)).fetchone()
    return for_owner(settings, row["owner_email"] if row else None)
