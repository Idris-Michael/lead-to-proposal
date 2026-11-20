from __future__ import annotations

import pytest
from pydantic import ValidationError

from lead_to_proposal.models import LeadIn
from lead_to_proposal.signing import SignatureError, sign, verify

from .conftest import lead_payload


def test_email_is_lowercased_so_dedupe_ignores_case() -> None:
    a = LeadIn(**lead_payload())
    b = LeadIn(**lead_payload(email="sam.carter@EXAMPLE.co.uk"))
    assert a.email == "sam.carter@example.co.uk"
    assert a.dedupe_key() == b.dedupe_key()


def test_dedupe_key_ignores_whitespace_but_not_content() -> None:
    a = LeadIn(**lead_payload(message="Please   automate our\norders into Xero"))
    b = LeadIn(**lead_payload(message="please automate our orders into xero"))
    c = LeadIn(**lead_payload(message="please automate our invoices into xero"))
    assert a.dedupe_key() == b.dedupe_key()
    assert a.dedupe_key() != c.dedupe_key()


def test_unknown_fields_are_rejected_so_form_changes_fail_loudly() -> None:
    with pytest.raises(ValidationError):
        LeadIn(**lead_payload(phone="0123"))


def test_signature_round_trip() -> None:
    body = b'{"a":1}'
    verify("s3cret", body, "1000", sign("s3cret", body, 1000), now=1000)


@pytest.mark.parametrize(
    ("secret", "ts", "sig_body", "now", "reason"),
    [
        ("s3cret", "1000", b'{"a":2}', 1000, "mismatch"),  # body edited in transit
        ("s3cret", "1000", b'{"a":1}', 1301, "window"),  # replayed after five minutes
        ("", "1000", b'{"a":1}', 1000, "not configured"),  # fail closed without a secret
        ("s3cret", "abc", b'{"a":1}', 1000, "bad timestamp"),
    ],
)
def test_signature_rejections(secret: str, ts: str, sig_body: bytes, now: float, reason: str) -> None:
    sig = sign("s3cret", sig_body, 1000)
    with pytest.raises(SignatureError, match=reason):
        verify(secret, b'{"a":1}', ts, sig, now=now)


def test_missing_headers_rejected() -> None:
    with pytest.raises(SignatureError, match="missing"):
        verify("s3cret", b"{}", None, None)
