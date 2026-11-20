"""An in-process stand-in for the HubSpot endpoints this service uses. Mock mode wires it into the
real client through an httpx transport, so mock runs exercise the same retry and upsert code as
production. It can inject 429s and 5xx to drill the failure paths."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeHubSpot:
    rate_limit_ratio: float = 0.0
    server_error_ratio: float = 0.0
    seed: int = 7
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> record
    deals: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: dict[str, dict[str, Any]] = field(default_factory=dict)
    properties: set[tuple[str, str]] = field(default_factory=set)
    calls: int = 0
    injected: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)  # noqa: S311 - fault injection, not security
        self._lock = threading.Lock()
        self._next_id = 1000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- dispatch ---
    def handle(self, req: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls += 1
            roll = self._rng.random()
            if roll < self.rate_limit_ratio:
                self.injected += 1
                return httpx.Response(429, json={"status": "error", "policyName": "TEN_SECONDLY_ROLLING"})
            if roll < self.rate_limit_ratio + self.server_error_ratio:
                self.injected += 1
                return httpx.Response(502, text="bad gateway")
            if req.headers.get("authorization", "") == "Bearer ":
                return httpx.Response(401, json={"message": "missing token"})
            return self._route(req)

    def _route(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if path.startswith("/crm/v3/properties/"):
            return self._create_property(path.rsplit("/", 1)[1], body)
        if path == "/crm/v3/objects/contacts" and req.method == "POST":
            return self._create_contact(body)
        if path.startswith("/crm/v3/objects/contacts/") and req.method == "PATCH":
            return self._patch_contact(path.rsplit("/", 1)[1], req.url.params.get("idProperty"), body)
        if path == "/crm/v3/objects/deals/search":
            return self._search_deals(body)
        if path == "/crm/v3/objects/deals" and req.method == "POST":
            return self._create_deal(body)
        if path == "/crm/v3/objects/notes" and req.method == "POST":
            return self._create(self.notes, body)
        return httpx.Response(404, json={"message": f"no fake for {req.method} {path}"})

    # --- handlers ---
    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _create(self, store: dict[str, dict[str, Any]], body: dict[str, Any]) -> httpx.Response:
        rid = self._id()
        store[rid] = {"id": rid, "created_at": time.time(), **body}
        return httpx.Response(201, json={"id": rid, "properties": body.get("properties", {})})

    def _create_property(self, obj: str, body: dict[str, Any]) -> httpx.Response:
        key = (obj, body["name"])
        if key in self.properties:
            return httpx.Response(409, json={"message": "property exists"})
        self.properties.add(key)
        return httpx.Response(201, json=body)

    def _contact_by_email(self, email: str) -> dict[str, Any] | None:
        for c in self.contacts.values():
            if c["properties"]["email"] == email.lower():
                return c
        return None

    def _create_contact(self, body: dict[str, Any]) -> httpx.Response:
        if self._contact_by_email(body["properties"]["email"]):
            return httpx.Response(409, json={"message": "Contact already exists"})
        return self._create(self.contacts, body)

    def _patch_contact(self, ident: str, id_property: str | None, body: dict[str, Any]) -> httpx.Response:
        record = self._contact_by_email(ident) if id_property == "email" else self.contacts.get(ident)
        if record is None:
            return httpx.Response(404, json={"message": "resource not found"})
        record["properties"].update(body["properties"])
        return httpx.Response(200, json={"id": record["id"], "properties": record["properties"]})

    def _search_deals(self, body: dict[str, Any]) -> httpx.Response:
        f = body["filterGroups"][0]["filters"][0]
        hits = [
            {"id": d["id"]}
            for d in self.deals.values()
            if d["properties"].get(f["propertyName"]) == f["value"]
        ]
        return httpx.Response(200, json={"total": len(hits), "results": hits[: body.get("limit", 10)]})

    def _create_deal(self, body: dict[str, Any]) -> httpx.Response:
        key = body["properties"].get("ltp_lead_key")
        if key and any(d["properties"].get("ltp_lead_key") == key for d in self.deals.values()):
            return httpx.Response(409, json={"message": "unique property value already exists"})
        return self._create(self.deals, body)
