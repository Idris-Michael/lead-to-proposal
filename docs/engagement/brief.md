# Engagement brief

> **The customer is fictional.** "The agency" is an invented 15-person UK digital agency used to
> frame the work the way a forward-deployed engagement would be framed. The code, tests and
> benchmark results are real; the leads are synthetic.

## The problem
The agency gets 30–60 inbound enquiries a week through its website form. A sales lead reads each
one, decides whether it is worth pursuing, creates the contact and deal in HubSpot, writes a triage
note and, for good fits, drafts a first proposal email. It happens in gaps between other work, so
good leads wait a day or more and spam gets the same attention as a £10k project.

They already tried a no-code flow built by a contractor. It worked in the demo and then created
duplicate deals and quietly dropped leads when HubSpot rate-limited it during a busy week.

## Stakeholders
| Role | Cares about |
|---|---|
| Managing director (sponsor) | Good leads answered the same day; no more lost enquiries |
| Sales lead (reviewer) | A sensible verdict and a draft they can send after light edits |
| Operations | One contact and one deal per lead in HubSpot, every time |
| Information security | Lead data handled carefully; nothing goes out without a person approving it |
| The person maintaining it afterwards | Something they can run, read and change |

## Success criteria (agreed up front)
1. Every lead submitted is either processed or visibly failed. None are silently lost.
2. A repeated or double-clicked submission never creates a second contact or deal.
3. HubSpot rate limiting slows the pipeline down; it does not lose leads or duplicate records.
4. Verdicts agree with the sales lead's own judgement on a labelled test set.
5. A draft quotes only rate-card prices, and nothing is sent without a person approving it.
6. The website form gets an immediate answer, however busy the pipeline is.

## Constraints
- HubSpot is the system of record. A free developer test account for the pilot.
- No budget for model API credit during the pilot; the agency's Claude subscription is available.
- The previous attempt was n8n; the agency wants to know honestly whether to go back to it.
- Pilot only: no live sending of emails, no production HubSpot portal.

## Approach
Built twice from the same prompts: a Python service (FastAPI, Postgres ledger, LangGraph worker)
and the equivalent n8n workflow. Both were run against the same fake HubSpot server, the same model
proxy and the same 20 hand-labelled leads, then through duplicate and rate-limit drills.
[ADR 1](../adr/0001-low-code-vs-code.md) records the comparison and the recommendation.

## Outcome (pilot)
See the [README results](../../README.md#results) and `bench/results.json` for the measured figures.
