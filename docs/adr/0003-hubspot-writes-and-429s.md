# 3. Idempotent HubSpot writes and one retry policy for 429s

Status: accepted

## Context
HubSpot private apps are rate limited per ten-second window and per day. A morning burst of form
submissions processed by several workers will hit the burst limit. Retries make duplicates likely
unless every write is safe to repeat.

## Decision
- **Contacts:** `PATCH /crm/v3/objects/contacts/{email}?idProperty=email`; on 404, `POST`; on a
  409 from that `POST` (another worker created it first), `PATCH` once more. Email is the natural key.
- **Deals:** a custom deal property `ltp_lead_key` with `hasUniqueValue: true`, set to the lead's
  ledger id. Before creating, search by the key; if a racing create still gets in first, HubSpot
  refuses ours with 409 and we search again. HubSpot search is eventually consistent, so the unique
  property, not the search, is what guarantees one deal per lead.
- **Retries:** one loop for every call. `Retry-After` when HubSpot sends it; otherwise capped
  exponential backoff (0.5 s doubling to 12 s) with full jitter, so parallel workers spread out
  instead of retrying in step. Up to six attempts. 5xx and connection errors are retried the same
  way. A 429 whose `policyName` is `DAILY` is not retried: the quota resets at midnight.
- **Counters:** the client counts what it absorbed (429s, 5xx, time slept), and the benchmark
  reports it.

## Consequences
- In the drills (fake HubSpot over HTTP, 30% and 60% of calls answered 429) every lead completed,
  with zero duplicate contacts or deals. Figures are in `bench/results.json`.
- Six attempts at a 60% fault rate still fail about 5% of calls. The worker's own retry (ADR 2)
  sits above this loop, so a lead that exhausts it is retried later rather than lost.
- The custom properties have to exist: the worker creates them on start (`ensure_properties`), which
  needs the `crm.schemas.*.write` scopes on the private app.
