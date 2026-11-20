"""The fake HubSpot over real HTTP, for drills that both twins (Python and n8n) must face equally.
`/_fake/*` endpoints let the benchmark read record counts, set fault ratios and reset state."""

import os
import time

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from .hubspot_fake import FakeHubSpot


class FaultConfig(BaseModel):
    rate_limit_ratio: float = Field(default=0.0, ge=0, le=0.9)
    server_error_ratio: float = Field(default=0.0, ge=0, le=0.5)
    seed: int = 7


def create_app() -> FastAPI:
    app = FastAPI(title="fake-hubspot", version="0.1.0")
    state = {"fake": FakeHubSpot()}

    @app.get("/_fake/state")
    def fake_state() -> dict[str, object]:
        f = state["fake"]
        emails = [c["properties"]["email"] for c in f.contacts.values()]
        keys = [d["properties"].get("ltp_lead_key") for d in f.deals.values()]
        return {
            "contacts": len(f.contacts),
            "deals": len(f.deals),
            "notes": len(f.notes),
            "duplicate_contacts": len(emails) - len(set(emails)),
            "contact_emails": emails,
            "calls": f.calls,
            "injected_faults": f.injected,
            "deal_keys": keys,
            "deals_detail": [
                {"id": d["id"], "key": d["properties"].get("ltp_lead_key"), "created_at": d["created_at"]}
                for d in f.deals.values()
            ],
            "draft_notes": [
                {
                    "created_at": n["created_at"],
                    "deal_ids": [a["to"]["id"] for a in n.get("associations", [])],
                }
                for n in f.notes.values()
                if str(n["properties"].get("hs_note_body", "")).startswith(("PROPOSAL DRAFT", "PRICE CHECK"))
            ],
        }

    @app.get("/_fake/time")
    def fake_time() -> dict[str, float]:
        return {"now": time.time()}

    @app.post("/_fake/reset")
    def reset(cfg: FaultConfig) -> dict[str, str]:
        state["fake"] = FakeHubSpot(
            rate_limit_ratio=cfg.rate_limit_ratio, server_error_ratio=cfg.server_error_ratio, seed=cfg.seed
        )
        return {"status": "reset"}

    @app.api_route("/crm/{path:path}", methods=["GET", "POST", "PATCH"])
    async def crm(path: str, request: Request) -> Response:
        req = httpx.Request(
            request.method,
            str(request.url),
            headers={"authorization": request.headers.get("authorization", "")},
            content=await request.body(),
        )
        res = state["fake"].handle(req)
        return Response(res.content, status_code=res.status_code, media_type="application/json")

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("FAKE_HUBSPOT_HOST", "0.0.0.0"), port=8091)  # noqa: S104
