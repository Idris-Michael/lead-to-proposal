from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from lead_to_proposal.agent import DRAFT_SYSTEM, QUALIFY_SYSTEM

ROOT = Path(__file__).resolve().parents[1]


def load_builder():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("build_workflow", ROOT / "n8n" / "build_workflow.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_workflow_matches_the_builder() -> None:
    """Prompts live in agent.py; if they change, the n8n JSON must be regenerated."""
    builder = load_builder()
    committed = (ROOT / "n8n" / "lead-to-proposal.json").read_text("utf-8")
    assert committed == builder.render(), "run: uv run python n8n/build_workflow.py"


def test_both_twins_send_the_same_prompts() -> None:
    wf = json.loads((ROOT / "n8n" / "lead-to-proposal.json").read_text("utf-8"))
    by_name = {n["name"]: n["parameters"] for n in wf["nodes"]}
    assert json.dumps(QUALIFY_SYSTEM) in by_name["Qualify (LLM proxy)"]["jsonBody"]
    assert json.dumps(DRAFT_SYSTEM) in by_name["Draft prompt"]["jsCode"]


def test_every_connection_points_at_a_real_node() -> None:
    wf = json.loads((ROOT / "n8n" / "lead-to-proposal.json").read_text("utf-8"))
    names = {n["name"] for n in wf["nodes"]}
    for src, outs in wf["connections"].items():
        assert src in names
        for branch in outs["main"]:
            for link in branch:
                assert link["node"] in names
