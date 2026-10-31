"""The ledger against a real PostgreSQL. Runs only when LFG_TEST_DATABASE_URL points at one:

    docker run -d --rm --name lfg-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=lfg \
        -p 55432:5432 postgres:16-alpine
    LFG_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:55432/lfg \
        pytest tests/test_ledger_postgres.py
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from llm_finops_gateway.ledger import CostEntry, Ledger

URL = os.environ.get("LFG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="LFG_TEST_DATABASE_URL not set")


@pytest.fixture
def pg_ledger() -> Ledger:
    assert URL is not None
    ledger = Ledger(URL)
    with ledger._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        conn.execute(text("DELETE FROM cost_records"))
        conn.execute(text("DELETE FROM budget_counters"))
    return ledger


def _entry(team: str = "platform", cost: str = "0.5", **kw: object) -> CostEntry:
    return CostEntry(
        request_id=f"gw-{uuid.uuid4().hex}",
        team=team,
        project="p",
        use_case="u",
        provider="azure_openai",
        model="gpt-4.1-mini",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal(cost),
        price_book_version="t",
        **kw,  # type: ignore[arg-type]
    )


def test_record_reserve_release_and_summary_on_postgres(pg_ledger: Ledger) -> None:
    assert pg_ledger.reserve("platform", Decimal("1"), limit=Decimal("2")) is True
    assert pg_ledger.reserve("platform", Decimal("1.5"), limit=Decimal("2")) is False  # over
    assert pg_ledger.counter("platform").reserved_usd == Decimal("1")
    pg_ledger.record(_entry(cost="0.5", cached_input_tokens=40), release_reserved=Decimal("1"))
    counter = pg_ledger.counter("platform")
    assert (counter.spent_usd, counter.reserved_usd) == (Decimal("0.5"), Decimal("0"))
    pg_ledger.release("platform", Decimal("5"))  # never below zero
    assert pg_ledger.counter("platform").reserved_usd == Decimal("0")
    rows = pg_ledger.summary()
    assert len(rows) == 1 and rows[0].cost_usd == Decimal("0.5") and rows[0].calls == 1


def test_usage_by_model_and_month_to_date_on_postgres(pg_ledger: Ledger) -> None:
    now = datetime.now(UTC)
    pg_ledger.record(_entry(cost="0.25", ts=now - timedelta(days=1)))
    pg_ledger.record(_entry(cost="0.75", ts=now))
    usage = pg_ledger.usage_by_model(now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(usage) == 1 and usage[0].calls == 1 and usage[0].cost_usd == Decimal("0.75")
    assert pg_ledger.month_to_date("platform", now - timedelta(days=2)) == Decimal("1")


def test_duplicate_request_id_is_rejected_on_postgres(pg_ledger: Ledger) -> None:
    entry = _entry()
    pg_ledger.record(entry)
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError wrapped by SQLAlchemy
        pg_ledger.record(entry)
