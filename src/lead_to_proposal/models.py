"""Data shapes at the system boundaries: what the web form sends, and what the model must return."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator

SERVICE_LINES = ("automation", "analytics", "paid_ads", "web", "other")
ServiceLine = Literal["automation", "analytics", "paid_ads", "web", "other"]


class LeadIn(BaseModel):
    """A web-form submission. Extra fields are rejected so a changed form fails loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    company: str = Field(min_length=1, max_length=160)
    website: HttpUrl | None = None
    message: str = Field(min_length=10, max_length=4000)
    budget_gbp: int | None = Field(default=None, ge=0, le=10_000_000)
    source: str = Field(default="web_form", max_length=40)

    @field_validator("email")
    @classmethod
    def lower_email(cls, v: str) -> str:
        return v.lower()

    def dedupe_key(self) -> str:
        """Same person, same request: the key that makes a re-submitted form a no-op."""
        basis = f"{self.email}\n{' '.join(self.message.lower().split())}"
        return hashlib.sha256(basis.encode()).hexdigest()[:32]


class Fit(StrEnum):
    QUALIFIED = "qualified"
    NURTURE = "nurture"
    REJECT = "reject"


class Qualification(BaseModel):
    """The model's verdict. Validated strictly; a reply that does not fit is retried, then failed."""

    model_config = ConfigDict(extra="forbid")

    fit: Fit
    score: int = Field(ge=0, le=100)
    service_line: ServiceLine
    reasons: list[str] = Field(min_length=1, max_length=5)
    red_flags: list[str] = Field(default_factory=list, max_length=5)
    budget_band: Literal["unknown", "under_2k", "2k_10k", "10k_50k", "over_50k"]


class LeadStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"
    NURTURE = "nurture"
    DISQUALIFIED = "disqualified"
    FAILED = "failed"


TERMINAL_STATUSES = {
    LeadStatus.APPROVED,
    LeadStatus.REJECTED_BY_REVIEWER,
    LeadStatus.NURTURE,
    LeadStatus.DISQUALIFIED,
    LeadStatus.FAILED,
}
