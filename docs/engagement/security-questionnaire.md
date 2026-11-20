# Security questionnaire answers

The questions a customer's information security team typically sends before a pilot, answered for
this system as built. Where something is not built, the answer says so.

## Architecture and data flow

**What does the system do?**
It receives website enquiries, asks a model to classify each one, records the contact (and, for
good fits, a deal and note) in HubSpot, drafts a proposal for qualified leads and holds each draft
until a person approves it.

**Where does data go?**
1. Website backend → API (HTTPS in deployment): name, email, company, website, message, budget.
2. Worker → model: the same fields inside a prompt. In the pilot, through `claude -p` on the
   operator's machine to Anthropic. In production, the Anthropic API, Amazon Bedrock or Azure AI
   Foundry under the customer's own agreement (ADR 5).
3. Worker → HubSpot (HTTPS): contact fields, lead score and fit, deal, a triage note.
4. Worker → approval gateway (optional): lead id, company, service line, score. Not the message.

**Does any data leave our environment?**
Yes: to the model provider and to HubSpot. Both are named sub-processors the agency already uses.
No other third party receives lead data. The fake HubSpot and offline model let the whole system run
with no external calls at all, which is how CI runs.

**What is stored, where, and for how long?**
| Store | Contents | Retention |
|---|---|---|
| Postgres `leads` | The submission, verdict, HubSpot ids, draft, status | Not pruned; set a policy (suggest 12 months, matching CRM retention) |
| Postgres `lead_events` | Timeline: step names, model ids, token counts, errors | Same as above |
| Logs (stdout, JSON) | Lead ids, step outcomes, retry counts and short error summaries (an error can quote up to 200 characters of a HubSpot or model reply). The lead message itself is not logged | Platform default |
| Traces (OTLP → Jaeger in the pilot) | Span names and lead ids | Platform default |

## Identity and access

**How do callers authenticate?**
- Webhook: HMAC-SHA256 signature over `timestamp.body` with a shared secret; five-minute window
  against replay. Unsigned or stale requests get 401.
- Operator API and console: bearer token, compared in constant time. In the pilot one shared token;
  production should sit behind the customer's SSO (Entra ID / Okta) at the ingress.
- Approval callback from the gateway: separate HMAC secret.
- Model proxy: bearer token and a model allow-list.

**Least privilege for HubSpot?**
A private app with `crm.objects.contacts.write`, `crm.objects.deals.write`, notes write, and
`crm.schemas.contacts.write` / `crm.schemas.deals.write` (only to create the three custom
properties on first start). No delete scopes, no access to other objects.

## Secrets
All secrets come from the environment and have no defaults; the service refuses to start without
them. Deployment reads them from Azure Key Vault or AWS Secrets Manager (see `infra/`). Nothing
secret is in the repository; CI runs gitleaks on every push.

## AI-specific risks (OWASP Top 10 for LLM Applications, 2025)
| Risk | Control |
|---|---|
| LLM01 Prompt injection | Lead text fenced as data with its brackets neutralised; model has no tools; output validated; code rules can only downgrade a verdict; every draft is approved by a person. Tested with an injection lead in the benchmark set. |
| LLM02 Sensitive information disclosure | The model only sees the lead's own submission. Logs carry ids and short error summaries, not the message. |
| LLM05 Improper output handling | Verdict parsed into a strict schema; draft scanned for off-card prices; console renders all lead and model text with `textContent`, never as HTML. |
| LLM06 Excessive agency | The model cannot call anything. Code decides what is written to HubSpot; sending needs a person. |
| LLM09 Misinformation | Prices restricted to the rate card; drafts are reviewed. |
| LLM10 Unbounded consumption | Prompt sizes capped by schema limits; proxy allow-lists models; webhook is rate limited. |

## Logging, monitoring, incident response
- JSON logs with trace ids; OpenTelemetry traces for the API and worker.
- Failed leads are visible in the console with the error and attempt count, and can be retried.
- Runbook: [runbook.md](runbook.md).

## Not built in the pilot
SSO for the console, per-user audit of reviewer decisions beyond the event log, data-subject
deletion tooling, retention jobs, a WAF. Each is listed in the [handover](handover.md).
