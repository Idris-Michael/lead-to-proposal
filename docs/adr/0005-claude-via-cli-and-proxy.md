# 5. Claude through `claude -p` and a small model proxy

Status: accepted for the pilot; production would use the Messages API

## Context
The pilot had no budget for API credit. The operator has a Claude subscription, which can be used
headless through the Claude Code CLI (`claude -p`). That CLI is logged in on the host only, but the
worker and the n8n twin run in containers.

## Decision
- `ClaudeCli` runs `claude -p` with tools, MCP servers, slash commands and user settings switched
  off, so the model sees only our prompt. Arguments are a list (no shell); the lead text travels on
  stdin, never in argv.
- `ltp-llm-proxy` exposes `POST /v1/complete` on the host behind a bearer token, with a model
  allow-list. Containers and n8n call it via `host.docker.internal`. Both twins therefore send the
  same prompts to the same models, so the benchmark compares orchestration, not models.
- Haiku 5.5 qualifies (short structured output, called for every lead); Sonnet 5.5 drafts (longer
  prose, called only for qualified leads).
- `model: "offline"` is always served by deterministic keyword rules, so CI and the failure drills
  never spend model calls.

## Consequences
- Latency includes the CLI's start-up and a fixed preamble of about 930 input tokens per call, so
  measured times are pessimistic compared with a direct API call.
- Production replaces `ClaudeCli` with an adapter for the Anthropic Messages API, Amazon Bedrock or
  Azure AI Foundry, behind the same `LLM` protocol (one method). The proxy then becomes unnecessary,
  or becomes the organisation's model gateway.
- The proxy binds to all interfaces so containers can reach it; the token is what protects it.
  On a shared network, bind it to the Docker bridge address instead.
