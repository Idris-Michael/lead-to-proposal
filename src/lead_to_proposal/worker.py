"""The worker: claim a lead, run the pipeline, and on failure schedule a retry by extending the
lease with backoff. After `worker_max_attempts` the lead is marked failed for a person to look at.
Several workers can run at once; Postgres SKIP LOCKED keeps them off each other's leads."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from datetime import timedelta
from types import FrameType

import structlog
from sqlalchemy.orm import Session, sessionmaker

from . import approvals, pipeline
from .config import Settings
from .db import Lead, add_event, claim_next, make_engine, now
from .hubspot import HubSpotClient
from .hubspot_fake import FakeHubSpot
from .llm import LLM, ClaudeCli, OfflineLLM
from .models import LeadStatus

log = structlog.get_logger(__name__)
RETRY_BASE_S = 15


def build_deps(
    settings: Settings, sessions: Callable[[], Session], fake: FakeHubSpot | None = None
) -> pipeline.Deps:
    if settings.hubspot_mode == "live":
        hubspot = HubSpotClient(settings.hubspot_token.get_secret_value())
    else:
        fake = fake or FakeHubSpot(rate_limit_ratio=settings.hubspot_fake_429_ratio)
        hubspot = HubSpotClient(
            "mock-token", base_url="https://fake.hubapi.local", transport=fake.transport()
        )
    llm: LLM = OfflineLLM() if settings.llm_mode == "offline" else ClaudeCli(settings.claude_cli_path)
    return pipeline.Deps(
        llm=llm,
        hubspot=hubspot,
        session_factory=sessions,
        qualify_model=settings.qualify_model,
        draft_model=settings.draft_model,
    )


def run_one(deps: pipeline.Deps, settings: Settings) -> bool:
    """Processes at most one lead. Returns False when the queue was empty."""
    with deps.session_factory() as s:
        row = claim_next(s, settings.worker_lease_s)
        if row is None:
            return False
        lead_id, attempts = row.id, row.attempts
    log_ = log.bind(lead_id=lead_id, attempt=attempts)
    try:
        pipeline.process(deps, lead_id)
        log_.info("lead_processed")
    except Exception as e:
        _record_failure(deps.session_factory, lead_id, attempts, e, settings.worker_max_attempts)
        log_.warning("lead_failed", error=str(e)[:300])
    return True


def _record_failure(
    sessions: Callable[[], Session], lead_id: str, attempts: int, e: Exception, max_attempts: int
) -> None:
    with sessions() as s:
        row = s.get(Lead, lead_id)
        if row is None:  # pragma: no cover
            return
        row.error = f"{type(e).__name__}: {e}"[:2000]
        if attempts >= max_attempts:
            row.status, row.locked_until = LeadStatus.FAILED.value, None
            add_event(s, lead_id, "failed", error=row.error)
        else:
            delay = RETRY_BASE_S * 2 ** (attempts - 1)
            row.locked_until = now() + timedelta(seconds=delay)  # claim_next picks it up when this passes
            add_event(s, lead_id, "retry_scheduled", in_s=delay, error=row.error)
        s.commit()


def main() -> None:  # pragma: no cover - process entry point
    from . import telemetry

    settings = Settings()
    telemetry.setup(settings.service_name + "-worker", settings.otel_exporter_otlp_endpoint)
    sessions = sessionmaker(make_engine(settings.database_url), expire_on_commit=False)
    deps = build_deps(settings, sessions)
    if settings.hubspot_mode == "live":
        deps.hubspot.ensure_properties()
    gw = (
        approvals.GatewayApprovals(settings.gateway_url, settings.gateway_agent_key.get_secret_value())
        if settings.approval_mode == "gateway"
        else None
    )
    stopping = False

    def stop(_sig: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info(
        "worker_started", llm=deps.llm.name, hubspot=settings.hubspot_mode, approvals=settings.approval_mode
    )
    while not stopping:
        worked = run_one(deps, settings)
        if gw is not None:
            with sessions() as s:
                try:
                    approvals.request_pending(s, gw)
                    approvals.sync_decisions(s, gw)
                except (approvals.GatewayError, OSError) as e:
                    log.warning("gateway_unavailable", error=str(e)[:200])
        if not worked:
            time.sleep(settings.worker_poll_s)
    log.info("worker_stopped")
