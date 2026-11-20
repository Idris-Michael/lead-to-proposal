"""Benchmark: the same 20 leads through both twins, plus duplicate and HTTP 429 drills.

Needs the bench stack (see bench/compose.bench.yml) and the host LLM proxy (`uv run ltp-llm-proxy`).

    uv run python bench/run.py claude  --twin python|n8n   # real model, timing + accuracy
    uv run python bench/run.py dupes   --twin python|n8n   # offline model, resend + concurrent doubles
    uv run python bench/run.py ratelimit --twin python|n8n # offline model, 30% of HubSpot calls get 429
    uv run python bench/run.py report                      # bench/results.json + bench/RESULTS.md

Each run tags lead emails with a fresh suffix, so no twin can reuse dedupe history from an earlier run.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from send_lead import send

from lead_to_proposal import agent
from lead_to_proposal.llm import OfflineLLM
from lead_to_proposal.models import LeadIn

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FAKE = "http://localhost:8091"
URLS = {
    "python": f"http://localhost:{os.environ.get('API_PORT', '8080')}/v1/leads",
    "n8n": "http://localhost:5678/webhook/lead",
}


def load_leads(tag: str) -> list[dict[str, Any]]:
    rows = json.loads((HERE / "leads.json").read_text("utf-8"))
    for r in rows:
        local, domain = r["lead"]["email"].split("@")
        r["lead"] = {**r["lead"], "email": f"{local}+{tag}@{domain}"}
        r["key"] = LeadIn(**r["lead"]).dedupe_key()
    return rows


def vm_offset() -> float:
    """Host clock minus the Docker VM clock, so host send times compare with container timestamps."""
    t0 = time.time()
    vm = httpx.get(f"{FAKE}/_fake/time").json()["now"]
    return (t0 + time.time()) / 2 - vm


def reset_fake(ratio: float = 0.0) -> None:
    httpx.post(f"{FAKE}/_fake/reset", json={"rate_limit_ratio": ratio}).raise_for_status()


ACKS: dict[str, float] = {}  # seconds until the form got its HTTP response, per lead


def burst(twin: str, rows: list[dict[str, Any]], secret: str) -> dict[str, float]:
    """Sends every lead at once, like a busy morning's form submissions. Returns host send times."""
    sent: dict[str, float] = {}

    def one(r: dict[str, Any]) -> None:
        sent[r["id"]] = time.time()
        res = send(URLS[twin], secret, r["lead"], timeout=600)
        ACKS[r["id"]] = time.time() - sent[r["id"]]
        if res.status_code >= 400:
            raise RuntimeError(f"{r['id']}: {res.status_code} {res.text[:200]}")

    with ThreadPoolExecutor(max_workers=len(rows)) as pool:
        list(pool.map(one, rows))
    return sent


def op_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {os.environ['OPERATOR_TOKEN']}"}


def python_leads() -> list[dict[str, Any]]:
    base = URLS["python"]
    rows = httpx.get(base, headers=op_headers(), params={"limit": 500}).json()
    return [httpx.get(f"{base}/{r['id']}", headers=op_headers()).json() for r in rows]


def wait_python(tag: str, n: int, timeout_s: float) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rows = [
            r
            for r in httpx.get(URLS["python"], headers=op_headers(), params={"limit": 500}).json()
            if tag in r["email"]
        ]
        if len(rows) >= n and all(r["status"] not in ("received", "processing") for r in rows):
            return [d for d in python_leads() if tag in d["email"]]
        time.sleep(2)
    raise TimeoutError("python twin did not finish in time")


def wait_quiet(quiet_s: float, timeout_s: float) -> dict[str, Any]:
    """n8n keeps no ledger, so 'finished' means the fake HubSpot saw no calls for `quiet_s`."""
    deadline, last, since = time.time() + timeout_s, -1, time.time()
    while time.time() < deadline:
        state = httpx.get(f"{FAKE}/_fake/state").json()
        if state["calls"] != last:
            last, since = state["calls"], time.time()
        elif time.time() - since >= quiet_s and last > 0:
            return state
        time.sleep(1)
    raise TimeoutError("n8n twin did not go quiet in time")


def offline_expected(rows: list[dict[str, Any]]) -> dict[str, str]:
    llm = OfflineLLM()
    return {r["id"]: agent.qualify(llm, LeadIn(**r["lead"]), model="offline")[0].fit.value for r in rows}


def crm_outcome(
    rows: list[dict[str, Any]], state: dict[str, Any], ledger: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Per lead, which records exist in the fake HubSpot. n8n deals carry the dedupe key; Python
    deals carry the ledger id, so for Python the ledger says which deal belongs to which lead."""
    contacts = set(state.get("contact_emails", []))
    deals = {d["id"]: d for d in state["deals_detail"]}
    deal_by_key = {d["key"]: d for d in state["deals_detail"]}
    drafts = {i: n["created_at"] for n in state["draft_notes"] for i in n["deal_ids"]}
    flagged = {i for n in state["draft_notes"] if n.get("flagged") for i in n["deal_ids"]}
    out = {}
    for r in rows:
        if ledger is not None:
            row = ledger.get(r["lead"]["email"], {})
            deal = deals.get((row.get("hubspot") or {}).get("deal_id") or "")
        else:
            deal = deal_by_key.get(r["key"])
        out[r["id"]] = {
            "contact": r["lead"]["email"] in contacts,
            "deal": deal is not None,
            "draft_at": drafts.get(deal["id"]) if deal else None,
            "price_flagged": bool(deal and deal["id"] in flagged),
        }
    return out


def contact_emails() -> set[str]:
    return set(httpx.get(f"{FAKE}/_fake/state").json().get("contact_emails", []))


# --- scenarios ---
def run_claude(twin: str, secret: str) -> dict[str, Any]:
    tag = "c" + uuid.uuid4().hex[:6]
    rows = load_leads(tag)
    reset_fake()
    offset = vm_offset()
    sent = burst(twin, rows, secret)
    per_lead: dict[str, dict[str, Any]] = {}
    if twin == "python":
        details = {d["email"]: d for d in wait_python(tag, len(rows), 1800)}
        for r in rows:
            d = details[r["lead"]["email"]]
            calls = [e["detail"] for e in d["events"] if e["kind"].startswith("llm_")]
            drafted = datetime.fromisoformat(d["drafted_at"]).timestamp() if d["drafted_at"] else None
            per_lead[r["id"]] = {
                "expected": r["expected_fit"],
                "fit": (d["qualification"] or {}).get("fit"),
                "status": d["status"],
                "to_draft_s": round(drafted + offset - sent[r["id"]], 1) if drafted else None,
                "price_issues": len(d["price_issues"]),
                "llm_calls": len(calls),
                "input_tokens": sum(c["input_tokens"] for c in calls),
                "output_tokens": sum(c["output_tokens"] for c in calls),
            }
    else:
        state = wait_quiet(60, 1800)
        outcome = crm_outcome(rows, state)
        for r in rows:
            o = outcome[r["id"]]
            fit = "qualified" if o["deal"] else "nurture" if o["contact"] else "reject_or_failed"
            per_lead[r["id"]] = {
                "expected": r["expected_fit"],
                "fit": fit,
                "to_draft_s": round(o["draft_at"] + offset - sent[r["id"]], 1) if o["draft_at"] else None,
                "price_issues": int(o["price_flagged"]),
            }
    times = [v["to_draft_s"] for v in per_lead.values() if v["to_draft_s"] is not None]
    correct = sum(
        1
        for v in per_lead.values()
        if v["fit"] == v["expected"] or (v["expected"] == "reject" and v["fit"] == "reject_or_failed")
    )
    return {
        "scenario": "claude",
        "twin": twin,
        "models": {
            "qualify": os.environ.get("QUALIFY_MODEL", "claude-haiku-5-5"),
            "draft": os.environ.get("DRAFT_MODEL", "claude-sonnet-5-5"),
        },
        "leads": len(rows),
        "concurrency": 4,
        "last_draft_s": max(times) if times else None,
        "drafts": len(times),
        "median_to_draft_s": round(statistics.median(times), 1) if times else None,
        "p90_to_draft_s": round(statistics.quantiles(times, n=10)[-1], 1) if len(times) >= 2 else None,
        "fit_agreement": f"{correct}/{len(rows)}",
        "drafts_regenerated": sum(1 for v in per_lead.values() if v.get("llm_calls", 0) == 3),
        "drafts_sent_to_review_flagged": sum(1 for v in per_lead.values() if v.get("price_issues")),
        "median_ack_s": round(statistics.median(ACKS.values()), 2),
        "max_ack_s": round(max(ACKS.values()), 2),
        "per_lead": per_lead,
    }


def run_drill(twin: str, secret: str, kind: str, ratio: float = 0.3) -> dict[str, Any]:
    tag = ("d" if kind == "dupes" else "r") + uuid.uuid4().hex[:6]
    rows = load_leads(tag)
    expected = offline_expected(rows)
    reset_fake(ratio if kind == "ratelimit" else 0.0)
    burst(twin, rows, secret)
    resent = doubles = 0
    if kind == "dupes":
        burst(twin, rows, secret)  # the whole batch again, e.g. a form backend replaying its queue
        resent = len(rows)
        extra = load_leads(tag + "x")[:5]
        for r in extra:  # the same submission twice at the same instant (double-click)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _i, r=r: send(URLS[twin], secret, r["lead"]), range(2)))
        doubles = len(extra)
        rows = rows + extra
        expected.update(offline_expected(extra))
    ledger = None
    if twin == "python":
        ledger = {d["email"]: d for d in wait_python(tag, len(rows), 900)}
    state = wait_quiet(15, 900)
    outcome = crm_outcome(rows, state, ledger)
    complete = sum(
        1
        for r in rows
        if (expected[r["id"]] == "reject" and not outcome[r["id"]]["contact"])
        or (expected[r["id"]] == "nurture" and outcome[r["id"]]["contact"] and not outcome[r["id"]]["deal"])
        or (
            expected[r["id"]] == "qualified"
            and outcome[r["id"]]["deal"]
            and (twin == "python" or outcome[r["id"]]["draft_at"])
        )
    )
    keys = state["deal_keys"]
    result: dict[str, Any] = {
        "scenario": kind,
        "twin": twin,
        "leads": len(rows),
        "resent": resent,
        "concurrent_doubles": doubles,
        "fault_ratio": ratio if kind == "ratelimit" else 0.0,
        "hubspot_calls": state["calls"],
        "injected_429s": state["injected_faults"],
        "complete_leads": complete,
        "duplicate_contacts": state["duplicate_contacts"],
        "duplicate_deals": len(keys) - len(set(keys)),
    }
    if twin == "python":
        mine = [d for d in python_leads() if tag in d["email"]]
        result["ledger_rows"] = len(mine)
        result["ledger_failed"] = sum(1 for d in mine if d["status"] == "failed")
    return result


def write(result: dict[str, Any]) -> Path:
    OUT.mkdir(exist_ok=True)
    suffix = f"-{int(result['fault_ratio'] * 100)}" if result["scenario"] == "ratelimit" else ""
    path = OUT / f"{result['scenario']}{suffix}-{result['twin']}.json"
    path.write_text(json.dumps(result, indent=2), "utf-8")
    return path


def report() -> None:
    results = {p.stem: json.loads(p.read_text("utf-8")) for p in sorted(OUT.glob("*.json"))}
    (HERE / "results.json").write_text(json.dumps(results, indent=2) + "\n", "utf-8")
    print(f"wrote bench/results.json with {len(results)} runs")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["claude", "dupes", "ratelimit", "report"])
    p.add_argument("--twin", choices=["python", "n8n"], default="python")
    p.add_argument("--ratio", type=float, default=0.3, help="share of HubSpot calls answered 429")
    a = p.parse_args()
    if a.scenario == "report":
        report()
        return
    secret = os.environ["WEBHOOK_SECRET"]
    result = (
        run_claude(a.twin, secret)
        if a.scenario == "claude"
        else run_drill(a.twin, secret, a.scenario, a.ratio)
    )
    print(json.dumps({k: v for k, v in result.items() if k != "per_lead"}, indent=2))
    print("saved", write(result))


if __name__ == "__main__":
    main()
