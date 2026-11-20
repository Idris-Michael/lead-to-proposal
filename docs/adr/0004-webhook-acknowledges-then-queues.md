# 4. The webhook acknowledges in milliseconds and queues the work

Status: accepted

## Context
The website's form backend calls the webhook. It should get an answer quickly whatever the
model or HubSpot are doing, and a repeated submission should not create a second lead.

## Decision
`POST /v1/leads` verifies an HMAC-SHA256 signature over `timestamp.body` (five-minute window, so a
captured request cannot be replayed later), validates the body against a strict schema (unknown
fields are rejected so a changed form fails loudly), inserts the lead and returns `202`. All model
and CRM work happens in the worker.

A resubmission is recognised by an `Idempotency-Key` header if the caller sends one, otherwise by a
hash of the email and the normalised message. Unique constraints decide, so two identical requests
arriving together still produce one row; the second gets `200` with `duplicate: true` and the
existing id.

The database insert runs in a threadpool. The first benchmark burst showed why: synchronous
SQLAlchemy calls inside an `async` handler block the event loop and serialise every request.

Rate limiting is a sliding one-minute window per client address, in process.

## Consequences
- In the benchmark burst of 20 simultaneous submissions the form got its response in about 0.2 s,
  independent of model latency. The n8n twin, which only responds once an execution reaches its
  Respond node, made submitters wait while executions queued (see ADR 1).
- The in-process rate limit is per replica. Behind a load balancer with several replicas, enforce
  it at the ingress (Azure Front Door / AWS WAF rate rules) or in a shared store.
- The sender must hold the webhook secret. A form posting straight from the browser cannot sign; it
  has to go through the site's backend.
