"""The LangGraph flow for one lead:

    qualify ─┬─ qualified ─> crm ─> draft ─> await_approval
             ├─ nurture ───> crm ─> (stop: nurture)
             └─ reject ────> (stop: disqualified, nothing written to the CRM)

Each node first checks the ledger: if its result is already stored (a previous attempt got that
far before dying) it reuses it. That is what makes a retried lead safe to re-run end to end."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace
from sqlalchemy.orm import Session

from . import agent
from .db import Lead, add_event, now
from .hubspot import HubSpotClient
from .llm import LLM
from .models import Fit, LeadIn, LeadStatus, Qualification

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class Deps:
    llm: LLM
    hubspot: HubSpotClient
    session_factory: Callable[[], Session]
    qualify_model: str
    draft_model: str


class State(TypedDict, total=False):
    lead_id: str
    fit: str


def _with_lead(deps: Deps, lead_id: str, fn: Callable[[Session, Lead], dict[str, Any]]) -> dict[str, Any]:
    with deps.session_factory() as s:
        row = s.get(Lead, lead_id)
        if row is None:
            raise LookupError(f"lead {lead_id} vanished")
        out = fn(s, row)
        s.commit()
        return out


def _log_calls(s: Session, lead_id: str, step: str, result: agent.StepResult) -> None:
    for c in result.calls:
        add_event(
            s,
            lead_id,
            f"llm_{step}",
            model=c.model,
            latency_ms=c.latency_ms,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
        )
    for n in result.notes:
        add_event(s, lead_id, f"{step}_note", note=n)


def build(deps: Deps) -> Any:
    def qualify(state: State) -> State:
        def run(s: Session, row: Lead) -> dict[str, Any]:
            if row.qualification is None:
                with tracer.start_as_current_span("qualify"):
                    q, result = agent.qualify(deps.llm, LeadIn(**row.payload), model=deps.qualify_model)
                row.qualification = q.model_dump(mode="json")
                _log_calls(s, row.id, "qualify", result)
                add_event(s, row.id, "qualified", fit=q.fit.value, score=q.score)
            return {"fit": row.qualification["fit"]}

        return _with_lead(deps, state["lead_id"], run)  # type: ignore[return-value]

    def crm(state: State) -> State:
        def run(s: Session, row: Lead) -> dict[str, Any]:
            lead = LeadIn(**row.payload)
            q = Qualification.model_validate(row.qualification)
            with tracer.start_as_current_span("hubspot"):
                if row.hubspot_contact_id is None:
                    row.hubspot_contact_id = deps.hubspot.upsert_contact(lead, q)
                    add_event(s, row.id, "hubspot_contact", id=row.hubspot_contact_id)
                    s.commit()
                if q.fit is Fit.QUALIFIED and row.hubspot_deal_id is None:
                    row.hubspot_deal_id = deps.hubspot.create_deal(lead, q, row.hubspot_contact_id, row.id)
                    add_event(s, row.id, "hubspot_deal", id=row.hubspot_deal_id)
                    s.commit()
                if row.hubspot_note_id is None:
                    body = (
                        f"Lead triage: {q.fit.value} ({q.score}/100), {q.service_line}.\n"
                        f"Reasons: {'; '.join(q.reasons)}"
                        + (f"\nRed flags: {'; '.join(q.red_flags)}" if q.red_flags else "")
                    )
                    row.hubspot_note_id = deps.hubspot.add_note(
                        body, row.hubspot_contact_id, row.hubspot_deal_id
                    )
                    add_event(s, row.id, "hubspot_note", id=row.hubspot_note_id)
            return {}

        return _with_lead(deps, state["lead_id"], run)  # type: ignore[return-value]

    def draft(state: State) -> State:
        def run(s: Session, row: Lead) -> dict[str, Any]:
            if row.proposal_md is None:
                lead = LeadIn(**row.payload)
                q = Qualification.model_validate(row.qualification)
                with tracer.start_as_current_span("draft"):
                    text, issues, result = agent.draft_proposal(deps.llm, lead, q, model=deps.draft_model)
                row.proposal_md, row.price_issues, row.drafted_at = text, issues, now()
                _log_calls(s, row.id, "draft", result)
                add_event(s, row.id, "drafted", price_issues=len(issues))
            row.status = LeadStatus.AWAITING_APPROVAL.value
            row.locked_until = None
            return {}

        return _with_lead(deps, state["lead_id"], run)  # type: ignore[return-value]

    def finish(status: LeadStatus) -> Any:  # LangGraph's node protocol rejects a plain Callable alias
        def run_node(state: State) -> State:
            def run(s: Session, row: Lead) -> dict[str, Any]:
                row.status, row.locked_until, row.decided_at = status.value, None, now()
                add_event(s, row.id, status.value)
                return {}

            return _with_lead(deps, state["lead_id"], run)  # type: ignore[return-value]

        return run_node

    g = StateGraph(State)
    g.add_node("qualify", qualify)
    g.add_node("crm", crm)
    g.add_node("draft", draft)
    g.add_node("nurture", finish(LeadStatus.NURTURE))
    g.add_node("disqualify", finish(LeadStatus.DISQUALIFIED))
    g.add_edge(START, "qualify")
    g.add_conditional_edges(
        "qualify",
        lambda st: {"qualified": "crm", "nurture": "crm", "reject": "disqualify"}[st["fit"]],
    )
    g.add_conditional_edges("crm", lambda st: "draft" if st["fit"] == "qualified" else "nurture")
    g.add_edge("draft", END)
    g.add_edge("nurture", END)
    g.add_edge("disqualify", END)
    return g.compile()


def process(deps: Deps, lead_id: str) -> None:
    with tracer.start_as_current_span("process_lead", attributes={"lead.id": lead_id}):
        build(deps).invoke({"lead_id": lead_id})
