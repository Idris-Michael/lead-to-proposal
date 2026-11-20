"""Signs and posts one lead, the way the website form backend would.

uv run python scripts/send_lead.py bench/leads.json --index 0
uv run python scripts/send_lead.py my-lead.json --url http://localhost:5678/webhook/lead
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

from lead_to_proposal.signing import sign

# One pooled client: building a client per request costs ~1.5 s each under a 20-thread burst,
# which would be measured as the service's response time.
_CLIENT = httpx.Client()


def send(
    url: str, secret: str, lead: dict[str, object], idempotency_key: str | None = None, timeout: float = 30
) -> httpx.Response:
    body = json.dumps(lead).encode()
    ts = int(time.time())
    headers = {
        "content-type": "application/json",
        "x-timestamp": str(ts),
        "x-signature": sign(secret, body, ts),
    }
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    return _CLIENT.post(url, content=body, headers=headers, timeout=timeout)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file", type=Path, help="a lead object, or a list of leads")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--url", default="http://localhost:8080/v1/leads")
    a = p.parse_args()
    data = json.loads(a.file.read_text("utf-8"))
    lead = data[a.index]["lead"] if isinstance(data, list) else data
    res = send(a.url, os.environ["WEBHOOK_SECRET"], lead)
    print(res.status_code, res.text)


if __name__ == "__main__":
    main()
