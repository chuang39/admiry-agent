"""The channel framework's types (agent/channels/base.py, spec 2026-09-29 §5.2): what an adapter declares it can do,
the account it runs against, and the protocol every adapter implements."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.channels import base
from agent.tests.fixtures import FakeAdapter

REPO = Path(__file__).parents[2]

DECLARED = {
    "create": True,
    "update": True,
    "pause": True,
    "metrics": True,
    "conversions": False,
    "leads": False,
    "modes": {"own_account"},
    "min_monthly_usd": 100,
    "geo": "radius",
    "lead_formats": {"call", "form"},
}


def test_the_optional_declarations_have_the_specs_defaults():
    caps = base.Capabilities(**DECLARED)
    assert caps.needs_image is False
    assert caps.validate == "dry"
    assert caps.page_rules == ()


def test_page_rules_and_validate_read_back_as_declared():
    """`validate` is also the name of pydantic's old BaseModel.validate method; on a Capabilities it must be the
    declared value, or a launch would take the method for "server"."""
    caps = base.Capabilities(**DECLARED, validate="server", page_rules=["terms", "contact"], needs_image=True)
    assert caps.validate == "server"
    assert caps.page_rules == ("terms", "contact")
    assert caps.needs_image is True


@pytest.mark.parametrize(
    "wrong",
    [
        {"geo": "galaxy"},
        {"modes": {"rented"}},
        {"lead_formats": {"fax"}},
        {"validate": "maybe"},
        {"min_monthly_usd": "lots"},
        {"needs_imag": True},  # a misspelt declaration is refused, not dropped
    ],
)
def test_capabilities_refuse_what_the_spec_does_not_name(wrong):
    with pytest.raises(ValidationError):
        base.Capabilities(**{**DECLARED, **wrong})


def test_an_account_prints_without_its_token():
    account = base.Account(
        business_id="biz_1",
        channel="chatgpt_ads",
        mode="own_account",
        status="active",
        external={"ad_account_id": "acct_9"},
        secret="tok_SECRET",
    )
    assert account.secret == "tok_SECRET"
    assert "tok_SECRET" not in repr(account)
    assert "tok_SECRET" not in str(account)
    assert "acct_9" in repr(account)


def test_an_adapter_is_whatever_implements_the_whole_protocol():
    class HalfAnAdapter:
        channel = "half"

        def capabilities(self):
            return None

    assert isinstance(FakeAdapter(), base.Adapter)
    assert not isinstance(HalfAnAdapter(), base.Adapter)


def test_importing_the_types_prints_no_pydantic_warning():
    """The field named `validate` shadows a pydantic method, which pydantic reports as a UserWarning at import; base.py
    silences that one warning on purpose, so it is not printed in every process's log at boot."""
    subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", "import agent.channels.base"], cwd=REPO, check=True
    )
