"""Model adapters. `ClaudeCli` runs Claude headless through `claude -p` (a subscription, no API key);
`OfflineLLM` is a deterministic stand-in so a fresh clone and CI run with no credentials at all."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    def complete(self, system: str, prompt: str, *, model: str) -> Completion: ...


class ClaudeCli:
    """`claude -p` with tools, MCP servers, slash commands and user settings switched off, so the
    model only sees the prompt. The lead text never reaches a shell: argv is a list, the prompt
    goes on stdin."""

    name = "claude-cli"

    def __init__(self, binary: str | None = None, timeout_s: float = 180.0) -> None:
        found = binary or shutil.which("claude")
        if not found:
            raise LLMError("claude CLI not found on PATH (set CLAUDE_CLI_PATH or use LLM_MODE=offline)")
        self._bin = found
        self._timeout = timeout_s

    def complete(self, system: str, prompt: str, *, model: str) -> Completion:
        args = [
            self._bin,
            "-p",
            "--model",
            model,
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--system-prompt",
            system,
        ]
        started = time.perf_counter()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, binary resolved from PATH
                args, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=self._timeout
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude-cli timed out after {self._timeout}s") from e
        latency = int((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"unreadable claude-cli output: {(proc.stdout or proc.stderr)[:160]}") from e
        if data.get("is_error"):
            raise LLMError(f"claude-cli error: {str(data.get('result'))[:200]}")
        usage = data.get("usage") or {}
        return Completion(
            text=str(data.get("result", "")),
            model=model,
            latency_ms=latency,
            input_tokens=int(usage.get("input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


class HttpLLM:
    """Calls the LLM proxy (`ltp-llm-proxy`). Lets containers and the n8n twin use the same model
    as the host without holding its credentials."""

    name = "proxy"

    def __init__(self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=200.0,
            headers={"authorization": f"Bearer {token}"},
        )

    def complete(self, system: str, prompt: str, *, model: str) -> Completion:
        try:
            res = self._http.post("/v1/complete", json={"system": system, "prompt": prompt, "model": model})
        except httpx.HTTPError as e:
            raise LLMError(f"llm proxy unreachable: {e}") from e
        if res.status_code != 200:
            raise LLMError(f"llm proxy {res.status_code}: {res.text[:200]}")
        return Completion(**res.json())


class OfflineLLM:
    """Keyword rules shaped like the real prompts' output. For CI and credential-free demos only;
    the benchmark labels any run that used it."""

    name = "offline"

    def complete(self, system: str, prompt: str, *, model: str) -> Completion:
        text = json.dumps(_offline_qualify(prompt)) if "QUALIFY" in system else _offline_proposal(prompt)
        return Completion(text=text, model="offline", latency_ms=1)


_LINES = {
    "automation": ("automat", "zapier", "workflow", "retype", "manual", "integrat", "n8n"),
    "analytics": ("ga4", "analytics", "tracking", "attribution", "dashboard"),
    "paid_ads": ("google ads", "meta ads", "ppc", "shopping", "roas"),
    "web": ("website", "redesign", "landing page", "shopify"),
}


def _offline_qualify(prompt: str) -> dict[str, object]:
    text = prompt.lower()
    line = next((k for k, words in _LINES.items() if any(w in text for w in words)), "other")
    budget = re.search(r"budget_gbp: (\d+)", text)
    amount = int(budget.group(1)) if budget else None
    band = (
        "unknown"
        if amount is None
        else "under_2k"
        if amount < 2000
        else "2k_10k"
        if amount < 10000
        else "10k_50k"
        if amount < 50000
        else "over_50k"
    )
    spammy = any(w in text for w in ("seo backlinks", "crypto", "guest post", "casino"))
    if spammy:
        return {
            "fit": "reject",
            "score": 5,
            "service_line": "other",
            "reasons": ["Unsolicited sales pitch"],
            "red_flags": ["spam"],
            "budget_band": band,
        }
    score = 40 + (25 if line != "other" else 0) + (25 if amount and amount >= 2000 else 0)
    fit = "qualified" if score >= 70 else "nurture"
    return {
        "fit": fit,
        "score": score,
        "service_line": line,
        "reasons": [f"Request matches {line}", f"Budget band {band}"],
        "red_flags": [],
        "budget_band": band,
    }


def _offline_proposal(prompt: str) -> str:
    company = re.search(r"company: (.+)", prompt)
    name = company.group(1).strip() if company else "the client"
    package = re.search(r"PACKAGE: (.+?) \| (£[\d,]+)", prompt)
    title, price = (package.group(1), package.group(2)) if package else ("Discovery", "£1,500")
    return (
        f"# Proposal for {name}\n\n## What you asked for\nSummary of the request.\n\n"
        f"## Proposed approach\n{title}.\n\n## Investment\n{title}: {price} + VAT.\n\n"
        "## Next step\nA 30-minute call to confirm scope."
    )
