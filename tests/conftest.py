from __future__ import annotations

from typing import Any

import pytest

from lead_to_proposal.models import Fit, LeadIn, Qualification


def lead_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Sam Carter",
        "email": "Sam.Carter@example.co.uk",
        "company": "Example Joinery Ltd",
        "website": "https://example.co.uk",
        "message": "We take orders by email and retype them into Xero. Can you automate this?",
        "budget_gbp": 8000,
    }
    base.update(over)
    return base


@pytest.fixture
def lead() -> LeadIn:
    return LeadIn(**lead_payload())


@pytest.fixture
def qualified() -> Qualification:
    return Qualification(
        fit=Fit.QUALIFIED,
        score=82,
        service_line="automation",
        reasons=["Clear manual process", "Budget stated"],
        budget_band="2k_10k",
    )
