from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from lead_to_proposal import llm as llm_mod
from lead_to_proposal.llm import ClaudeCli, LLMError


def fake_run(stdout: str, seen: dict[str, Any]) -> Any:
    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["args"], seen["input"] = args, kw["input"]
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_cli_isolates_the_model_and_sends_prompt_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    out = {"result": "hi", "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 2}}
    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run(json.dumps(out), seen))
    c = ClaudeCli(binary="claude").complete("SYS", "lead text; rm -rf /", model="claude-haiku-5-5")
    args = seen["args"]
    assert args[args.index("--tools") + 1] == ""  # no tools: lead text cannot trigger actions
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert "lead text" not in " ".join(args)  # the untrusted text only travels on stdin
    assert seen["input"] == "lead text; rm -rf /"
    assert (c.text, c.input_tokens, c.output_tokens) == ("hi", 10, 2)


@pytest.mark.parametrize(
    ("stdout", "match"),
    [("not json", "unreadable"), (json.dumps({"is_error": True, "result": "usage limit"}), "usage limit")],
)
def test_cli_errors_are_raised(monkeypatch: pytest.MonkeyPatch, stdout: str, match: str) -> None:
    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run(stdout, {}))
    with pytest.raises(LLMError, match=match):
        ClaudeCli(binary="claude").complete("s", "p", model="m")


def test_cli_timeout_is_an_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(args: list[str], **kw: Any) -> None:
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(llm_mod.subprocess, "run", slow)
    with pytest.raises(LLMError, match="timed out"):
        ClaudeCli(binary="claude", timeout_s=1).complete("s", "p", model="m")


def test_missing_cli_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _n: None)
    with pytest.raises(LLMError, match="not found"):
        ClaudeCli()
