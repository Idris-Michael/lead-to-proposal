from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from lead_to_proposal import fake_hubspot_server, llm_proxy
from lead_to_proposal.hubspot import HubSpotClient
from lead_to_proposal.llm import HttpLLM, LLMError, OfflineLLM
from lead_to_proposal.models import LeadIn, Qualification

TOKEN = "proxy-token-0123456789abcdef"


def via(app_client: TestClient) -> httpx.MockTransport:
    """Routes an httpx client's requests into an in-process FastAPI app."""

    def handle(r: httpx.Request) -> httpx.Response:
        res = app_client.request(
            r.method, r.url.raw_path.decode(), content=r.content, headers=dict(r.headers)
        )
        return httpx.Response(res.status_code, content=res.content, headers=res.headers)

    return httpx.MockTransport(handle)


def test_proxy_requires_token_and_allowed_model() -> None:
    c = TestClient(llm_proxy.create_app(OfflineLLM(), TOKEN))
    body = {"system": "QUALIFY", "prompt": "budget_gbp: 5000 automate orders", "model": "claude-haiku-5-5"}
    assert c.post("/v1/complete", json=body).status_code == 401
    bad = {**body, "model": "gpt-9"}
    assert c.post("/v1/complete", json=bad, headers={"authorization": f"Bearer {TOKEN}"}).status_code == 400
    ok = c.post("/v1/complete", json=body, headers={"authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and '"fit"' in ok.json()["text"]
    with pytest.raises(ValueError, match="24"):
        llm_proxy.create_app(OfflineLLM(), "short")


def test_http_llm_round_trips_through_the_proxy() -> None:
    proxy = TestClient(llm_proxy.create_app(OfflineLLM(), TOKEN))
    llm = HttpLLM("http://testserver", TOKEN, transport=via(proxy))
    c = llm.complete("DRAFT", "company: Acme", model="claude-sonnet-5-5")
    assert c.text.startswith("# Proposal for Acme")
    wrong = HttpLLM("http://testserver", "nope", transport=via(proxy))
    with pytest.raises(LLMError, match="401"):
        wrong.complete("s", "p", model="offline")


def test_http_llm_unreachable_is_llm_error() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="unreachable"):
        HttpLLM("http://x", TOKEN, transport=httpx.MockTransport(boom)).complete("s", "p", model="m")


def test_proxy_maps_model_errors_to_502() -> None:
    class Broken:
        name = "broken"

        def complete(self, system: str, prompt: str, *, model: str) -> object:
            raise LLMError("usage limit")

    c = TestClient(llm_proxy.create_app(Broken(), TOKEN))  # type: ignore[arg-type]
    auth = {"authorization": f"Bearer {TOKEN}"}
    body = {"system": "s", "prompt": "p", "model": "claude-haiku-5-5"}
    assert c.post("/v1/complete", json=body, headers=auth).status_code == 502
    # "offline" never reaches the real model, so drills cannot spend model calls by accident.
    assert c.post("/v1/complete", json={**body, "model": "offline"}, headers=auth).status_code == 200


def test_fake_hubspot_server_counts_and_injects(lead: LeadIn, qualified: Qualification) -> None:
    server = TestClient(fake_hubspot_server.create_app())
    hs = HubSpotClient("tok", base_url="http://testserver", transport=via(server), sleep=lambda _s: None)
    cid = hs.upsert_contact(lead, qualified)
    hs.create_deal(lead, qualified, cid, "k1")
    state = server.get("/_fake/state").json()
    assert (state["contacts"], state["deals"], state["duplicate_contacts"]) == (1, 1, 0)
    server.post("/_fake/reset", json={"rate_limit_ratio": 0.5})
    hs.upsert_contact(lead, qualified)
    state = server.get("/_fake/state").json()
    assert state["contacts"] == 1 and state["injected_faults"] == hs.stats.rate_limited
