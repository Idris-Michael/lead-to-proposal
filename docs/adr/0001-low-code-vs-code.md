# 1. Python service over n8n for this workload, measured

Status: accepted

## Context
The agency's previous attempt was an n8n flow, and they asked whether to go back to it. Opinion
was not enough, so both were built from the same prompts (the n8n JSON is generated from
`agent.py`) and run under the same conditions: the same fake HubSpot server, the same model proxy
and models, the same 20 labelled leads, four-way parallelism on both sides.

## What was measured
Single recorded runs, all in `bench/results.json`. Model runs: Haiku 5.5 qualifies, Sonnet 5.5
drafts, through `claude -p`. Drills use the offline model so only the orchestration differs.

| | Python service | n8n twin |
|---|---|---|
| Form submitter waits for a response (20 at once) | median 0.21 s, max 0.22 s | median 34.1 s, max 50.1 s |
| Webhook to proposal draft, median / p90 | 26.0 s / 42.1 s | 31.4 s / 49.2 s |
| Verdicts agreeing with hand labels | 19/20 | 19/20 |
| Duplicate drill: 20 leads, all re-sent, 5 double-clicks | 25/25 complete, 0 duplicates, 97 HubSpot calls | 25/25 complete, 0 duplicates, 175 HubSpot calls |
| 30% of HubSpot calls answered 429 | 20/20 complete | 19/20 complete |
| 60% of HubSpot calls answered 429 | 20/20 complete | 8/20 complete |
| Can tell "rejected" from "failed" without opening the tool | yes (ledger status) | no |

Earlier runs of the same drills on earlier builds gave n8n 20/20 and 18/20 at 30%, and 9/20 at
60%. Python completed every lead in those runs too. The 429s are injected at random, so treat the
n8n figures as a range rather than a constant.

## Why the numbers differ
- **Response time:** with production concurrency capped at 4, n8n queues whole executions; a
  queued execution has not reached its Respond node, so the submitter waits behind the model calls.
  Uncapped, it would answer quickly but start 20 model calls at once.
- **HubSpot calls:** n8n's Retry On Fail treats the expected 404 from the contact lookup as a
  failure and retries it twice with a fixed 1 s wait before taking the error branch.
- **429s:** n8n retries three times with a fixed wait and no `Retry-After` or jitter. The Python
  client honours `Retry-After`, backs off with jitter for up to six attempts, and the worker retries
  the whole lead later from where it stopped (ADR 2, 3). n8n's failed leads exist only in its
  execution list.
- **Duplicates:** both held. n8n's Remove Duplicates node and the unique deal property did the work.

## Decision
Run the Python service. Keep n8n where it is strong: low-volume internal automations, and
notifications or integrations triggered by this service's events.

## Consequences
- More code to own than a canvas. Offset by tests (72, ~97% coverage), Postgres integration tests
  and the runbook.
- The n8n twin stays in the repo, generated from the same prompts and checked for drift in CI, so
  the comparison can be re-run when either side changes.
- An n8n setup in queue mode with Redis and workers, or a two-workflow design that responds
  immediately and hands off, would close the response-time gap. It would not by itself change the
  retry behaviour or give a per-lead ledger.
