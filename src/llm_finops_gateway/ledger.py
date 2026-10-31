"""Cost ledger: one row per completed LLM call, the source of truth for chargeback.

Also holds the per-team monthly budget counters used for atomic reservations (ADR-0007). A
reservation is an atomic conditional UPDATE, so two concurrent calls near the limit cannot both
pass; the ledger row and the counter settlement are written in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import (
    DateTime,
    Integer,
    Numeric,
    String,
    case,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from .pricing import money

ZERO = Decimal(0)


class Base(DeclarativeBase):
    pass


class CostRecord(Base):
    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    request_id: Mapped[str] = mapped_column(String(40), unique=True)
    team: Mapped[str] = mapped_column(String(64), index=True)
    project: Mapped[str] = mapped_column(String(64), index=True)
    use_case: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    price_book_version: Mapped[str] = mapped_column(String(32))


class BudgetCounter(Base):
    """Running month-to-date totals per team. ``reserved_usd`` is spend in flight."""

    __tablename__ = "budget_counters"

    team: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM (UTC)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=ZERO)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=ZERO)


@dataclass(frozen=True)
class CostEntry:
    request_id: str
    team: str
    project: str
    use_case: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    price_book_version: str
    ts: datetime | None = None  # defaults to now (UTC); explicit only for backfills and tests
    key_id: str | None = None
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ModelUsage:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class SummaryRow:
    team: str
    project: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class Counter:
    spent_usd: Decimal
    reserved_usd: Decimal

    @property
    def committed_usd(self) -> Decimal:
        return self.spent_usd + self.reserved_usd


def clean(value: object) -> Decimal:
    """Decimal from a DB value without exponent noise ('0E-8' -> '0', '1.5000' -> '1.5')."""
    return Decimal(money(Decimal(str(value))))


def month_key(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).strftime("%Y-%m")


class Ledger:
    def __init__(self, url: str = "sqlite:///./gateway.db") -> None:
        self._engine: Engine = _make_engine(url)
        Base.metadata.create_all(self._engine)

    # ---- writes ------------------------------------------------------------------------

    def record(self, entry: CostEntry, release_reserved: Decimal = ZERO) -> None:
        """Insert the ledger row and settle the counter in one transaction."""
        ts = entry.ts or datetime.now(UTC)
        month = month_key(ts)
        with Session(self._engine) as session:
            session.add(
                CostRecord(
                    ts=ts,
                    request_id=entry.request_id,
                    team=entry.team,
                    project=entry.project,
                    use_case=entry.use_case,
                    key_id=entry.key_id,
                    provider=entry.provider,
                    model=entry.model,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cached_input_tokens=entry.cached_input_tokens,
                    cost_usd=entry.cost_usd,
                    price_book_version=entry.price_book_version,
                )
            )
            self._ensure_counter(session, entry.team, month)
            session.execute(
                update(BudgetCounter)
                .where(BudgetCounter.team == entry.team, BudgetCounter.month == month)
                .values(
                    spent_usd=BudgetCounter.spent_usd + entry.cost_usd,
                    reserved_usd=_floor_zero(BudgetCounter.reserved_usd - release_reserved),
                )
            )
            session.commit()

    def reserve(self, team: str, amount: Decimal, limit: Decimal | None) -> bool:
        """Atomically add ``amount`` to the team's reservation if the result stays within
        ``limit`` (``None`` = unlimited). Returns False, and reserves nothing, otherwise."""
        month = month_key()
        with Session(self._engine) as session:
            self._ensure_counter(session, team, month)
            stmt = (
                update(BudgetCounter)
                .where(BudgetCounter.team == team, BudgetCounter.month == month)
                .values(reserved_usd=BudgetCounter.reserved_usd + amount)
            )
            if limit is not None:
                stmt = stmt.where(
                    BudgetCounter.spent_usd + BudgetCounter.reserved_usd + amount <= limit
                )
            result = cast(CursorResult[Any], session.execute(stmt))
            session.commit()
            return bool(result.rowcount == 1)

    def release(self, team: str, amount: Decimal) -> None:
        """Give back a reservation (upstream failure, nothing was spent)."""
        month = month_key()
        with Session(self._engine) as session:
            session.execute(
                update(BudgetCounter)
                .where(BudgetCounter.team == team, BudgetCounter.month == month)
                .values(reserved_usd=_floor_zero(BudgetCounter.reserved_usd - amount))
            )
            session.commit()

    # ---- reads -------------------------------------------------------------------------

    def counter(self, team: str, month: str | None = None) -> Counter:
        month = month or month_key()
        with Session(self._engine) as session:
            row = session.get(BudgetCounter, (team, month))
        if row is None:
            return Counter(ZERO, ZERO)
        return Counter(clean(row.spent_usd), clean(row.reserved_usd))

    def month_to_date(self, team: str, since: datetime) -> Decimal:
        """Total recorded spend for a team since ``since`` (ledger rows, for reports)."""
        stmt = select(func.coalesce(func.sum(CostRecord.cost_usd), 0)).where(
            CostRecord.team == team, CostRecord.ts >= since
        )
        with Session(self._engine) as session:
            total = session.execute(stmt).scalar_one()
        return clean(total)

    def usage_by_model(self, start: datetime, end: datetime) -> list[ModelUsage]:
        """Calls, tokens and cost per model for rows with ``start <= ts < end`` (reconciliation)."""
        stmt = (
            select(
                CostRecord.model,
                func.count().label("calls"),
                func.coalesce(func.sum(CostRecord.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(CostRecord.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(CostRecord.cached_input_tokens), 0).label("cached"),
                func.coalesce(func.sum(CostRecord.cost_usd), 0).label("cost_usd"),
            )
            .where(CostRecord.ts >= start, CostRecord.ts < end)
            .group_by(CostRecord.model)
            .order_by(CostRecord.model)
        )
        with Session(self._engine) as session:
            rows = session.execute(stmt).all()
        return [
            ModelUsage(
                model=str(r.model),
                calls=int(r.calls),
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
                cached_input_tokens=int(r.cached),
                cost_usd=clean(r.cost_usd),
            )
            for r in rows
        ]

    def summary(self, team: str | None = None, project: str | None = None) -> list[SummaryRow]:
        stmt = (
            select(
                CostRecord.team,
                CostRecord.project,
                CostRecord.model,
                func.count().label("calls"),
                func.coalesce(func.sum(CostRecord.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(CostRecord.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(CostRecord.cost_usd), 0).label("cost_usd"),
            )
            .group_by(CostRecord.team, CostRecord.project, CostRecord.model)
            .order_by(CostRecord.team, CostRecord.project, CostRecord.model)
        )
        if team:
            stmt = stmt.where(CostRecord.team == team)
        if project:
            stmt = stmt.where(CostRecord.project == project)
        with Session(self._engine) as session:
            rows = session.execute(stmt).all()
        return [
            SummaryRow(
                team=str(r.team),
                project=str(r.project),
                model=str(r.model),
                calls=int(r.calls),
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
                cost_usd=clean(r.cost_usd),
            )
            for r in rows
        ]

    # ---- internals ---------------------------------------------------------------------

    @staticmethod
    def _ensure_counter(session: Session, team: str, month: str) -> None:
        if session.get(BudgetCounter, (team, month)) is not None:
            return
        try:
            with session.begin_nested():
                session.execute(
                    insert(BudgetCounter).values(
                        team=team, month=month, spent_usd=ZERO, reserved_usd=ZERO
                    )
                )
        except IntegrityError:
            pass  # another request created it first; that is fine


def _floor_zero(expr: Any) -> Any:
    """Portable ``GREATEST(expr, 0)`` (SQLite has no GREATEST)."""
    return case((expr < 0, ZERO), else_=expr)


def _make_engine(url: str) -> Engine:
    if url.endswith(":memory:"):
        # Share one in-memory database across threads (FastAPI runs sync code in a pool).
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
        )
    return create_engine(url, future=True)
