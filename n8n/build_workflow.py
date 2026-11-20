"""Generates n8n/lead-to-proposal.json, the low-code twin of the Python service.

Prompts, rate card and the budget rule are pulled from `lead_to_proposal.agent`, so both twins
send the model exactly the same instructions; a test fails if the committed JSON drifts.

    uv run python n8n/build_workflow.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from lead_to_proposal.agent import DRAFT_SYSTEM, MIN_QUALIFIED_BUDGET_GBP, QUALIFY_SYSTEM, RATE_CARD

OUT = Path(__file__).with_name("lead-to-proposal.json")
RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 1000}  # n8n's built-in retry: fixed wait
HS_AUTH = "={{ 'Bearer ' + $env.HUBSPOT_TOKEN }}"

VERIFY_JS = r"""
// Same contract as the Python API: HMAC-SHA256 over `timestamp.rawBody`, 5-minute window.
const crypto = require('crypto');
const out = [];
for (let i = 0; i < $input.all().length; i++) {
  const item = $input.all()[i];
  const h = item.json.headers || {};
  const raw = await this.helpers.getBinaryDataBuffer(i, 'data');
  const ts = parseInt(h['x-timestamp'] || '', 10);
  const expected = 'v1=' + crypto.createHmac('sha256', $env.WEBHOOK_SECRET)
    .update(Buffer.concat([Buffer.from(`${ts}.`), raw])).digest('hex');
  const given = h['x-signature'] || '';
  const fresh = Number.isFinite(ts) && Math.abs(Date.now() / 1000 - ts) <= 300;
  const sigOk = fresh && given.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected));
  if (!sigOk) { out.push({ json: { ok: false, status: 401, error: 'invalid signature' } }); continue; }
  const b = item.json.body || {};
  const email = String(b.email || '').trim().toLowerCase();
  const message = String(b.message || '').trim();
  const valid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) && b.name && b.company &&
    message.length >= 10 && message.length <= 4000;
  if (!valid) { out.push({ json: { ok: false, status: 422, error: 'invalid lead' } }); continue; }
  const clean = (s) => String(s).replace(/</g, '(').replace(/>/g, ')');
  const lead = { name: b.name, email, company: b.company, website: b.website || null,
                 message, budget_gbp: b.budget_gbp ?? null };
  const dedupe_key = crypto.createHash('sha256')
    .update(`${email}\n${message.toLowerCase().split(/\s+/).join(' ')}`).digest('hex').slice(0, 32);
  const lead_block = `<lead>\nname: ${clean(lead.name)}\ncompany: ${clean(lead.company)}\n` +
    `website: ${lead.website || 'not given'}\nbudget_gbp: ${lead.budget_gbp ?? 'not given'}\n` +
    `message: ${clean(message)}\n</lead>`;
  out.push({ json: { ok: true, status: 202, lead, dedupe_key, lead_block, received_at: Date.now() } });
}
return out;
"""

PARSE_JS = (
    r"""
// Parse and validate the model's JSON, then apply the code-side budget rule.
const ctx = $('Verify and validate').item.json;
const text = $json.text || '';
const m = text.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/) || [null, text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1)];
const q = JSON.parse(m[1]);
const FITS = ['qualified', 'nurture', 'reject'];
const LINES = ['automation', 'analytics', 'paid_ads', 'web', 'other'];
if (!FITS.includes(q.fit) || !LINES.includes(q.service_line) || !(q.score >= 0 && q.score <= 100)) {
  throw new Error('model returned an invalid verdict: ' + text.slice(0, 200));
}
const MIN = __MIN__;
if (q.fit === 'qualified' && ctx.lead.budget_gbp != null && ctx.lead.budget_gbp < MIN) {
  q.fit = 'nurture'; q.score = Math.min(q.score, 60);
}
if (q.fit === 'qualified' && (q.red_flags || []).length && q.score < 70) q.fit = 'nurture';
return { json: { ...ctx, q } };
"""
).replace("__MIN__", str(MIN_QUALIFIED_BUDGET_GBP))

CONTACT_JS = r"""
return { json: { ...$('Parse verdict and policy').item.json, contact_id: String($json.id) } };
"""

NOTE_JS = r"""
const ctx = $('Contact id').item.json;
let deal_id = null;
if (ctx.q.fit === 'qualified') {
  deal_id = String($json.id || ($json.results && $json.results[0] && $json.results[0].id));
}
const q = ctx.q;
const body = `Lead triage: ${q.fit} (${q.score}/100), ${q.service_line}.\nReasons: ${(q.reasons || []).join('; ')}` +
  ((q.red_flags || []).length ? `\nRed flags: ${q.red_flags.join('; ')}` : '');
const assoc = [{ to: { id: ctx.contact_id }, types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }] }];
if (deal_id) assoc.push({ to: { id: deal_id }, types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 214 }] });
return { json: { ...ctx, deal_id, note: { properties: { hs_note_body: body, hs_timestamp: new Date().toISOString() }, associations: assoc } } };
"""

DRAFT_PROMPT_JS = (
    (
        r"""
const ctx = $('Build note').item.json;
const CARD = __CARD__;
const [title, price] = CARD[ctx.q.service_line];
const fmt = '£' + price.toLocaleString('en-GB');
const prompt = `${ctx.lead_block}\n<qualification>\n${JSON.stringify(ctx.q)}\n</qualification>\n` +
  `<rate_card>\nPACKAGE: ${title} | ${fmt} + VAT\n</rate_card>`;
return { json: { ...ctx, price, draft_request: { system: __DRAFT__, prompt, model: 'claude-sonnet-5-5' } } };
"""
    )
    .replace("__CARD__", json.dumps({k: list(v) for k, v in RATE_CARD.items()}))
    .replace("__DRAFT__", json.dumps(DRAFT_SYSTEM))
)

PRICE_JS = r"""
const ctx = $('Draft prompt').item.json;
const text = ($json.text || '').trim();
const figures = [...text.matchAll(/£\s?(\d[\d,]*)/g)].map((m) => parseInt(m[1].replace(/,/g, ''), 10));
const issues = figures.filter((n) => n !== ctx.price).map((n) => `£${n} is not on the rate card`);
const flag = issues.length ? `PRICE CHECK: ${issues.join('; ')}\n\n` : '';
return { json: { ...ctx, issues, drafted_ms: Date.now() - ctx.received_at,
  note: { properties: { hs_note_body: flag + 'PROPOSAL DRAFT (needs review)\n\n' + text, hs_timestamp: new Date().toISOString() },
          associations: [{ to: { id: ctx.deal_id }, types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 214 }] }] } } };
"""


def code(name: str, js: str, pos: tuple[int, int], *, each: bool = True) -> dict[str, Any]:
    params: dict[str, Any] = {"jsCode": js.strip()}
    if each:
        params["mode"] = "runOnceForEachItem"
    return {
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": list(pos),
        "parameters": params,
    }


def http(
    name: str,
    method: str,
    url: str,
    body: str | None,
    pos: tuple[int, int],
    *,
    auth: str | None,
    on_error: bool = False,
) -> dict[str, Any]:
    headers = [{"name": "authorization", "value": auth}] if auth else []
    params: dict[str, Any] = {
        "method": method,
        "url": url,
        "sendHeaders": bool(headers),
        "headerParameters": {"parameters": headers},
        "options": {"timeout": 200000},
    }
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    node: dict[str, Any] = {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": list(pos),
        "parameters": params,
        **RETRY,
    }
    if on_error:
        node["onError"] = "continueErrorOutput"
    return node


def condition(name: str, left: str, right: str, pos: tuple[int, int]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": list(pos),
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                "combinator": "and",
                "conditions": [
                    {
                        "id": name.lower().replace(" ", "-"),
                        "leftValue": left,
                        "rightValue": right,
                        "operator": {"type": "string", "operation": "equals"},
                    }
                ],
            },
            "options": {},
        },
    }


def proxy_body(system: str) -> str:
    return (
        "={{ JSON.stringify({ system: "
        + json.dumps(system)
        + ", prompt: $json.lead_block, model: 'claude-haiku-5-5' }) }}"
    )


def build() -> dict[str, Any]:
    hs = "={{ $env.HUBSPOT_BASE_URL }}"
    nodes = [
        {
            "name": "Lead webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 300],
            "webhookId": "5b0c7a52-lead-to-proposal",
            "parameters": {
                "httpMethod": "POST",
                "path": "lead",
                "responseMode": "responseNode",
                "options": {"rawBody": True},
            },
        },
        code("Verify and validate", VERIFY_JS, (220, 300), each=False),
        {
            "name": "Respond",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [440, 300],
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ accepted: $json.ok, error: $json.error || null }) }}",
                "options": {"responseCode": "={{ $json.status }}"},
            },
        },
        condition("Valid?", "={{ String($json.ok) }}", "true", (660, 300)),
        {
            "name": "Seen before?",
            "type": "n8n-nodes-base.removeDuplicates",
            "typeVersion": 2,
            "position": [880, 280],
            "parameters": {
                "operation": "removeItemsSeenInPreviousExecutions",
                "dedupeValue": "={{ $json.dedupe_key }}",
                "options": {},
            },
        },
        http(
            "Qualify (LLM proxy)",
            "POST",
            "={{ $env.LLM_PROXY_URL }}/v1/complete",
            proxy_body(QUALIFY_SYSTEM),
            (1100, 280),
            auth="={{ 'Bearer ' + $env.LLM_PROXY_TOKEN }}",
        ),
        code("Parse verdict and policy", PARSE_JS, (1320, 280)),
        condition("Reject?", "={{ $json.q.fit }}", "reject", (1540, 280)),
        http(
            "HubSpot: update contact",
            "PATCH",
            hs + "/crm/v3/objects/contacts/{{ encodeURIComponent($json.lead.email) }}?idProperty=email",
            "={{ JSON.stringify({ properties: { email: $json.lead.email, firstname: $json.lead.name.split(' ')[0],"
            " lastname: $json.lead.name.split(' ').slice(1).join(' '), company: $json.lead.company,"
            " ltp_score: String($json.q.score), ltp_fit: $json.q.fit } }) }}",
            (1760, 360),
            auth=HS_AUTH,
            on_error=True,
        ),
        http(
            "HubSpot: create contact",
            "POST",
            hs + "/crm/v3/objects/contacts",
            "={{ JSON.stringify({ properties: { email: $('Parse verdict and policy').item.json.lead.email,"
            " company: $('Parse verdict and policy').item.json.lead.company,"
            " ltp_score: String($('Parse verdict and policy').item.json.q.score),"
            " ltp_fit: $('Parse verdict and policy').item.json.q.fit } }) }}",
            (1980, 460),
            auth=HS_AUTH,
        ),
        code("Contact id", CONTACT_JS, (2200, 360)),
        condition("Qualified?", "={{ $json.q.fit }}", "qualified", (2420, 360)),
        http(
            "HubSpot: create deal",
            "POST",
            hs + "/crm/v3/objects/deals",
            "={{ JSON.stringify({ properties: { dealname: $json.lead.company + ' - ' + $json.q.service_line,"
            " pipeline: 'default', dealstage: 'appointmentscheduled', ltp_lead_key: $json.dedupe_key },"
            " associations: [{ to: { id: $json.contact_id }, types: [{ associationCategory: 'HUBSPOT_DEFINED',"
            " associationTypeId: 3 }] }] }) }}",
            (2640, 300),
            auth=HS_AUTH,
            on_error=True,
        ),
        http(
            "HubSpot: find deal",
            "POST",
            hs + "/crm/v3/objects/deals/search",
            "={{ JSON.stringify({ filterGroups: [{ filters: [{ propertyName: 'ltp_lead_key', operator: 'EQ',"
            " value: $('Contact id').item.json.dedupe_key }] }], limit: 1 }) }}",
            (2860, 400),
            auth=HS_AUTH,
        ),
        code("Build note", NOTE_JS, (3080, 360)),
        http(
            "HubSpot: triage note",
            "POST",
            hs + "/crm/v3/objects/notes",
            "={{ JSON.stringify($json.note) }}",
            (3300, 360),
            auth=HS_AUTH,
        ),
        condition("Needs proposal?", "={{ $('Build note').item.json.q.fit }}", "qualified", (3520, 360)),
        code("Draft prompt", DRAFT_PROMPT_JS, (3740, 300)),
        http(
            "Draft (LLM proxy)",
            "POST",
            "={{ $env.LLM_PROXY_URL }}/v1/complete",
            "={{ JSON.stringify($json.draft_request) }}",
            (3960, 300),
            auth="={{ 'Bearer ' + $env.LLM_PROXY_TOKEN }}",
        ),
        code("Price check", PRICE_JS, (4180, 300)),
        http(
            "HubSpot: draft note",
            "POST",
            hs + "/crm/v3/objects/notes",
            "={{ JSON.stringify($json.note) }}",
            (4400, 300),
            auth=HS_AUTH,
        ),
    ]

    def to(node: str) -> list[dict[str, Any]]:
        return [{"node": node, "type": "main", "index": 0}]

    connections = {
        "Lead webhook": {"main": [to("Verify and validate")]},
        "Verify and validate": {"main": [to("Respond")]},
        "Respond": {"main": [to("Valid?")]},
        "Valid?": {"main": [to("Seen before?"), []]},
        "Seen before?": {"main": [to("Qualify (LLM proxy)")]},
        "Qualify (LLM proxy)": {"main": [to("Parse verdict and policy")]},
        "Parse verdict and policy": {"main": [to("Reject?")]},
        "Reject?": {"main": [[], to("HubSpot: update contact")]},
        # error output (404 not found, or retries exhausted) falls through to create
        "HubSpot: update contact": {"main": [to("Contact id"), to("HubSpot: create contact")]},
        "HubSpot: create contact": {"main": [to("Contact id")]},
        "Contact id": {"main": [to("Qualified?")]},
        "Qualified?": {"main": [to("HubSpot: create deal"), to("Build note")]},
        # a 409 (deal already carries this lead key) falls through to search
        "HubSpot: create deal": {"main": [to("Build note"), to("HubSpot: find deal")]},
        "HubSpot: find deal": {"main": [to("Build note")]},
        "Build note": {"main": [to("HubSpot: triage note")]},
        "HubSpot: triage note": {"main": [to("Needs proposal?")]},
        "Needs proposal?": {"main": [to("Draft prompt"), []]},
        "Draft prompt": {"main": [to("Draft (LLM proxy)")]},
        "Draft (LLM proxy)": {"main": [to("Price check")]},
        "Price check": {"main": [to("HubSpot: draft note")]},
    }
    for n in nodes:  # stable ids, so a rebuild produces an identical file
        n["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lead-to-proposal/{n['name']}"))
    return {
        "id": "LtpN8nTwin000001",
        "name": "lead-to-proposal (n8n twin)",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "saveDataSuccessExecution": "all"},
        "active": False,
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), "utf-8")
    print(f"wrote {OUT}")
