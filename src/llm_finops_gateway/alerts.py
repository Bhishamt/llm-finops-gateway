"""Webhook alerts for policy and upstream events (ADR-0017).

One HTTP POST per event to ``LFG_ALERT_WEBHOOK_URL``: a Slack or Teams incoming webhook, or
anything that takes JSON. The payload has a human ``text`` line plus structured fields. Events
are sent in the background and never delay or fail a request; the same event for the same
team (or provider) is sent once per cooldown window, so a team at 85 % of its budget does not
page every call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .policy import BudgetStatus
from .pricing import money

log = logging.getLogger("llm_finops_gateway.alerts")


class Alerter:
    def __init__(
        self,
        url: str | None,
        cooldown_seconds: int = 3600,
        currency: str = "USD",
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._cooldown = cooldown_seconds
        self._currency = currency
        self._timeout = timeout
        self._client = client
        self._last_sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.sent = 0  # events actually posted (for tests and /healthz-style introspection)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    # ---- events --------------------------------------------------------------------------

    def budget_warn(self, team: str, status: BudgetStatus, message: str) -> None:
        self._emit(
            key=f"budget_warn:{team}",
            event="budget_warn",
            text=(
                f":warning: LLM budget warning: team {team} has used {status.used_pct} % "
                f"of {money(status.limit_usd)} {self._currency} this month"
            ),
            team=team,
            level=status.level,
            used_pct=str(status.used_pct),
            spent=money(status.spent_usd),
            limit=money(status.limit_usd),
            message=message,
        )

    def budget_block(self, team: str, status: BudgetStatus | None, message: str) -> None:
        pct = str(status.used_pct) if status else "?"
        limit = money(status.limit_usd) if status else "?"
        self._emit(
            key=f"budget_block:{team}",
            event="budget_block",
            text=(
                f":no_entry: LLM budget exhausted: team {team} is blocked at {pct} % "
                f"of {limit} {self._currency}"
            ),
            team=team,
            level="block",
            used_pct=pct,
            spent=money(status.spent_usd) if status else "?",
            limit=limit,
            message=message,
        )

    def upstream_failure(self, provider: str, outcome: str, model: str, detail: str) -> None:
        self._emit(
            key=f"{outcome}:{provider}",
            event=outcome,
            text=f":rotating_light: LLM gateway: {outcome} from provider {provider} ({model})",
            provider=provider,
            model=model,
            message=detail[:300],
        )

    # ---- plumbing ------------------------------------------------------------------------

    def _emit(self, key: str, event: str, text: str, **fields: Any) -> None:
        if not self._url:
            return
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self._cooldown:
            return
        self._last_sent[key] = now
        payload = {"text": text, "event": event, "source": "llm-finops-gateway", **fields}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (unit code outside the server): send nothing
            log.warning("alert %s dropped: no running event loop", event)
            return
        task = loop.create_task(self._post(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, payload: dict[str, Any]) -> None:
        assert self._url is not None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            self.sent += 1
        except Exception as exc:  # alerts must never break requests
            log.warning("alert %s not delivered: %s", payload.get("event"), exc)
        finally:
            if client is not self._client:
                await client.aclose()

    async def aclose(self) -> None:
        """Wait for in-flight posts (shutdown, tests), then close the client."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
