"""Live run: the 20 leads through the Python service into a real HubSpot portal, then a duplicate
drill, then every record read back from HubSpot's own API.

Needs the live stack (bench/compose.live.yml), the host LLM proxy, and HUBSPOT_TOKEN in the env.

    uv run python bench/live.py        # writes bench/out/live-python.json

What it checks in HubSpot, per lead, against the verdict in the ledger:
  qualified -> one contact, exactly one deal carrying the lead key, a note on the deal
  nurture   -> one contact with a note, no deal
  reject    -> no contact
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
from run import ACKS, OUT, URLS, burst, load_leads, vm_offset, wait_python
from send_lead import send  # on sys.path via run

HUBSPOT = "https://api.hubapi.com"


class Portal:
    """Read-only checks against the real portal, paced well under HubSpot's burst limit."""

    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url=HUBSPOT, headers={"authorization": f"Bearer {token}"}, timeout=30)
        self.calls = 0

    def get(self, path: str, **params: str) -> httpx.Response:
        time.sleep(0.15)
        self.calls += 1
        return self.http.get(path, params=params)

    def assoc(self, obj: str, oid: str, to: str) -> list[str]:
        res = self.get(f"/crm/v4/objects/{obj}/{oid}/associations/{to}")
        res.raise_for_status()
        return [str(r["toObjectId"]) for r in res.json()["results"]]

    def lead_records(self, email: str) -> dict[str, Any]:
        res = self.get(f"/crm/v3/objects/contacts/{email}", idProperty="email")
        if res.status_code == 404:
            return {"contact": False, "deals": 0, "deal_keys": [], "deal_notes": 0, "contact_notes": 0}
        res.raise_for_status()
        cid = res.json()["id"]
        deals = self.assoc("contacts", cid, "deals")
        keys = []
        deal_notes = 0
        for d in deals:
            r = self.get(f"/crm/v3/objects/deals/{d}", properties="ltp_lead_key")
            r.raise_for_status()
            keys.append(r.json()["properties"].get("ltp_lead_key"))
            deal_notes += len(self.assoc("deals", d, "notes"))
        return {
            "contact": True,
            "deals": len(deals),
            "deal_keys": keys,
            "deal_notes": deal_notes,
            "contact_notes": len(self.assoc("contacts", cid, "notes")),
        }


def consistent(fit: str | None, rec: dict[str, Any], ledger_id: str) -> bool:
    if fit == "qualified":
        return bool(rec["contact"] and rec["deal_keys"] == [ledger_id] and rec["deal_notes"] >= 1)
    if fit == "nurture":
        return bool(rec["contact"] and rec["deals"] == 0 and rec["contact_notes"] >= 1)
    return not rec["contact"]


def worker_log_count(since: str, needle: str) -> int:
    # Fixed argv, no shell; `since` is a timestamp this script made.
    out = subprocess.run(  # noqa: S603
        ["docker", "compose", "logs", "worker", "--since", since, "--no-log-prefix"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout
    return sum(1 for line in out.splitlines() if needle in line)


def main() -> None:
    secret = os.environ["WEBHOOK_SECRET"]
    portal = Portal(os.environ["HUBSPOT_TOKEN"])
    started = datetime.now(UTC).isoformat(timespec="seconds")
    tag = "l" + uuid.uuid4().hex[:6]
    rows = load_leads(tag)
    offset = vm_offset()
    sent = burst("python", rows, secret)
    details = {d["email"]: d for d in wait_python(tag, len(rows), 1800)}
    first_acks = dict(ACKS)

    # Duplicate drill: the batch replayed, plus five new submissions each sent twice at once.
    burst("python", rows, secret)
    extra = load_leads(tag + "x")[:5]
    for r in extra:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _i, r=r: send(URLS["python"], secret, r["lead"]), range(2)))
    details = {d["email"]: d for d in wait_python(tag, len(rows) + len(extra), 1800)}

    per_lead: dict[str, dict[str, Any]] = {}
    for r in rows + extra:
        key = r["id"] if r in rows else r["id"] + "x"
        d = details[r["lead"]["email"]]
        fit = (d["qualification"] or {}).get("fit")
        drafted = datetime.fromisoformat(d["drafted_at"]).timestamp() if d["drafted_at"] else None
        rec = portal.lead_records(r["lead"]["email"])
        per_lead[key] = {
            "expected": r["expected_fit"],
            "fit": fit,
            "status": d["status"],
            "to_draft_s": round(drafted + offset - sent[r["id"]], 1) if drafted and r in rows else None,
            "hubspot": {k: v for k, v in rec.items() if k != "deal_keys"},
            "hubspot_matches_ledger": consistent(fit, rec, d["id"]),
        }
    main_rows = [per_lead[r["id"]] for r in rows]
    times = [v["to_draft_s"] for v in main_rows if v["to_draft_s"] is not None]
    matches = sum(v["hubspot_matches_ledger"] for v in per_lead.values())
    result = {
        "scenario": "live",
        "twin": "python",
        "hubspot": "developer test account, CRM v3/v4 API, EU data hosting",
        "measured_at": started,
        "models": {
            "qualify": os.environ.get("QUALIFY_MODEL", "claude-haiku-5-5"),
            "draft": os.environ.get("DRAFT_MODEL", "claude-sonnet-5-5"),
        },
        "leads": len(rows),
        "concurrency": 4,
        "fit_agreement": f"{sum(1 for v in main_rows if v['fit'] == v['expected'])}/{len(rows)}",
        "drafts": len(times),
        "median_to_draft_s": round(statistics.median(times), 1) if times else None,
        "p90_to_draft_s": round(statistics.quantiles(times, n=10)[-1], 1) if len(times) >= 2 else None,
        "median_ack_s": round(statistics.median(first_acks.values()), 2),
        "resent": len(rows),
        "concurrent_doubles": len(extra),
        "hubspot_matches_ledger": f"{matches}/{len(per_lead)}",
        "duplicate_deals": sum(max(0, v["hubspot"]["deals"] - 1) for v in per_lead.values()),
        "ledger_failed": sum(1 for v in per_lead.values() if v["status"] == "failed"),
        "hubspot_retries_logged": worker_log_count(started, "hubspot_retry"),
        "verification_calls": portal.calls,
        "per_lead": per_lead,
    }
    OUT.mkdir(exist_ok=True)
    path = OUT / "live-python.json"
    path.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "per_lead"}, indent=2))
    print("saved", path)


if __name__ == "__main__":
    main()
