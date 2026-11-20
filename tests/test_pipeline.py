from __future__ import annotations

from pathlib import Path

import pytest

from lead_to_proposal import pipeline
from lead_to_proposal.db import Lead, LeadEvent, claim_next, insert_lead
from lead_to_proposal.hubspot_fake import FakeHubSpot
from lead_to_proposal.llm import LLMError
from lead_to_proposal.models import LeadIn, LeadStatus

from .conftest import lead_payload
from .helpers import GOOD_DRAFT, ScriptedLLM, deps_for, sqlite_sessions, verdict


@pytest.fixture
def sessions(tmp_path: Path):  # type: ignore[no-untyped-def]
    return sqlite_sessions(str(tmp_path / "t.db"))


def add(sessions, **over) -> str:  # type: ignore[no-untyped-def]
    with sessions() as s:
        row, created = insert_lead(s, LeadIn(**lead_payload(**over)), None)
        assert created
        return row.id


def test_duplicate_submission_returns_existing_row(sessions) -> None:  # type: ignore[no-untyped-def]
    with sessions() as s:
        a, created_a = insert_lead(s, LeadIn(**lead_payload()), "idem-1")
        b, created_b = insert_lead(s, LeadIn(**lead_payload(email="SAM.carter@example.co.uk")), None)
        c, created_c = insert_lead(
            s, LeadIn(**lead_payload(message="A different request entirely")), "idem-1"
        )
    assert (created_a, created_b, created_c) == (True, False, False)
    assert a.id == b.id == c.id


def test_qualified_lead_goes_to_crm_and_awaits_approval(sessions) -> None:  # type: ignore[no-untyped-def]
    fake = FakeHubSpot()
    lead_id = add(sessions)
    pipeline.process(deps_for(ScriptedLLM(verdict(), GOOD_DRAFT), sessions, fake), lead_id)
    with sessions() as s:
        row = s.get(Lead, lead_id)
        assert row is not None
        assert row.status == LeadStatus.AWAITING_APPROVAL.value
        assert row.hubspot_deal_id and row.hubspot_note_id and row.proposal_md == GOOD_DRAFT
    assert len(fake.contacts) == len(fake.deals) == len(fake.notes) == 1


def test_nurture_lead_gets_contact_and_note_but_no_deal(sessions) -> None:  # type: ignore[no-untyped-def]
    fake = FakeHubSpot()
    lead_id = add(sessions)
    pipeline.process(deps_for(ScriptedLLM(verdict(fit="nurture", score=45)), sessions, fake), lead_id)
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.NURTURE.value  # type: ignore[union-attr]
    assert (len(fake.contacts), len(fake.deals), len(fake.notes)) == (1, 0, 1)


def test_rejected_lead_writes_nothing_to_crm(sessions) -> None:  # type: ignore[no-untyped-def]
    fake = FakeHubSpot()
    lead_id = add(sessions)
    pipeline.process(deps_for(ScriptedLLM(verdict(fit="reject", score=3)), sessions, fake), lead_id)
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.DISQUALIFIED.value  # type: ignore[union-attr]
    assert fake.calls == 0


def test_crash_after_deal_resumes_without_duplicates(sessions) -> None:  # type: ignore[no-untyped-def]
    """The worker dies after HubSpot created the deal but before the note. The retry must reuse the
    stored qualification (no second model call) and the stored deal (no second deal)."""
    fake = FakeHubSpot()
    lead_id = add(sessions)
    deps = deps_for(ScriptedLLM(verdict()), sessions, fake)

    def die(*_a: object, **_k: object) -> str:
        raise ConnectionError("worker killed")

    deps.hubspot.add_note = die  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        pipeline.process(deps, lead_id)

    retry = deps_for(ScriptedLLM(GOOD_DRAFT), sessions, fake)  # only the draft reply is available
    pipeline.process(retry, lead_id)
    with sessions() as s:
        assert s.get(Lead, lead_id).status == LeadStatus.AWAITING_APPROVAL.value  # type: ignore[union-attr]
    assert (len(fake.contacts), len(fake.deals), len(fake.notes)) == (1, 1, 1)


def test_model_failure_propagates_and_leaves_no_crm_writes(sessions) -> None:  # type: ignore[no-untyped-def]
    fake = FakeHubSpot()
    lead_id = add(sessions)
    with pytest.raises(LLMError):
        pipeline.process(deps_for(ScriptedLLM("x", "y"), sessions, fake), lead_id)
    assert fake.calls == 0


def test_claim_takes_oldest_and_recovers_expired_leases(sessions) -> None:  # type: ignore[no-untyped-def]
    first = add(sessions)
    second = add(sessions, email="other@example.com")
    with sessions() as s:
        a = claim_next(s, lease_s=-1)  # lease already expired: looks like a dead worker
        assert a is not None and a.id == first
    with sessions() as s:
        b = claim_next(s)
        assert b is not None and b.id == first and b.attempts == 2
        c = claim_next(s)
        assert c is not None and c.id == second
        assert claim_next(s) is None
        kinds = [e.kind for e in s.query(LeadEvent).filter_by(lead_id=first)]
    assert kinds == ["received", "claimed", "resumed_after_lease_expiry"]
