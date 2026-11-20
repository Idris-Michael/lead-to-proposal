"""HubSpot CRM v3 client: contact upsert by email, deal create (idempotent by a lead key property),
and a note. Every call goes through one retry loop that handles 429s and 5xx (ADR 0003)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .models import LeadIn, Qualification

log = structlog.get_logger(__name__)

# HubSpot-defined association type ids (CRM v3).
DEAL_TO_CONTACT = 3
NOTE_TO_CONTACT = 202
NOTE_TO_DEAL = 214

LEAD_KEY_PROPERTY = "ltp_lead_key"
RETRYABLE = {429, 500, 502, 503, 504}

LEAD_STATUS = {"qualified": "OPEN_DEAL", "nurture": "OPEN", "reject": "UNQUALIFIED"}
BUDGET_TO_AMOUNT = {"under_2k": 1500, "2k_10k": 6000, "10k_50k": 25000, "over_50k": 60000}


class HubSpotError(Exception):
    def __init__(self, status: int | None, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"hubspot {status or 'transport'}: {message}")
        self.status = status
        self.retryable = retryable


@dataclass
class RetryStats:
    """Counts what the retry loop absorbed, so the benchmark can report it."""

    rate_limited: int = 0
    server_errors: int = 0
    transport_errors: int = 0
    slept_s: float = 0.0
    by_path: dict[str, int] = field(default_factory=dict)


class HubSpotClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.hubapi.com",
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 6,
        base_delay_s: float = 0.5,
        max_delay_s: float = 12.0,
        timeout_s: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if not token:
            raise ValueError("HUBSPOT_TOKEN is not set")
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout_s,
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        )
        self._max_attempts = max_attempts
        self._base = base_delay_s
        self._cap = max_delay_s
        self._sleep = sleep
        self._rng = rng
        self.stats = RetryStats()

    # --- transport ---
    def _request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            try:
                res = self._http.request(method, path, json=json, params=params)
            except httpx.TransportError as e:
                self.stats.transport_errors += 1
                if attempt == self._max_attempts:
                    raise HubSpotError(None, str(e), retryable=True) from e
                self._wait(attempt, None)
                continue
            if res.status_code not in RETRYABLE:
                return res
            if res.status_code == 429:
                self.stats.rate_limited += 1
                if _policy(res) == "DAILY":
                    # The daily quota resets at midnight; retrying now only burns the next request.
                    raise HubSpotError(429, "daily API limit reached", retryable=False)
            else:
                self.stats.server_errors += 1
            self.stats.by_path[path] = self.stats.by_path.get(path, 0) + 1
            if attempt == self._max_attempts:
                raise HubSpotError(res.status_code, f"gave up after {attempt} attempts", retryable=True)
            self._wait(attempt, res.headers.get("retry-after"))
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        if retry_after is not None and retry_after.isdigit():
            delay = float(retry_after)
        else:
            # Full jitter: spreads retries from parallel workers instead of synchronising them.
            delay = min(self._cap, self._base * 2 ** (attempt - 1)) * self._rng()
        self.stats.slept_s += delay
        log.info("hubspot_retry", attempt=attempt, delay_s=round(delay, 3))
        self._sleep(delay)

    # --- setup ---
    def ensure_properties(self) -> None:
        """Creates the custom properties this integration writes. Safe to run repeatedly."""
        specs = [
            ("deals", LEAD_KEY_PROPERTY, "Lead key (lead-to-proposal)", "dealinformation", True),
            ("contacts", "ltp_score", "Lead score (lead-to-proposal)", "contactinformation", False),
            ("contacts", "ltp_fit", "Lead fit (lead-to-proposal)", "contactinformation", False),
        ]
        for obj, name, label, group, unique in specs:
            body = {
                "name": name,
                "label": label,
                "groupName": group,
                "type": "string",
                "fieldType": "text",
                "hasUniqueValue": unique,
            }
            res = self._request("POST", f"/crm/v3/properties/{obj}", json=body)
            if res.status_code not in (200, 201, 409):  # 409: already exists
                raise HubSpotError(res.status_code, res.text[:200])

    # --- CRM writes ---
    def upsert_contact(self, lead: LeadIn, q: Qualification) -> str:
        """Upsert keyed by email: update if the contact exists, otherwise create. A 409 on create
        means another worker created it in between, so the update is tried once more."""
        first, _, last = lead.name.partition(" ")
        props: dict[str, str] = {
            "email": lead.email,
            "firstname": first,
            "lastname": last,
            "company": lead.company,
            "hs_lead_status": LEAD_STATUS[q.fit.value],
            "ltp_score": str(q.score),
            "ltp_fit": q.fit.value,
        }
        if lead.website:
            props["website"] = str(lead.website)
        path = f"/crm/v3/objects/contacts/{lead.email}"
        res = self._request("PATCH", path, json={"properties": props}, params={"idProperty": "email"})
        if res.status_code == 200:
            return str(res.json()["id"])
        if res.status_code != 404:
            raise HubSpotError(res.status_code, res.text[:200])
        res = self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})
        if res.status_code == 201:
            return str(res.json()["id"])
        if res.status_code == 409:
            res = self._request("PATCH", path, json={"properties": props}, params={"idProperty": "email"})
            if res.status_code == 200:
                return str(res.json()["id"])
        raise HubSpotError(res.status_code, res.text[:200])

    def find_deal(self, lead_key: str) -> str | None:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": LEAD_KEY_PROPERTY, "operator": "EQ", "value": lead_key}]}
            ],
            "limit": 1,
        }
        res = self._request("POST", "/crm/v3/objects/deals/search", json=body)
        if res.status_code != 200:
            raise HubSpotError(res.status_code, res.text[:200])
        results = res.json().get("results", [])
        return str(results[0]["id"]) if results else None

    def create_deal(self, lead: LeadIn, q: Qualification, contact_id: str, lead_key: str) -> str:
        """Idempotent by `lead_key`: a deal already carrying the key is returned, not duplicated.
        The key property is unique in HubSpot, so a racing duplicate create is refused (409)."""
        existing = self.find_deal(lead_key)
        if existing:
            return existing
        props = {
            "dealname": f"{lead.company} - {q.service_line.replace('_', ' ')}",
            "pipeline": "default",
            "dealstage": "appointmentscheduled",
            LEAD_KEY_PROPERTY: lead_key,
        }
        amount = lead.budget_gbp or BUDGET_TO_AMOUNT.get(q.budget_band)
        if amount:
            props["amount"] = str(amount)
        body = {
            "properties": props,
            "associations": [_assoc(contact_id, DEAL_TO_CONTACT)],
        }
        res = self._request("POST", "/crm/v3/objects/deals", json=body)
        if res.status_code == 201:
            return str(res.json()["id"])
        if res.status_code == 409:
            found = self.find_deal(lead_key)
            if found:
                return found
        raise HubSpotError(res.status_code, res.text[:200])

    def add_note(self, body: str, contact_id: str, deal_id: str | None) -> str:
        assocs = [_assoc(contact_id, NOTE_TO_CONTACT)]
        if deal_id:
            assocs.append(_assoc(deal_id, NOTE_TO_DEAL))
        payload = {
            "properties": {"hs_note_body": body, "hs_timestamp": datetime.now(UTC).isoformat()},
            "associations": assocs,
        }
        res = self._request("POST", "/crm/v3/objects/notes", json=payload)
        if res.status_code != 201:
            raise HubSpotError(res.status_code, res.text[:200])
        return str(res.json()["id"])

    def close(self) -> None:
        self._http.close()


def _assoc(to_id: str, type_id: int) -> dict[str, Any]:
    return {
        "to": {"id": to_id},
        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
    }


def _policy(res: httpx.Response) -> str | None:
    try:
        data = res.json()
    except ValueError:
        return None
    return data.get("policyName") if isinstance(data, dict) else None
