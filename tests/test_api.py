from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lead_to_proposal.api import RateLimiter, create_app
from lead_to_proposal.config import Settings
from lead_to_proposal.db import Lead
from lead_to_proposal.models import LeadStatus
from lead_to_proposal.signing import sign

from .conftest import lead_payload
from .helpers import sqlite_sessions

OP = "op-token-0123456789abcdefghij"
AUTH = {"authorization": f"Bearer {OP}"}


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"webhook_secret": "hook", "operator_token": OP, "gateway_callback_secret": "cb"}
    return Settings(_env_file=None, **{**base, **over})  # type: ignore[call-arg]


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[TestClient, Any]:
    sessions = sqlite_sessions(str(tmp_path / "api.db"))
    return TestClient(create_app(settings(), sessions)), sessions


def post_lead(client: TestClient, payload: dict[str, Any], secret: str = "hook", **headers: str) -> Any:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    h = {"x-timestamp": str(ts), "x-signature": sign(secret, body, ts), "content-type": "application/json"}
    return client.post("/v1/leads", content=body, headers={**h, **headers})


def test_signed_lead_is_accepted_and_a_resubmit_is_a_no_op(ctx: tuple[TestClient, Any]) -> None:
    client, _ = ctx
    first = post_lead(client, lead_payload())
    again = post_lead(client, lead_payload())
    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert again.status_code == 200 and again.json() == {**first.json(), "duplicate": True}


def test_unsigned_or_forged_requests_are_rejected(ctx: tuple[TestClient, Any]) -> None:
    client, _ = ctx
    assert post_lead(client, lead_payload(), secret="wrong").status_code == 401
    assert client.post("/v1/leads", json=lead_payload()).status_code == 401


def test_invalid_payload_is_422_without_echoing_input(ctx: tuple[TestClient, Any]) -> None:
    client, _ = ctx
    res = post_lead(client, lead_payload(email="not-an-email"))
    assert res.status_code == 422
    assert "not-an-email" not in res.text


def test_operator_endpoints_require_the_token(ctx: tuple[TestClient, Any]) -> None:
    client, _ = ctx
    assert client.get("/v1/leads").status_code == 401
    assert client.get("/v1/leads", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/leads", headers=AUTH).status_code == 200


def test_review_flow(ctx: tuple[TestClient, Any]) -> None:
    client, sessions = ctx
    lead_id = post_lead(client, lead_payload()).json()["id"]
    assert (
        client.post(f"/v1/leads/{lead_id}/decision", json={"decision": "approve"}, headers=AUTH).status_code
        == 409
    )
    with sessions() as s:
        row = s.get(Lead, lead_id)
        row.status, row.qualification, row.proposal_md = (
            LeadStatus.AWAITING_APPROVAL.value,
            {"fit": "qualified"},
            "# Hi",
        )
        s.commit()
    listed = client.get("/v1/leads?status=awaiting_approval", headers=AUTH).json()
    assert [r["id"] for r in listed] == [lead_id]
    res = client.post(
        f"/v1/leads/{lead_id}/decision", json={"decision": "approve", "note": "ok"}, headers=AUTH
    )
    assert res.json()["status"] == "approved"
    detail = client.get(f"/v1/leads/{lead_id}", headers=AUTH).json()
    assert [e["kind"] for e in detail["events"]] == ["received", "reviewer_approved"]
    assert client.get("/v1/leads/nope", headers=AUTH).status_code == 404


def test_failed_lead_can_be_retried(ctx: tuple[TestClient, Any]) -> None:
    client, sessions = ctx
    lead_id = post_lead(client, lead_payload()).json()["id"]
    assert client.post(f"/v1/leads/{lead_id}/retry", headers=AUTH).status_code == 409
    with sessions() as s:
        s.get(Lead, lead_id).status = LeadStatus.FAILED.value
        s.commit()
    assert client.post(f"/v1/leads/{lead_id}/retry", headers=AUTH).json()["status"] == "received"


def test_gateway_callback_is_signed_and_idempotent(tmp_path: Path) -> None:
    sessions = sqlite_sessions(str(tmp_path / "gw.db"))
    client = TestClient(create_app(settings(approval_mode="gateway", gateway_agent_key="k"), sessions))
    lead_id = post_lead(client, lead_payload()).json()["id"]
    with sessions() as s:
        row = s.get(Lead, lead_id)
        row.status, row.approval_ref = LeadStatus.AWAITING_APPROVAL.value, "apr_1"
        s.commit()

    def callback(payload: dict[str, Any], secret: str = "cb") -> Any:
        body = json.dumps(payload).encode()
        ts = int(time.time())
        return client.post(
            "/v1/approvals/callback",
            content=body,
            headers={"x-timestamp": str(ts), "x-signature": sign(secret, body, ts)},
        )

    ok = {"lead_id": lead_id, "approval_id": "apr_1", "approved_by": "sales-lead"}
    assert callback(ok, secret="forged").status_code == 401
    assert callback({**ok, "approval_id": "apr_other"}).status_code == 404
    assert callback(ok).json() == {"status": "approved", "duplicate": False}
    assert callback(ok).json() == {"status": "approved", "duplicate": True}
    # In gateway mode the console cannot bypass the gateway.
    assert (
        client.post(f"/v1/leads/{lead_id}/decision", json={"decision": "approve"}, headers=AUTH).status_code
        == 409
    )


def test_stats_and_console(ctx: tuple[TestClient, Any]) -> None:
    client, _ = ctx
    post_lead(client, lead_payload())
    stats = client.get("/v1/stats", headers=AUTH).json()
    assert stats["by_status"] == {"received": 1} and stats["median_webhook_to_draft_s"] is None
    page = client.get("/")
    assert page.status_code == 200 and "Lead Desk" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert client.get("/healthz").json() == {"status": "ok"}


def test_rate_limiter_window() -> None:
    t = [0.0]
    rl = RateLimiter(2, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 60.0
    assert rl.allow("a")


def test_webhook_rate_limit_returns_429(tmp_path: Path) -> None:
    client = TestClient(
        create_app(settings(rate_limit_per_minute=1), sqlite_sessions(str(tmp_path / "rl.db")))
    )
    assert post_lead(client, lead_payload()).status_code == 202
    res = post_lead(client, lead_payload(email="b@example.com"))
    assert res.status_code == 429 and res.headers["retry-after"] == "60"


def test_settings_refuse_unsafe_modes() -> None:
    with pytest.raises(ValueError, match="HUBSPOT_TOKEN"):
        settings(hubspot_mode="live")
    with pytest.raises(ValueError, match="GATEWAY_AGENT_KEY"):
        settings(approval_mode="gateway", gateway_callback_secret="")
    with pytest.raises(ValueError):
        settings(operator_token="short")
