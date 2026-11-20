"""Times a person doing the job by hand, so the automation has a measured baseline to beat.

For each lead: press Enter to start, do the whole job in HubSpot and your mail client, press Enter
when done. Nothing is estimated; only what you time is written.

    uv run python bench/manual_baseline.py --leads L01 L11 L16 --who "sales lead"

The job, per lead (the same steps the service automates):
  1. Read the enquiry and decide: qualified, nurture or reject.
  2. Qualified or nurture: create or update the contact in HubSpot.
  3. Qualified: create the deal, linked to the contact.
  4. Write a triage note (verdict and reasons) on the contact / deal.
  5. Qualified: write a first-draft proposal email using the rate card below.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from lead_to_proposal.agent import RATE_CARD

HERE = Path(__file__).resolve().parent
OUT = HERE / "manual_baseline.json"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--leads", nargs="+", default=["L01", "L11", "L16"])
    p.add_argument("--who", default="operator", help="role of the person timed, e.g. 'sales lead'")
    a = p.parse_args()
    leads = {r["id"]: r for r in json.loads((HERE / "leads.json").read_text("utf-8"))}
    print(__doc__.split("The job, per lead")[1])
    print("Rate card:")
    for line, (title, price) in RATE_CARD.items():
        print(f"  {line:<11} {title}: £{price:,} + VAT")
    runs = []
    for lead_id in a.leads:
        lead = leads[lead_id]["lead"]
        print(f"\n=== {lead_id}: {lead['company']} ({lead['name']}, {lead['email']})")
        print(f"budget: {lead.get('budget_gbp') or 'not given'}\n{lead['message']}\n")
        input("Press Enter to START this lead... ")
        t0 = time.monotonic()
        verdict = input(
            "Do the whole job. Type your verdict (qualified/nurture/reject) and press Enter when DONE: "
        )
        seconds = round(time.monotonic() - t0, 1)
        runs.append({"lead": lead_id, "seconds": seconds, "verdict": verdict.strip().lower()})
        print(f"  {seconds} s")
    secs = [r["seconds"] for r in runs]
    result = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "who": a.who,
        "runs": runs,
        "median_seconds": statistics.median(secs),
        "total_seconds": round(sum(secs), 1),
    }
    OUT.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(f"\nmedian {result['median_seconds']} s per lead, written to {OUT}")


if __name__ == "__main__":
    main()
