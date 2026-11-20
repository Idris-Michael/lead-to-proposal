from __future__ import annotations

import httpx
import pytest

from lead_to_proposal.hubspot import HubSpotClient, HubSpotError
from lead_to_proposal.hubspot_fake import FakeHubSpot
from lead_to_proposal.models import LeadIn, Qualification

from .conftest import lead_payload


def client_for(fake: FakeHubSpot, **kw: object) -> tuple[HubSpotClient, list[float]]:
    slept: list[float] = []
    c = HubSpotClient("tok", transport=fake.transport(), sleep=slept.append, rng=lambda: 1.0, **kw)  # type: ignore[arg-type]
    return c, slept


def test_upsert_creates_then_updates_same_contact(lead: LeadIn, qualified: Qualification) -> None:
    fake = FakeHubSpot()
    hs, _ = client_for(fake)
    first = hs.upsert_contact(lead, qualified)
    again = hs.upsert_contact(LeadIn(**lead_payload(email="SAM.CARTER@example.co.uk")), qualified)
    assert first == again
    assert len(fake.contacts) == 1
    assert fake.contacts[first]["properties"]["ltp_score"] == "82"


def test_upsert_recovers_when_another_worker_creates_first(lead: LeadIn, qualified: Qualification) -> None:
    """PATCH 404s, then a racing worker creates the contact, so our POST gets 409: we must end up
    updating the existing record, not failing and not creating a second one."""
    fake = FakeHubSpot()
    hs, _ = client_for(fake)
    original = fake._patch_contact
    calls = {"n": 0}

    def racing_patch(ident: str, id_property: str | None, body: dict[str, object]) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            fake._create(fake.contacts, {"properties": {"email": lead.email}})
            return httpx.Response(404, json={})
        return original(ident, id_property, body)  # type: ignore[arg-type]

    fake._patch_contact = racing_patch  # type: ignore[method-assign]
    hs.upsert_contact(lead, qualified)
    assert len(fake.contacts) == 1


def test_deal_is_not_duplicated_for_the_same_lead_key(lead: LeadIn, qualified: Qualification) -> None:
    fake = FakeHubSpot()
    hs, _ = client_for(fake)
    cid = hs.upsert_contact(lead, qualified)
    d1 = hs.create_deal(lead, qualified, cid, "key-1")
    d2 = hs.create_deal(lead, qualified, cid, "key-1")
    assert d1 == d2
    assert len(fake.deals) == 1
    deal = fake.deals[d1]
    assert deal["properties"]["amount"] == "8000"
    assert deal["associations"][0]["to"]["id"] == cid


def test_note_links_contact_and_deal(lead: LeadIn, qualified: Qualification) -> None:
    fake = FakeHubSpot()
    hs, _ = client_for(fake)
    nid = hs.add_note("hello", "c1", "d1")
    ids = [a["to"]["id"] for a in fake.notes[nid]["associations"]]
    assert ids == ["c1", "d1"]


def test_429s_are_retried_with_backoff_and_counted(lead: LeadIn, qualified: Qualification) -> None:
    fake = FakeHubSpot(rate_limit_ratio=0.5, seed=3)
    hs, slept = client_for(fake, max_attempts=20)
    cid = hs.upsert_contact(lead, qualified)
    hs.create_deal(lead, qualified, cid, "k")
    assert hs.stats.rate_limited == fake.injected > 0
    assert len(slept) == fake.injected
    assert len(fake.contacts) == 1 and len(fake.deals) == 1


def test_retry_after_header_is_honoured() -> None:
    responses = iter(
        [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json={"id": "9"})]
    )
    t = httpx.MockTransport(lambda _req: next(responses))
    slept: list[float] = []
    hs = HubSpotClient("tok", transport=t, sleep=slept.append)
    hs._request("GET", "/x")
    assert slept == [3.0]


def test_backoff_doubles_and_caps() -> None:
    t = httpx.MockTransport(lambda _req: httpx.Response(503))
    slept: list[float] = []
    hs = HubSpotClient("tok", transport=t, sleep=slept.append, rng=lambda: 1.0, max_attempts=6, max_delay_s=3)
    with pytest.raises(HubSpotError, match="gave up"):
        hs._request("GET", "/x")
    assert slept == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_daily_limit_is_not_retried() -> None:
    t = httpx.MockTransport(lambda _req: httpx.Response(429, json={"policyName": "DAILY"}))
    slept: list[float] = []
    hs = HubSpotClient("tok", transport=t, sleep=slept.append)
    with pytest.raises(HubSpotError) as e:
        hs._request("GET", "/x")
    assert e.value.retryable is False
    assert slept == []


def test_transport_errors_are_retried_then_surface() -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    hs = HubSpotClient("tok", transport=httpx.MockTransport(boom), sleep=lambda _s: None, max_attempts=3)
    with pytest.raises(HubSpotError, match="refused"):
        hs._request("GET", "/x")
    assert hs.stats.transport_errors == 3


def test_ensure_properties_is_repeatable() -> None:
    fake = FakeHubSpot()
    hs, _ = client_for(fake)
    hs.ensure_properties()
    hs.ensure_properties()
    assert ("deals", "ltp_lead_key") in fake.properties


def test_unexpected_status_raises(lead: LeadIn, qualified: Qualification) -> None:
    t = httpx.MockTransport(lambda _req: httpx.Response(400, text="bad property"))
    hs = HubSpotClient("tok", transport=t)
    with pytest.raises(HubSpotError, match="400"):
        hs.upsert_contact(lead, qualified)
    with pytest.raises(HubSpotError):
        hs.find_deal("k")
    with pytest.raises(HubSpotError):
        hs.add_note("x", "1", None)


def test_missing_token_fails_fast() -> None:
    with pytest.raises(ValueError, match="HUBSPOT_TOKEN"):
        HubSpotClient("")
