"""A small model gateway: `POST /v1/complete` → the configured LLM. Runs on the host so that
containers (the worker, the n8n twin) can use `claude -p` without holding the host's login.
Bearer-token protected; only the models in ALLOWED_MODELS can be requested."""

import hmac
import os
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .llm import LLM, ClaudeCli, LLMError, OfflineLLM

ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "offline"}


class CompleteIn(BaseModel):
    system: str = Field(max_length=20_000)
    prompt: str = Field(max_length=40_000)
    model: str


def create_app(llm: LLM, token: str) -> FastAPI:
    if len(token) < 24:
        raise ValueError("LLM_PROXY_TOKEN must be at least 24 characters")
    app = FastAPI(title="ltp-llm-proxy", version="0.1.0")

    @app.post("/v1/complete")
    def complete(
        body: CompleteIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, object]:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "token required")
        if body.model not in ALLOWED_MODELS:
            raise HTTPException(400, f"model not allowed: {body.model}")
        try:
            c = llm.complete(body.system, body.prompt, model=body.model)
        except LLMError as e:
            raise HTTPException(502, str(e)) from e
        return {
            "text": c.text,
            "model": c.model,
            "latency_ms": c.latency_ms,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
        }

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    llm: LLM = (
        OfflineLLM()
        if os.environ.get("LLM_MODE") == "offline"
        else ClaudeCli(os.environ.get("CLAUDE_CLI_PATH"))
    )
    app = create_app(llm, os.environ.get("LLM_PROXY_TOKEN", ""))
    # 0.0.0.0 so Docker containers can reach it via host.docker.internal; the token guards it.
    uvicorn.run(app, host=os.environ.get("LLM_PROXY_HOST", "0.0.0.0"), port=8090)  # noqa: S104
