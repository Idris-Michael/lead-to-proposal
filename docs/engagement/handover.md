# Handover

## What was delivered
- The Python service: webhook API, worker, operator console, Postgres ledger with migrations.
- The n8n twin, generated from the same prompts, and the comparison in ADR 1.
- A benchmark harness with 20 labelled leads, duplicate and rate-limit drills, and their results.
- Terraform for Azure Container Apps and AWS ECS Fargate, validated offline, not deployed.
- CI: lint, types, tests with a coverage gate, Postgres integration tests, secret scanning, SAST,
  SBOM, Terraform checks.

## What is not done
| Item | Why it matters | Suggested next step |
|---|---|---|
| Live HubSpot run | Pilot used a faithful fake over HTTP; the client code targets the real v3 API | Connect a developer test account (runbook) and re-run the benchmark |
| Production model adapter | `claude -p` is a pilot workaround (ADR 5) | Add a Messages API / Bedrock / Azure adapter behind the `LLM` protocol |
| SSO on the console | One shared operator token | Put the console behind Entra ID / Okta at the ingress |
| Retention and deletion | Leads are kept indefinitely | Scheduled prune; a delete-by-email endpoint for data subject requests |
| Sending the email | Approval marks the lead approved; nothing is emailed | A sender behind the approval step, ideally the gateway's executor |
| Shared rate limiting | In-process per replica | Enforce at the ingress |

## How to change the prompts
Edit `QUALIFY_SYSTEM` / `DRAFT_SYSTEM` / `RATE_CARD` in `src/lead_to_proposal/agent.py`, run
`uv run python n8n/build_workflow.py`, re-import the workflow if you use n8n, then re-run
`bench/run.py claude` for both twins and compare `fit_agreement` with the previous results.

## Known behaviours
- Haiku put the printing enquiry (L20) in `nurture` rather than `reject` in the recorded run of both twins. It does
  no harm: no deal is created and a person sees it.
- The rate-card check allows any figure the client mentioned. A draft could repeat a client's
  figure in a misleading way; review catches that, the check does not.
- Verdicts vary between runs at the margins. In one demo take Haiku put the prompt-injection lead
  (L19) in `nurture` with score 25 rather than `reject`, while noting the injected instruction and
  ignoring it. No deal was created, but a contact and note were. Re-run the labelled set after any
  prompt change and look at the spread, not one run.
