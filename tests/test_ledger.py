from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from llm_finops_gateway.ledger import CostEntry, Ledger


def _entry(
    request_id: str, team: str = "platform", cost: str = "0.001", run_id: str | None = None
) -> CostEntry:
    return CostEntry(
        request_id=request_id,
        team=team,
        project="demo",
        use_case="test",
        provider="fake",
        model="m",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        price_book_version="v",
        run_id=run_id,
    )


def test_record_and_summarise(ledger: Ledger) -> None:
    ledger.record(_entry("r1"))
    ledger.record(_entry("r2", cost="0.002"))
    ledger.record(_entry("r3", team="research"))
    rows = ledger.summary()
    assert [(r.team, r.calls) for r in rows] == [("platform", 2), ("research", 1)]
    platform = rows[0]
    assert platform.cost_usd == Decimal("0.003")
    assert platform.input_tokens == 20 and platform.output_tokens == 10


def test_run_id_is_stored_and_optional(ledger: Ledger) -> None:
    ledger.record(_entry("r1", run_id="run-7"))
    ledger.record(_entry("r2", run_id="run-7"))
    ledger.record(_entry("r3"))
    rows = ledger.rows(datetime(2020, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
    assert [r.run_id for r in rows] == ["run-7", "run-7", None]


def test_summary_filters(ledger: Ledger) -> None:
    ledger.record(_entry("r1"))
    ledger.record(_entry("r2", team="research"))
    assert [r.team for r in ledger.summary(team="research")] == ["research"]
    assert ledger.summary(project="nope") == []


def test_file_backed_ledger_persists(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'gw.db'}"
    Ledger(url).record(_entry("r1"))
    assert Ledger(url).summary()[0].calls == 1


def test_month_to_date_ignores_previous_months(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    old = CostEntry(**{**_entry("old").__dict__, "ts": start - timedelta(seconds=1)})
    ledger.record(old)
    ledger.record(_entry("new", cost="0.004"))
    ledger.record(_entry("other-team", team="research", cost="0.100"))
    assert ledger.month_to_date("platform", since=start) == Decimal("0.004")
    assert ledger.month_to_date("nobody", since=start) == Decimal("0")


def test_cost_precision_survives_round_trip(ledger: Ledger) -> None:
    ledger.record(_entry("r1", cost="0.00000075"))
    assert ledger.summary()[0].cost_usd == Decimal("0.00000075")


# ---- budget counters and reservations (ADR-0007) ------------------------------------------


def test_record_updates_counter(ledger: Ledger) -> None:
    ledger.record(_entry("r1", cost="0.5"))
    ledger.record(_entry("r2", cost="0.25"))
    c = ledger.counter("platform")
    assert c.spent_usd == Decimal("0.75") and c.reserved_usd == Decimal(0)
    assert ledger.counter("nobody").committed_usd == Decimal(0)


def test_reserve_respects_limit_and_release(ledger: Ledger) -> None:
    ledger.record(_entry("r1", cost="0.9"))
    assert ledger.reserve("platform", Decimal("0.05"), Decimal("1")) is True
    assert ledger.reserve("platform", Decimal("0.06"), Decimal("1")) is False  # 0.9+0.05+0.06 > 1
    assert ledger.counter("platform").reserved_usd == Decimal("0.05")
    ledger.release("platform", Decimal("0.05"))
    assert ledger.counter("platform").reserved_usd == Decimal(0)
    ledger.release("platform", Decimal("5"))  # over-release floors at zero
    assert ledger.counter("platform").reserved_usd == Decimal(0)


def test_reserve_without_limit_always_succeeds(ledger: Ledger) -> None:
    ledger.record(_entry("r1", cost="100"))
    assert ledger.reserve("platform", Decimal("1"), None) is True


def test_settle_moves_reserved_to_spent(ledger: Ledger) -> None:
    assert ledger.reserve("platform", Decimal("0.02"), Decimal("1"))
    ledger.record(_entry("r1", cost="0.004"), release_reserved=Decimal("0.02"))
    c = ledger.counter("platform")
    assert c.spent_usd == Decimal("0.004") and c.reserved_usd == Decimal(0)


def test_concurrent_reservations_cannot_overshoot(tmp_path: Path) -> None:
    """20 threads each try to reserve 0.1 against a 1.0 limit: exactly 10 may win."""
    from concurrent.futures import ThreadPoolExecutor

    url = f"sqlite:///{tmp_path / 'race.db'}"
    ledger = Ledger(url)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(lambda _: ledger.reserve("platform", Decimal("0.1"), Decimal("1")), range(20))
        )
    assert sum(results) == 10
    assert ledger.counter("platform").reserved_usd == Decimal("1.0")


def test_counter_is_per_month(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    old = CostEntry(**{**_entry("old", cost="9").__dict__, "ts": start - timedelta(seconds=1)})
    ledger.record(old)
    assert ledger.counter("platform").spent_usd == Decimal(0)
    assert ledger.counter(
        "platform", month=(start - timedelta(days=1)).strftime("%Y-%m")
    ).spent_usd == Decimal("9")
