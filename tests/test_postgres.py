"""Integration tests against real Postgres (testcontainers). They check the two properties SQLite
cannot: SKIP LOCKED keeps parallel workers off each other's leads, and the Alembic migration
produces the schema the code expects."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from lead_to_proposal.db import Lead, claim_next, insert_lead, make_engine
from lead_to_proposal.models import LeadIn

from .conftest import lead_payload

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if os.environ.get("SKIP_DOCKER_TESTS"):
        pytest.skip("SKIP_DOCKER_TESTS is set")
    try:
        from testcontainers.postgres import PostgresContainer

        pg = PostgresContainer("postgres:17-alpine", driver="psycopg")
        pg.start()
    except Exception as e:
        pytest.skip(f"Docker unavailable: {e}")
    url = pg.get_connection_url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    eng = make_engine(url)
    yield eng
    eng.dispose()
    pg.stop()


def test_migration_matches_models(engine: Engine) -> None:
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("leads")}
    assert cols == set(Lead.__table__.columns.keys())
    assert "ix_leads_status_received" in {i["name"] for i in insp.get_indexes("leads")}


def test_parallel_workers_never_claim_the_same_lead(engine: Engine) -> None:
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as s:
        for i in range(40):
            insert_lead(s, LeadIn(**lead_payload(email=f"p{i}@example.com")), None)
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        while True:
            with sessions() as s:
                row = claim_next(s)
                if row is None:
                    return
                with lock:
                    claimed.append(row.id)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40
    assert len(set(claimed)) == 40  # no lead handed to two workers
