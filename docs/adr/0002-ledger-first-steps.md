# 2. Every step writes to the ledger before the next one starts

Status: accepted

## Context
One lead triggers up to five side effects: two model calls, a HubSpot contact, a deal and a note.
Any of them can fail halfway, and the worker can be killed between any two. Re-running the whole
lead from the top would call the model again (cost, and possibly a different verdict) and could
create a second deal.

## Decision
The lead's row in Postgres is the checkpoint. Each LangGraph node checks whether its result is
already stored (`qualification`, `hubspot_contact_id`, `hubspot_deal_id`, `hubspot_note_id`,
`proposal_md`) and only acts if it is not, then commits before returning. No LangGraph
checkpointer is used: the ledger already is one, and it is the same table the operator console reads.

Workers claim leads with `SELECT ... FOR UPDATE SKIP LOCKED` and a lease (`locked_until`). A failed
attempt extends the lease by an exponential backoff instead of setting a separate retry time, so
"claimable" has one definition: received, or processing with an expired or missing lease.

## Consequences
- A worker killed after the deal was created resumes at the note, with no second model call and no
  second deal (`test_crash_after_deal_resumes_without_duplicates`).
- Forty leads across eight parallel workers on real Postgres: forty claims, no lead claimed twice
  (`test_parallel_workers_never_claim_the_same_lead`, testcontainers).
- There is still a gap between a HubSpot write succeeding and its id being committed. For deals it
  is closed on HubSpot's side (ADR 3). For contacts the upsert is naturally idempotent. A note can
  be duplicated in that gap; that was accepted as low harm.
