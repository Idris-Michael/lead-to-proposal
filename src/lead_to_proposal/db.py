"""The lead ledger. Every step writes its result here before moving on, so a worker that dies
mid-lead resumes from the last finished step instead of repeating CRM writes (ADR 0002)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import LeadIn, LeadStatus


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    company: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True, default=LeadStatus.RECEIVED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    hubspot_contact_id: Mapped[str | None] = mapped_column(String(32))
    hubspot_deal_id: Mapped[str | None] = mapped_column(String(32))
    hubspot_note_id: Mapped[str | None] = mapped_column(String(32))
    proposal_md: Mapped[str | None] = mapped_column(Text)
    price_issues: Mapped[list[str] | None] = mapped_column(JSON)
    approval_ref: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class LeadEvent(Base):
    """Append-only timeline shown in the operator console."""

    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def add_event(s: Session, lead_id: str, kind: str, **detail: Any) -> None:
    s.add(LeadEvent(lead_id=lead_id, kind=kind, detail=detail))


def insert_lead(s: Session, lead: LeadIn, idempotency_key: str | None) -> tuple[Lead, bool]:
    """Inserts once. A repeat (same Idempotency-Key header, or same person and message) returns the
    existing row with created=False. The unique constraints decide, so racing requests are safe."""
    row = Lead(
        dedupe_key=lead.dedupe_key(),
        idempotency_key=idempotency_key,
        email=lead.email,
        company=lead.company,
        payload=lead.model_dump(mode="json"),
    )
    s.add(row)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        existing = s.scalars(
            select(Lead).where(
                or_(Lead.dedupe_key == lead.dedupe_key(), Lead.idempotency_key == idempotency_key)
                if idempotency_key
                else Lead.dedupe_key == lead.dedupe_key()
            )
        ).first()
        if existing is None:  # pragma: no cover - constraint fired but row vanished
            raise
        add_event(s, existing.id, "duplicate_ignored")
        s.commit()
        return existing, False
    add_event(s, row.id, "received", source=lead.source)
    s.commit()
    return row, True


def claim_next(s: Session, lease_s: int = 300) -> Lead | None:
    """Takes the oldest waiting lead, or one whose worker's lease ran out. On Postgres
    `FOR UPDATE SKIP LOCKED` lets several workers claim in parallel without blocking or
    double-claiming; SQLite ignores the clause and serialises writers instead."""
    t = now()
    row = s.scalars(
        select(Lead)
        .where(
            or_(
                Lead.status == LeadStatus.RECEIVED.value,
                (Lead.status == LeadStatus.PROCESSING.value) & (Lead.locked_until < t),
            )
        )
        .order_by(Lead.received_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        s.rollback()
        return None
    resumed = row.status == LeadStatus.PROCESSING.value
    row.status = LeadStatus.PROCESSING.value
    row.attempts += 1
    row.locked_until = t + timedelta(seconds=lease_s)
    add_event(s, row.id, "resumed_after_lease_expiry" if resumed else "claimed", attempt=row.attempts)
    s.commit()
    return row
