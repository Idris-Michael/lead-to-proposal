from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from lead_to_proposal.db import Base, make_engine
from lead_to_proposal.hubspot import HubSpotClient
from lead_to_proposal.hubspot_fake import FakeHubSpot
from lead_to_proposal.llm import Completion
from lead_to_proposal.pipeline import Deps


class ScriptedLLM:
    """Replies from a queue, so a test states exactly what the model 'said'."""

    name = "scripted"

    def __init__(self, *replies: str | dict[str, Any]) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str, *, model: str) -> Completion:
        self.prompts.append((system, prompt))
        if not self.replies:
            raise AssertionError("ScriptedLLM ran out of replies")
        return Completion(
            text=self.replies.pop(0), model=model, latency_ms=5, input_tokens=10, output_tokens=5
        )


def verdict(fit: str = "qualified", score: int = 80, line: str = "automation", **over: Any) -> dict[str, Any]:
    return {
        "fit": fit,
        "score": score,
        "service_line": line,
        "reasons": ["they retype orders"],
        "red_flags": [],
        "budget_band": "2k_10k",
        **over,
    }


GOOD_DRAFT = "## Investment\nWorkflow automation sprint (2 weeks): £6,500 + VAT."


def sqlite_sessions(path: str) -> Callable[[], Session]:
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def deps_for(llm: Any, sessions: Callable[[], Session], fake: FakeHubSpot | None = None) -> Deps:
    hs = HubSpotClient("tok", transport=(fake or FakeHubSpot()).transport(), sleep=lambda _s: None)
    return Deps(llm=llm, hubspot=hs, session_factory=sessions, qualify_model="q", draft_model="d")
