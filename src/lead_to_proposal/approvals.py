"""Who signs off a draft before it goes out.

local:   an operator approves in this service's console.
gateway: the worker asks the agent approval gateway (a separate project) to run `send_proposal`;
         a person approves in Telegram or the gateway console; the gateway's executor then calls
         back here, signed. Rejections and expiries are picked up by polling the gateway."""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Lead, add_event, now
from .models import LeadStatus, Qualification

log = structlog.get_logger(__name__)


class GatewayError(Exception):
    pass


class GatewayApprovals:
    def __init__(
        self, base_url: str, agent_key: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=10.0,
            headers={"authorization": f"Bearer {agent_key}"},
        )

    def request(self, row: Lead) -> str:
        """Idempotent on the lead id: asking twice returns the same approval, not a second card."""
        q = Qualification.model_validate(row.qualification)
        args: dict[str, Any] = {
            "lead_id": row.id,
            "company": row.company,
            "service_line": q.service_line,
            "score": q.score,
            "hubspot_deal_id": row.hubspot_deal_id or "",
            "price_issues": len(row.price_issues or []),
        }
        res = self._http.post(
            "/v1/tool-calls",
            json={"tool": "send_proposal", "args": args, "reason": f"Proposal draft for {row.company}"},
            headers={"idempotency-key": f"ltp-{row.id}"},
        )
        data = res.json() if res.content else {}
        if res.status_code == 202:
            return str(data["approval"]["id"])
        raise GatewayError(
            f"gateway {res.status_code}: {data.get('error') or data.get('reason') or res.text[:200]}"
        )

    def status(self, approval_id: str) -> str:
        res = self._http.get(f"/v1/approvals/{approval_id}")
        if res.status_code != 200:
            raise GatewayError(f"gateway {res.status_code} for approval {approval_id}")
        return str(res.json()["status"])


def request_pending(s: Session, gw: GatewayApprovals) -> int:
    """Sends every awaiting draft that has no approval yet. Returns how many were sent."""
    rows = s.scalars(
        select(Lead).where(Lead.status == LeadStatus.AWAITING_APPROVAL.value, Lead.approval_ref.is_(None))
    ).all()
    for row in rows:
        row.approval_ref = gw.request(row)
        add_event(s, row.id, "approval_requested", via="gateway", approval_id=row.approval_ref)
        s.commit()
    return len(rows)


def sync_decisions(s: Session, gw: GatewayApprovals) -> int:
    """Marks leads whose gateway approval was rejected or expired. Approvals arrive by callback."""
    changed = 0
    rows = s.scalars(
        select(Lead).where(Lead.status == LeadStatus.AWAITING_APPROVAL.value, Lead.approval_ref.is_not(None))
    ).all()
    for row in rows:
        status = gw.status(str(row.approval_ref))
        if status in ("rejected", "expired"):
            row.status, row.decided_at = LeadStatus.REJECTED_BY_REVIEWER.value, now()
            add_event(s, row.id, "reviewer_rejected", via="gateway", gateway_status=status)
            changed += 1
    s.commit()
    return changed


def decide(s: Session, row: Lead, approve: bool, *, by: str, note: str | None = None) -> None:
    if row.status != LeadStatus.AWAITING_APPROVAL.value:
        raise ValueError(f"lead is {row.status}, not awaiting approval")
    row.status = (LeadStatus.APPROVED if approve else LeadStatus.REJECTED_BY_REVIEWER).value
    row.decided_at = now()
    add_event(s, row.id, "reviewer_approved" if approve else "reviewer_rejected", via=by, note=note)
    s.commit()
