# Runbook

## Run it locally
```bash
cp .env.example .env            # then set the three secrets
docker compose up -d --build    # Postgres, migrations, API, worker, Jaeger
open http://localhost:8080      # operator console (token = OPERATOR_TOKEN)
uv run python scripts/send_lead.py bench/leads.json --index 0
```
With `LLM_MODE=offline` and `HUBSPOT_MODE=mock` (the defaults in `.env.example`) nothing leaves the
machine. For real Claude: run `uv run ltp-llm-proxy` on the host and set `LLM_MODE=proxy`.

## Configuration
| Variable | Meaning |
|---|---|
| `WEBHOOK_SECRET` | HMAC secret shared with the website backend |
| `OPERATOR_TOKEN` | Bearer token for the console and operator API (24+ characters) |
| `LLM_MODE` | `offline`, `proxy` or `claude_cli` |
| `QUALIFY_MODEL`, `DRAFT_MODEL` | Model ids sent to the proxy / CLI |
| `HUBSPOT_MODE` | `mock` (in-process fake) or `live` |
| `HUBSPOT_TOKEN`, `HUBSPOT_BASE_URL` | Private app token; base URL (only changed for the fake server) |
| `APPROVAL_MODE` | `local` (console) or `gateway` (agent approval gateway) |
| `GATEWAY_URL`, `GATEWAY_AGENT_KEY`, `GATEWAY_CALLBACK_SECRET` | Gateway mode only |
| `RATE_LIMIT_PER_MINUTE` | Webhook requests per client per minute |
| `WORKER_MAX_ATTEMPTS`, `WORKER_LEASE_S` | Retry ceiling; how long a claimed lead is held |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where traces go |

## Connecting a HubSpot developer test account
1. In a HubSpot developer account, create a test account, then a private app with the scopes in
   the security questionnaire.
2. Set `HUBSPOT_MODE=live` and `HUBSPOT_TOKEN`. On start the worker creates the custom properties
   `ltp_lead_key` (deals, unique), `ltp_score` and `ltp_fit` (contacts).

## Day-to-day
| Symptom | Check | Fix |
|---|---|---|
| Leads stay `received` | Is a worker running? `docker compose ps` | Start or scale workers |
| Lead `failed` | Console shows the error and attempts | Fix the cause, then **Retry** in the console |
| Many `hubspot_retry` log lines | Burst limit; normal under load | Fewer workers if it is constant |
| `daily API limit reached` | HubSpot daily quota | Leads retry later; raise the quota or slow intake |
| Webhook 401s | Clock skew over 5 minutes, or wrong secret | Fix NTP or the secret |
| Model errors (`claude-cli error`) | Proxy log; subscription usage limit | Wait, or switch the adapter |

## Gateway mode
Run the [agent approval gateway](https://github.com/Idris-Michael/agent-approval-gateway) with a
`send_proposal` tool that requires approval, and an executor that posts the signed callback to
`/v1/approvals/callback`. Drafts are then approved in Telegram or the gateway console; rejections and
expiries are picked up by the worker.

## Benchmark
See `bench/run.py`'s docstring. The bench stack is
`docker compose -f docker-compose.yml -f bench/compose.bench.yml --profile bench --profile n8n up -d --scale worker=4`.
