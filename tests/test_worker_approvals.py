from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from lead_to_proposal import approvals, worker
from lead_to_proposal.db import Lead, LeadEvent, insert_lead
from lead_to_proposal.hubspot_fake import FakeHubSpot
from lead_to_proposal.models import LeadIn, LeadStatus

from .conftest import lead_payload
from .helpers import GOOD_DRAFT, ScriptedLLM, deps_for, sqlite_sessions, verdict
from .test_api import settings


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    return sqlite_sessions(str(tmp_path / "w.db"))


def add(sessions: Any) -> str:
    with sessions() as s:
        return insert_lead(s, LeadIn(**lead_payload()), None)[0].id


def test_run_one_processes_then_reports_empty(sessions: Any) -> None:
    lead_id = add(sessions)
    deps = deps_for(ScriptedLLM(verdict(), GOOD_DRAFT), sessions)
    assert worker.run_one(deps, settings()) is True
    assert worker.run_one(deps, settings()) is False
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.AWAITING_APPROVAL.value


def test_failures_back_off_then_mark_failed(sessions: Any) -> None:
    lead_id = add(sessions)
    cfg = settings(worker_max_attempts=2, worker_lease_s=0)
    deps = deps_for(ScriptedLLM("bad", "bad"), sessions)
    worker.run_one(deps, cfg)
    with sessions() as s:
        row = s.get(Lead, lead_id)
        assert row.status == LeadStatus.PROCESSING.value and row.error.startswith("LLMError")
        row.locked_until = None  # skip the 15 s backoff
        s.commit()
    worker.run_one(deps_for(ScriptedLLM("bad", "bad"), sessions), cfg)
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.FAILED.value
        kinds = [e.kind for e in s.query(LeadEvent).filter_by(lead_id=lead_id)]
    assert "retry_scheduled" in kinds and kinds[-1] == "failed"


def test_build_deps_modes(sessions: Any) -> None:
    deps = worker.build_deps(settings(llm_mode="offline", hubspot_fake_429_ratio=0.2), sessions)
    assert deps.llm.name == "offline"
    fake = FakeHubSpot()
    deps = worker.build_deps(settings(llm_mode="offline"), sessions, fake)
    lead_id = add(sessions)
    assert worker.run_one(deps, settings()) is True
    assert len(fake.deals) == 1
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.AWAITING_APPROVAL.value


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = "pending"

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            self.requests.append({"key": req.headers["idempotency-key"], "body": req.read()})
            return httpx.Response(202, json={"approval": {"id": "apr_9"}})
        return httpx.Response(200, json={"status": self.status})


def test_gateway_request_and_rejection_sync(sessions: Any) -> None:
    lead_id = add(sessions)
    with sessions() as s:
        row = s.get(Lead, lead_id)
        row.status, row.qualification = LeadStatus.AWAITING_APPROVAL.value, verdict()
        s.commit()
    fg = FakeGateway()
    gw = approvals.GatewayApprovals("http://gw", "key", transport=httpx.MockTransport(fg.handle))
    with sessions() as s:
        assert approvals.request_pending(s, gw) == 1
        assert approvals.request_pending(s, gw) == 0  # already requested
        assert approvals.sync_decisions(s, gw) == 0  # still pending
        fg.status = "expired"
        assert approvals.sync_decisions(s, gw) == 1
        row = s.get(Lead, lead_id)
        assert row.status == LeadStatus.REJECTED_BY_REVIEWER.value and row.approval_ref == "apr_9"
    assert fg.requests[0]["key"] == f"ltp-{lead_id}"


def test_gateway_errors_surface(sessions: Any) -> None:
    lead_id = add(sessions)
    with sessions() as s:
        row = s.get(Lead, lead_id)
        row.qualification = verdict()
        gw = approvals.GatewayApprovals(
            "http://gw",
            "k",
            transport=httpx.MockTransport(lambda _r: httpx.Response(403, json={"reason": "denied"})),
        )
        with pytest.raises(approvals.GatewayError, match="denied"):
            gw.request(row)
        with pytest.raises(approvals.GatewayError):
            gw.status("x")
        with pytest.raises(ValueError, match="not awaiting"):
            approvals.decide(s, row, True, by="t")
