"""Owner-side tokens at rest (agent/secretbox.py, channel_account.secret_enc): sealed under the box's
AGENT_KEY_ENCRYPTION_KEY, readable with that key only."""

import pytest

from agent import secretbox
from agent.tests.conftest import TEST_ENCRYPTION_KEY

OTHER_KEY = "fedcba9876543210" * 4
# Sealed once, under the derivation the stored tokens will live with (the key's bytes -> SHA-256 -> Fernet key)
SEALED_EARLIER = (
    b"gAAAAABqvJ9-IFpL8YqcbX6NWRweRUfCmDQhF5SCY_IxJiZkCOVGzd2Y-Q-FTkj6gM0SR_T"
    b"BhRbIkGwwDmMwqmITVRk0sZ8R1y3HdBBoT5pUbPwKUnKIi9c="
)


def test_a_token_round_trips_and_the_sealed_bytes_do_not_hold_it():
    sealed = secretbox.encrypt("tok_SECRET-ünï", TEST_ENCRYPTION_KEY)
    assert isinstance(sealed, bytes)
    assert b"tok_SECRET" not in sealed
    assert secretbox.decrypt(sealed, TEST_ENCRYPTION_KEY) == "tok_SECRET-ünï"


def test_a_token_sealed_earlier_still_opens():
    """Changing how the key is derived would strand every token already stored, and nothing else would notice."""
    assert secretbox.decrypt(SEALED_EARLIER, TEST_ENCRYPTION_KEY) == "tok_SECRET-known"


def test_another_key_or_damaged_bytes_cannot_open_a_token():
    sealed = secretbox.encrypt("tok_SECRET", TEST_ENCRYPTION_KEY)
    for key, blob in (
        (OTHER_KEY, sealed),  # a rotated key strands what the old one sealed
        (TEST_ENCRYPTION_KEY, sealed[:-4] + b"AAAA"),
        (TEST_ENCRYPTION_KEY, b"not a sealed token"),
        (TEST_ENCRYPTION_KEY, b""),
    ):
        with pytest.raises(secretbox.Undecryptable):
            secretbox.decrypt(blob, key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "hunter2",  # not hex
        "zz" * 32,
        "ab" * 31,  # 31 bytes: too short to be `openssl rand -hex 32`
        "a" * 65,  # odd length
    ],
)
def test_a_key_that_is_not_at_least_32_hex_bytes_is_refused(key):
    with pytest.raises(ValueError, match="hex"):
        secretbox.encrypt("tok", key)
    with pytest.raises(ValueError, match="hex"):
        secretbox.decrypt(b"anything", key)
