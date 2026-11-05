"""The gateway's FinOps rules as a LiteLLM proxy plugin (ADR-0019).

Register it in the proxy config and point the ``LFG_*`` settings at the same price book,
policy file and ledger database the gateway uses::

    litellm_settings:
      callbacks: llm_finops_gateway.litellm_plugin.plugin

Per chat call the plugin runs the same five steps as the gateway: attribution (team from the
LiteLLM key's team alias, project and use case from ``X-Project`` and ``X-Use-Case``), price
book lookup (unknown model: 422 before the call), policy with an atomic budget reservation
(403 or 429 before the call), then settlement of the real usage into the shared ledger and
the ``X-FinOps-*`` response headers. The response body is left untouched.

Settlement happens once per request, in whichever hook sees the usage first: the response
headers hook for non-streaming calls (so the cost header can be set), the success log event
for streamed calls. The failure event releases the reservation.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from .attribution import Attribution, attribution_from_headers
from .config import Settings
from .enforcement import budget_headers, cost_headers, rejection
from .ledger import CostEntry, Ledger
from .policy import BudgetStatus, PolicyEngine, PolicySet, estimate_cost, project_scope
from .pricing import PriceBook, UnknownModelError
from .providers.base import TokenUsage
from .reload import WatchedFile

log = logging.getLogger(__name__)

META = "finops"  # our one key inside LiteLLM's request metadata
CHAT_CALL_TYPES = frozenset({"completion", "acompletion"})
PENDING_MAX_AGE_S = 3600.0


@dataclass
class _Pending:
    """A reservation waiting for its usage."""

    request_id: str
    attr: Attribution
    model: str
    reserved_usd: Decimal
    warnings: list[str] = field(default_factory=list)
    project_reserved_usd: Decimal | None = None  # None: the project has no budget of its own
    created: float = field(default_factory=time.monotonic)


def _usage_of(obj: Any) -> TokenUsage | None:
    """Token usage from a LiteLLM response object or a usage mapping, if it has one."""
    usage = getattr(obj, "usage", None) if not isinstance(obj, dict) else obj.get("usage")
    if usage is None:
        return None

    def get(o: Any, name: str) -> Any:
        return o.get(name) if isinstance(o, dict) else getattr(o, name, None)

    prompt = get(usage, "prompt_tokens")
    completion = get(usage, "completion_tokens")
    if prompt is None or completion is None:
        return None
    details = get(usage, "prompt_tokens_details")
    cached = get(details, "cached_tokens") if details is not None else None
    cdetails = get(usage, "completion_tokens_details")
    reasoning = get(cdetails, "reasoning_tokens") if cdetails is not None else None
    return TokenUsage(
        input_tokens=int(prompt),
        output_tokens=int(completion),
        cached_input_tokens=int(cached or 0),
        reasoning_tokens=int(reasoning or 0),
    )


class FinOpsPlugin(CustomLogger):
    """LiteLLM callback that enforces and settles the gateway's FinOps policy."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        price_book: PriceBook | None = None,
        policies: PolicySet | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._book = price_book
        self._policies = policies
        self._ledger = ledger
        self._engine: PolicyEngine | None = None
        self._pending: dict[str, _Pending] = {}
        self._book_watch: WatchedFile[PriceBook] | None = None
        self._policy_watch: WatchedFile[PolicySet] | None = None

    # ---- wiring ---------------------------------------------------------------------

    def _ensure(self) -> tuple[PriceBook, PolicyEngine, Ledger]:
        """Build the price book, policy engine and ledger on first use, from LFG_* settings
        unless they were injected."""
        if self._engine is None:
            settings = self._settings or Settings()
            if self._book is None:
                self._book_watch = WatchedFile(
                    settings.pricing_path, PriceBook.from_yaml, lambda b: f"version {b.version}"
                )
                self._book = self._book_watch.value
            if self._policies is None:
                if settings.policies_path.exists():
                    self._policy_watch = WatchedFile(
                        settings.policies_path,
                        PolicySet.from_yaml,
                        lambda p: f"version {p.version}",
                    )
                    self._policies = self._policy_watch.value
                else:
                    self._policies = PolicySet.empty()
            if self._ledger is None:
                self._ledger = Ledger(settings.database_url)
            self._engine = PolicyEngine(self._policies, self._ledger)
            log.info(
                "finops plugin ready: price book %s (%s), policies %s, %d models",
                self._book.version,
                self._book.currency,
                self._policies.version,
                len(self._book.models()),
            )
        assert self._book is not None and self._ledger is not None
        # Hot reload (files only; injected objects are the caller's business).
        if self._book_watch is not None:
            self._book = self._book_watch.get()
        if self._policy_watch is not None:
            policies = self._policy_watch.get()
            if policies is not self._policies:
                self._policies = policies
                self._engine.replace(policies)
        return self._book, self._engine, self._ledger

    def _sweep(self, ledger: Ledger) -> None:
        """Release reservations of requests that never reported back."""
        now = time.monotonic()
        for rid in [r for r, p in self._pending.items() if now - p.created > PENDING_MAX_AGE_S]:
            stale = self._pending.pop(rid)
            self._release(stale, ledger)
            log.warning("released stale reservation request_id=%s team=%s", rid, stale.attr.team)

    # ---- step 1 to 3: before the upstream call ---------------------------------------

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any] | None:
        if call_type not in CHAT_CALL_TYPES:
            return data
        book, engine, ledger = self._ensure()
        self._sweep(ledger)

        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        team = getattr(user_api_key_dict, "team_alias", None)
        if not team:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_attribution",
                    "missing": ["team"],
                    "hint": "The LiteLLM key belongs to no team; create keys with a team_id "
                    "whose team_alias matches a team in policies.yaml.",
                },
            )
        if not headers.get("x-project"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_attribution",
                    "missing_headers": ["X-Project"],
                    "hint": "Set X-Project so the call can be charged back; the team comes "
                    "from the LiteLLM key.",
                },
            )
        base = attribution_from_headers(team, headers.get("x-project"), headers.get("x-use-case"))
        attr = Attribution(
            team=base.team,
            project=base.project,
            use_case=base.use_case,
            key_id=getattr(user_api_key_dict, "key_alias", None),
        )

        model = str(data.get("model") or "")
        if not book.has(model):
            raise HTTPException(status_code=422, detail=str(UnknownModelError(model)))

        messages = data.get("messages") or []
        policy = engine.resolve(attr.team)
        reserve_output = policy.budget.reserve_output_tokens if policy.budget else 1
        max_tokens = data.get("max_completion_tokens") or data.get("max_tokens")
        tool_chars = len(json.dumps(data["tools"])) if data.get("tools") else 0
        estimate = estimate_cost(book, model, messages, max_tokens, reserve_output, tool_chars)
        decision = engine.check(attr, model, estimate)
        if not decision.allowed:
            status_code, detail, extra = rejection(decision, attr, model, engine.version)
            raise HTTPException(status_code=status_code, detail=detail, headers=extra)

        request_id = f"lt-{uuid.uuid4().hex}"
        self._pending[request_id] = _Pending(
            request_id,
            attr,
            model,
            decision.reserved_usd,
            list(decision.warnings),
            decision.project_reserved_usd if decision.project_budget is not None else None,
        )
        data.setdefault("metadata", {})[META] = {"request_id": request_id, "team": attr.team}
        return data

    @staticmethod
    def _release(pending: _Pending, ledger: Ledger) -> None:
        if pending.reserved_usd:
            ledger.release(pending.attr.team, pending.reserved_usd)
        if pending.project_reserved_usd:
            ledger.release(
                project_scope(pending.attr.team, pending.attr.project),
                pending.project_reserved_usd,
            )

    # ---- step 4 and 5: settle, headers ------------------------------------------------

    def _settle(
        self, pending: _Pending, usage: TokenUsage, provider: str, litellm_cost: Any = None
    ) -> tuple[Decimal, BudgetStatus | None]:
        book, engine, ledger = self._ensure()
        cost = book.cost(
            pending.model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
        )
        ledger.record(
            CostEntry(
                request_id=pending.request_id,
                team=pending.attr.team,
                project=pending.attr.project,
                use_case=pending.attr.use_case,
                provider=provider,
                model=pending.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cost_usd=cost,
                price_book_version=book.version,
                key_id=pending.attr.key_id,
            ),
            release_reserved=pending.reserved_usd,
            project_reserved=pending.project_reserved_usd,
        )
        log.info(
            "settled request_id=%s team=%s project=%s model=%s cost=%s litellm_cost=%s",
            pending.request_id,
            pending.attr.team,
            pending.attr.project,
            pending.model,
            cost,
            litellm_cost,
        )
        return cost, engine.budget_status(pending.attr.team)

    def _project_status(self, pending: _Pending) -> BudgetStatus | None:
        return self._ensure()[1].project_budget_status(pending.attr.team, pending.attr.project)

    @staticmethod
    def _meta(container: dict[str, Any] | None) -> dict[str, Any] | None:
        meta = (container or {}).get("metadata") or {}
        found = meta.get(META)
        return found if isinstance(found, dict) else None

    async def async_post_call_response_headers_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        meta = self._meta(data)
        if meta is None:
            return None
        book, engine, _ = self._ensure()
        request_id = str(meta["request_id"])
        headers = {"X-FinOps-Request-Id": request_id, "X-FinOps-Currency": book.currency}
        pending = self._pending.get(request_id)
        usage = _usage_of(response)
        if pending is not None and usage is not None:
            # Non-streaming: the usage is here, settle now so the cost travels in the headers.
            self._pending.pop(request_id, None)
            hidden = getattr(response, "_hidden_params", None) or {}
            provider = str(hidden.get("custom_llm_provider") or "litellm")
            cost, status = self._settle(pending, usage, provider)
            headers.update(cost_headers(request_id, cost, book.currency))
            headers.update(budget_headers(status, pending.warnings, self._project_status(pending)))
        else:
            # Streaming: the cost is settled from the success event once the stream ends.
            warnings = pending.warnings if pending is not None else []
            project_status = self._project_status(pending) if pending is not None else None
            headers.update(
                budget_headers(engine.budget_status(str(meta["team"])), warnings, project_status)
            )
        return headers

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        meta = self._meta(kwargs.get("litellm_params"))
        if meta is None:
            return
        pending = self._pending.pop(str(meta["request_id"]), None)
        if pending is None:
            return  # settled in the headers hook
        slo = kwargs.get("standard_logging_object") or {}
        usage = _usage_of(response_obj)
        if usage is None and slo.get("prompt_tokens") is not None:
            usage = TokenUsage(
                input_tokens=int(slo.get("prompt_tokens") or 0),
                output_tokens=int(slo.get("completion_tokens") or 0),
            )
        if usage is None:
            log.warning("no usage for request_id=%s; releasing the reservation", pending.request_id)
            self._release(pending, self._ensure()[2])
            return
        provider = str(slo.get("custom_llm_provider") or "litellm")
        self._settle(pending, usage, provider, kwargs.get("response_cost"))

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        meta = self._meta(kwargs.get("litellm_params"))
        if meta is None:
            return
        pending = self._pending.pop(str(meta["request_id"]), None)
        if pending is not None:
            self._release(pending, self._ensure()[2])
            log.info("released reservation after failure request_id=%s", pending.request_id)


plugin = FinOpsPlugin()
