"""HTTP surface. The webhook only validates, stores and returns 202; all slow work (model calls,
CRM writes) happens in the worker, so the form never waits on HubSpot or Claude."""

import hmac
import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from importlib import resources  # nosemgrep: python37-compatibility-importlib2 -- requires Python 3.12
from statistics import median
from typing import Annotated, Any, Literal

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import approvals, signing
from .config import Settings
from .db import Lead, LeadEvent, add_event, insert_lead, make_engine
from .models import LeadIn, LeadStatus

log = structlog.get_logger(__name__)


class RateLimiter:
    """Sliding one-minute window per client. In-process only: with several replicas, enforce the
    limit at the ingress or with a shared store instead (see ADR 0004)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] >= 60:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(t)
            return True


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)


class GatewayCallback(BaseModel):
    lead_id: str
    approval_id: str
    approved_by: str | None = None


def lead_summary(row: Lead) -> dict[str, Any]:
    q = row.qualification or {}
    return {
        "id": row.id,
        "company": row.company,
        "email": row.email,
        "status": row.status,
        "fit": q.get("fit"),
        "score": q.get("score"),
        "service_line": q.get("service_line"),
        "price_issues": len(row.price_issues or []),
        "received_at": row.received_at.isoformat(),
        "drafted_at": row.drafted_at.isoformat() if row.drafted_at else None,
    }


def create_app(settings: Settings, session_factory: Callable[[], Session] | None = None) -> FastAPI:
    sessions = session_factory or sessionmaker(make_engine(settings.database_url), expire_on_commit=False)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    app = FastAPI(
        title="lead-to-proposal",
        version="0.1.0",
        description="Inbound lead webhook, operator review API and approval callback.",
    )

    def db() -> Iterator[Session]:
        with sessions() as s:
            yield s

    def operator(authorization: Annotated[str | None, Header()] = None) -> str:
        expected = f"Bearer {settings.operator_token.get_secret_value()}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "operator token required")
        return "operator"

    Db = Annotated[Session, Depends(db)]
    Operator = Annotated[str, Depends(operator)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/leads", status_code=202)
    async def receive_lead(
        request: Request,
        s: Db,
        x_timestamp: Annotated[str | None, Header()] = None,
        x_signature: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
    ) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, "rate limit exceeded", headers={"retry-after": "60"})
        body = await request.body()
        try:
            signing.verify(settings.webhook_secret.get_secret_value(), body, x_timestamp, x_signature)
        except signing.SignatureError as e:
            log.warning("webhook_rejected", reason=str(e), client=client)
            raise HTTPException(401, "invalid signature") from e
        try:
            lead = LeadIn.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValidationError) as e:
            detail = (
                e.errors(include_url=False, include_input=False)
                if isinstance(e, ValidationError)
                else "bad json"
            )
            raise HTTPException(422, detail) from e
        # Sync DB work off the event loop, so a burst of submissions is not serialised.
        row, created = await run_in_threadpool(insert_lead, s, lead, idempotency_key)
        log.info("lead_received", lead_id=row.id, duplicate=not created)
        return JSONResponse(
            {"id": row.id, "status": row.status, "duplicate": not created},
            status_code=202 if created else 200,
        )

    @app.get("/v1/leads")
    def list_leads(s: Db, _op: Operator, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = select(Lead).order_by(Lead.received_at.desc()).limit(min(limit, 500))
        if status:
            q = q.where(Lead.status == status)
        return [lead_summary(r) for r in s.scalars(q)]

    @app.get("/v1/leads/{lead_id}")
    def get_lead(lead_id: str, s: Db, _op: Operator) -> dict[str, Any]:
        row = s.get(Lead, lead_id)
        if row is None:
            raise HTTPException(404, "no such lead")
        events = s.scalars(select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.id))
        return {
            **lead_summary(row),
            "lead": row.payload,
            "qualification": row.qualification,
            "hubspot": {
                "contact_id": row.hubspot_contact_id,
                "deal_id": row.hubspot_deal_id,
                "note_id": row.hubspot_note_id,
            },
            "proposal_md": row.proposal_md,
            "price_issues": row.price_issues or [],
            "approval_ref": row.approval_ref,
            "error": row.error,
            "attempts": row.attempts,
            "events": [{"at": e.at.isoformat(), "kind": e.kind, "detail": e.detail} for e in events],
        }

    @app.post("/v1/leads/{lead_id}/decision")
    def decide(lead_id: str, body: Decision, s: Db, _op: Operator) -> dict[str, Any]:
        if settings.approval_mode == "gateway":
            raise HTTPException(409, "approvals are handled by the approval gateway")
        row = s.get(Lead, lead_id)
        if row is None:
            raise HTTPException(404, "no such lead")
        try:
            approvals.decide(s, row, body.decision == "approve", by="console", note=body.note)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return lead_summary(row)

    @app.post("/v1/approvals/callback")
    async def gateway_callback(
        request: Request,
        s: Db,
        x_timestamp: Annotated[str | None, Header()] = None,
        x_signature: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Called by the gateway's `send_proposal` executor after a person approved."""
        body = await request.body()
        try:
            signing.verify(
                settings.gateway_callback_secret.get_secret_value(), body, x_timestamp, x_signature
            )
        except signing.SignatureError as e:
            raise HTTPException(401, "invalid signature") from e
        cb = GatewayCallback.model_validate_json(body)
        row = s.get(Lead, cb.lead_id)
        if row is None or row.approval_ref != cb.approval_id:
            raise HTTPException(404, "no lead with that approval")
        if row.status == LeadStatus.APPROVED.value:
            return {"status": row.status, "duplicate": True}  # the gateway retried; already applied
        try:
            approvals.decide(s, row, True, by="gateway", note=cb.approved_by)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"status": row.status, "duplicate": False}

    @app.get("/v1/stats")
    def stats(s: Db, _op: Operator) -> dict[str, Any]:
        counts = dict(s.execute(select(Lead.status, func.count()).group_by(Lead.status)).all())
        drafted = s.execute(
            select(Lead.received_at, Lead.drafted_at).where(Lead.drafted_at.is_not(None))
        ).all()
        secs = [(d - r).total_seconds() for r, d in drafted if d is not None]
        return {
            "by_status": counts,
            "drafted": len(secs),
            "median_webhook_to_draft_s": round(median(secs), 1) if secs else None,
        }

    @app.post("/v1/leads/{lead_id}/retry")
    def retry(lead_id: str, s: Db, _op: Operator) -> dict[str, Any]:
        row = s.get(Lead, lead_id)
        if row is None:
            raise HTTPException(404, "no such lead")
        if row.status != LeadStatus.FAILED.value:
            raise HTTPException(409, f"lead is {row.status}; only failed leads can be retried")
        row.status, row.attempts, row.error = LeadStatus.RECEIVED.value, 0, None
        add_event(s, row.id, "manual_retry")
        s.commit()
        return lead_summary(row)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def console() -> str:
        return resources.files("lead_to_proposal").joinpath("static/console.html").read_text("utf-8")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Any:
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["x-frame-options"] = "DENY"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["content-security-policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; frame-ancestors 'none'"
        )
        return response

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    from . import telemetry

    settings = Settings()
    telemetry.setup(settings.service_name + "-api", settings.otel_exporter_otlp_endpoint)
    app = create_app(settings)
    FastAPIInstrumentor.instrument_app(app)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)  # noqa: S104 - container port
