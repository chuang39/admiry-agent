"""§6.1's decision table and the webhook signature (spec 2026-09-28 §5.1, §6.1). DKIM fixtures are signed here with
dkimpy and a throwaway key; DNS is a function that serves that key for one selector."""

import base64
import hashlib
import hmac
import time

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent import mail_auth
from agent.tests.conftest import RESEND_WHSEC

KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
)
PUB = base64.b64encode(
    KEY.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
)


def dns(name, timeout=5):
    return b"v=DKIM1; k=rsa; p=" + PUB if name == b"s1._domainkey.example.com." else None


def message(from_addr="Owner <owner@example.com>") -> bytes:
    return (
        f"From: {from_addr}\r\nTo: hello@agent.test\r\nSubject: hi\r\nMessage-ID: <1@example.com>\r\n\r\n"
        "We need more customers.\r\n"
    ).encode()


def signed(msg: bytes, domain=b"example.com") -> bytes:
    sig = dkim.sign(msg, b"s1", domain, PEM, include_headers=[b"from", b"to", b"subject", b"message-id"])
    return sig + msg


def received(from_addr="Owner <owner@example.com>", dmarc="gray") -> dict:
    return {"from": from_addr, "authentication": {"spf": "gray", "dkim": "gray", "dmarc": dmarc}}


def test_dmarc_verdict_decides_when_definite():
    assert mail_auth.decide(received(dmarc="pass"), lambda: None) == mail_auth.Decision(True, "dmarc_pass")
    assert mail_auth.decide(received(dmarc="fail"), lambda: signed(message())) == mail_auth.Decision(
        False, "dmarc_fail"
    )
    assert mail_auth.decide({"from": "", "authentication": {"dmarc": "pass"}}, lambda: None).reason == "no_from"


def test_dkim_decides_the_rest():
    assert mail_auth.decide(received(), lambda: signed(message()), dnsfunc=dns) == mail_auth.Decision(
        True, "dkim_aligned"
    )
    assert mail_auth.decide(received(), lambda: message(), dnsfunc=dns).reason == "dkim_none"
    tampered = signed(message()).replace(b"more customers", b"fewer customers")
    assert mail_auth.decide(received(), lambda: tampered, dnsfunc=dns).reason == "dkim_invalid"
    forged = signed(message("Owner <owner@other.example>"))  # a valid signature by example.com on someone else's From
    assert (
        mail_auth.decide(received("Owner <owner@other.example>"), lambda: forged, dnsfunc=dns).reason
        == "dkim_unaligned"
    )
    assert mail_auth.decide(received(), lambda: None, dnsfunc=dns).reason == "raw_unavailable"


def test_a_malformed_header_line_is_dkim_invalid_not_an_exception():
    """dkim.DKIM() itself raises dkim.MessageFormatError on a header line with no colon that also doesn't start
    the mbox "From " envelope line dkimpy special-cases; before the fix this propagated out of dkim_status
    uncaught (finding M5)."""
    raw = b"From: owner@example.com\r\nThis is not a header line\r\nSubject: hi\r\n\r\nhi\r\n"
    assert mail_auth.dkim_status(raw, "example.com") == "dkim_invalid"


def test_alignment_is_relaxed_both_ways():
    assert mail_auth.aligned("example.com", "example.com")
    assert mail_auth.aligned("example.com", "mail.example.com") and mail_auth.aligned("mail.example.com", "example.com")
    assert not mail_auth.aligned("example.com", "example.com.evil.test") and not mail_auth.aligned("", "example.com")
    assert mail_auth.sender({"from": "Acme Owner <Owner@Example.COM>"}) == "owner@example.com"


def svix_headers(payload: bytes, *, secret=RESEND_WHSEC, ts=None, msg_id="msg_1") -> dict:
    ts = str(int(time.time()) if ts is None else ts)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


def test_svix_signature():
    body = b'{"type":"email.received"}'
    mail_auth.verify_svix(body, svix_headers(body), RESEND_WHSEC)
    h = svix_headers(body)
    h["svix-signature"] = "v1,bm9wZQ== " + h["svix-signature"]  # a second, matching entry still passes
    mail_auth.verify_svix(body, h, RESEND_WHSEC)
    with pytest.raises(mail_auth.BadSignature):
        mail_auth.verify_svix(body + b" ", svix_headers(body), RESEND_WHSEC)
    with pytest.raises(mail_auth.BadSignature):
        mail_auth.verify_svix(body, svix_headers(body, ts=int(time.time()) - 400), RESEND_WHSEC)
    with pytest.raises(mail_auth.BadSignature):
        mail_auth.verify_svix(
            body, svix_headers(body, secret="whsec_" + base64.b64encode(b"z" * 32).decode()), RESEND_WHSEC
        )
    with pytest.raises(mail_auth.BadSignature):
        mail_auth.verify_svix(body, {}, RESEND_WHSEC)
    with pytest.raises(mail_auth.BadSignature):
        mail_auth.verify_svix(body, svix_headers(body), "")
    with pytest.raises(mail_auth.BadSignature):
        h = svix_headers(body)
        h["svix-signature"] = "v1," + chr(233)  # non-ASCII value
        mail_auth.verify_svix(body, h, RESEND_WHSEC)
    h = svix_headers(body)
    h["svix-signature"] = "v1," + chr(233) + " " + h["svix-signature"]  # non-ASCII first, valid second
    mail_auth.verify_svix(body, h, RESEND_WHSEC)
