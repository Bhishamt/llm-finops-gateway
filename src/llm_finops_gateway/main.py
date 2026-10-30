"""FastAPI application.

Use the factory so tests can inject a price book, policies, key ring, ledger and provider:

    uvicorn --factory llm_finops_gateway.main:create_app
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .attribution import Attribution, resolve_attribution
from .auth import KeyRing
from .config import ProviderKind, Settings
from .ledger import CostEntry, Ledger
from .metrics import CONTENT_TYPE, Metrics
from .models import (
    BudgetEnvelope,
    BudgetReport,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    CostSummary,
    CostSummaryRow,
    FinOpsEnvelope,
    PolicyEnvelope,
    PromptTokensDetails,
    TeamBudgetRow,
    Usage,
)
from .policy import BudgetStatus, PolicyEngine, PolicySet, estimate_cost, month_start
from .pricing import PriceBook, UnknownModelError, money
from .providers import Provider, ProviderError
from .providers.azure_openai import AzureOpenAIProvider
from .providers.ollama import OllamaProvider
from .providers.openai_compat import OpenAICompatProvider
from .telemetry import Telemetry

log = logging.getLogger("llm_finops_gateway")


def build_providers(settings: Settings) -> dict[str, Provider]:
    """Every provider the settings describe, keyed by name (ADR-0011). Ollama needs no
    credentials and is always present; the others are added when configured."""
    providers: dict[str, Provider] = {
        ProviderKind.OLLAMA.value: OllamaProvider(
            base_url=settings.ollama_base_url, timeout=settings.request_timeout_seconds
        )
    }
    endpoint, key = settings.azure_openai_endpoint, settings.azure_openai_api_key
    if (endpoint is None) != (key is None):
        raise RuntimeError(
            "LFG_AZURE_OPENAI_ENDPOINT and LFG_AZURE_OPENAI_API_KEY must be set together"
        )
    if endpoint is not None and key is not None:
        providers[ProviderKind.AZURE_OPENAI.value] = AzureOpenAIProvider(
            endpoint=endpoint,
            api_key=key.get_secret_value(),
            api_version=settings.azure_openai_api_version,
            deployments=settings.azure_openai_deployments,
            timeout=settings.request_timeout_seconds,
        )
    if settings.openai_compat_base_url is not None:
        compat_key = settings.openai_compat_api_key
        providers[ProviderKind.OPENAI_COMPAT.value] = OpenAICompatProvider(
            base_url=settings.openai_compat_base_url,
            api_key=compat_key.get_secret_value() if compat_key else None,
            headers=settings.openai_compat_headers,
            models=settings.openai_compat_models,
            timeout=settings.request_timeout_seconds,
        )
    return providers


def build_provider(settings: Settings) -> Provider:
    """The default provider (``LFG_PROVIDER``)."""
    return build_providers(settings)[settings.provider.value]


def route_models(
    book: PriceBook, providers: dict[str, Provider], default: Provider
) -> dict[str, Provider]:
    """Resolve each price book row to the provider that serves it (ADR-0011)."""
    routes: dict[str, Provider] = {}
    for model in book.models():
        name = book.provider_for(model)
        if name is not None and name not in providers:
            log.warning(
                "price book routes model %s to provider '%s', which is not configured; "
                "using default '%s'",
                model,
                name,
                default.name,
            )
        routes[model] = providers[name] if name in providers else default
    return routes


def load_policies(settings: Settings) -> PolicySet:
    if settings.policies_path.exists():
        return PolicySet.from_yaml(settings.policies_path)
    log.warning(
        "no policy file at %s; running without budgets or allow-lists", settings.policies_path
    )
    return PolicySet.empty()


def load_keyring(settings: Settings) -> KeyRing:
    if settings.auth_mode == "headers":
        log.warning("auth_mode=headers: X-Team is trusted as declared; not for production")
        return KeyRing.empty()
    if not settings.keys_path.exists():
        raise RuntimeError(
            f"auth_mode=api_key but no key file at {settings.keys_path}. "
            "Create keys with 'llm-finops-gateway keys new --team <team>' "
            "or set LFG_AUTH_MODE=headers for local development."
        )
    ring = KeyRing.from_yaml(settings.keys_path)
    if len(ring) == 0:
        log.warning("key file %s has no keys; every request will get 401", settings.keys_path)
    return ring


def _state(request: Request) -> Any:
    return request.app.state


def _budget_envelope(status: BudgetStatus | None) -> BudgetEnvelope | None:
    return BudgetEnvelope(**status.as_dict()) if status else None


def _budget_headers(response: Response, status: BudgetStatus | None, warnings: list[str]) -> None:
    if status is not None:
        response.headers["X-FinOps-Budget-Limit-USD"] = money(status.limit_usd)
        response.headers["X-FinOps-Budget-Spent-USD"] = money(status.spent_usd)
        response.headers["X-FinOps-Budget-Used-Pct"] = str(status.used_pct)
        response.headers["X-FinOps-Budget-Status"] = status.level
    if warnings:
        response.headers["X-FinOps-Warning"] = " | ".join(warnings)


def create_app(
    settings: Settings | None = None,
    *,
    price_book: PriceBook | None = None,
    policies: PolicySet | None = None,
    keyring: KeyRing | None = None,
    ledger: Ledger | None = None,
    provider: Provider | None = None,
    providers: dict[str, Provider] | None = None,
) -> FastAPI:
    """``provider`` is the default provider; ``providers`` adds further ones by name. With
    neither, every provider the settings describe is built and ``LFG_PROVIDER`` is the default."""
    settings = settings or Settings()
    price_book = price_book or PriceBook.from_yaml(settings.pricing_path)
    policies = policies or load_policies(settings)
    keyring = keyring or load_keyring(settings)
    ledger = ledger or Ledger(settings.database_url)
    providers = dict(providers or {})
    if provider is not None:
        providers.setdefault(provider.name, provider)
    if not providers:
        providers = build_providers(settings)
    default_name = provider.name if provider is not None else settings.provider.value
    if default_name not in providers:
        raise RuntimeError(
            f"default provider '{default_name}' is not configured; have {sorted(providers)}"
        )
    default_provider = providers[default_name]
    routes = route_models(price_book, providers, default_provider)
    engine = PolicyEngine(policies, ledger)
    metrics = Metrics()
    telemetry = Telemetry(settings.otel_enabled, settings.otel_service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.price_book = price_book
        app.state.policies = policies
        app.state.policy_engine = engine
        app.state.keyring = keyring
        app.state.ledger = ledger
        app.state.provider = default_provider
        app.state.providers = providers
        app.state.routes = routes
        app.state.metrics = metrics
        app.state.telemetry = telemetry
        log.info(
            "gateway up providers=%s default=%s auth=%s price_book=%s policies=%s keys=%d otel=%s",
            ",".join(sorted(providers)),
            default_provider.name,
            settings.auth_mode,
            price_book.version,
            policies.version,
            len(keyring),
            telemetry.enabled,
        )
        try:
            yield
        finally:
            for p in providers.values():
                await p.aclose()
            telemetry.shutdown()

    app = FastAPI(
        title="llm-finops-gateway",
        version=__version__,
        summary="OpenAI-compatible gateway with cost attribution and policy guardrails.",
        lifespan=lifespan,
    )
    telemetry.instrument(app)

    @app.exception_handler(ProviderError)
    async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        if exc.status_code == 429:
            # The provider is throttling us: tell the client to back off, not that we are broken.
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": exc.retry_after or "1"},
                content={
                    "error": "upstream_rate_limited",
                    "provider": exc.provider,
                    "detail": str(exc),
                },
            )
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_error", "provider": exc.provider, "detail": str(exc)},
        )

    @app.post("/v1/chat/completions", response_model=ChatResponse)
    async def chat_completions(
        req: ChatRequest,
        response: Response,
        attr: Annotated[Attribution, Depends(resolve_attribution)],
        state: Annotated[Any, Depends(_state)],
    ) -> ChatResponse:
        book: PriceBook = state.price_book
        engine: PolicyEngine = state.policy_engine
        ledger: Ledger = state.ledger
        provider: Provider = state.routes.get(req.model, state.provider)
        metrics: Metrics = state.metrics
        telemetry: Telemetry = state.telemetry

        telemetry.annotate(
            team=attr.team,
            project=attr.project,
            use_case=attr.use_case,
            model=req.model,
            key_id=attr.key_id or "",
        )
        if req.stream:
            raise HTTPException(status_code=400, detail="stream=true is not supported yet")

        # 1. Fail before spending: a model we cannot price must not be called (ADR-0002).
        if not book.has(req.model):
            metrics.requests.labels(attr.team, req.model, "unpriced").inc()
            raise HTTPException(status_code=422, detail=str(UnknownModelError(req.model)))

        # 2. Policy: allow-list, rate limit, atomic budget reservation (ADR-0005, 0007).
        messages = [m.model_dump() for m in req.messages]
        policy = engine.resolve(attr.team)
        estimate = estimate_cost(
            book,
            req.model,
            messages,
            req.max_tokens,
            policy.budget.reserve_output_tokens if policy.budget else 1,
        )
        decision = engine.check(attr, req.model, estimate)
        if not decision.allowed:
            metrics.requests.labels(attr.team, req.model, decision.violation or "blocked").inc()
            telemetry.annotate(policy_violation=decision.violation or "")
            detail: dict[str, Any] = {
                "error": decision.violation,
                "message": decision.message,
                "team": attr.team,
                "project": attr.project,
                "model": req.model,
                "policy_version": engine.version,
            }
            headers: dict[str, str] = {}
            status_code = 403
            if decision.budget is not None:
                detail["budget"] = decision.budget.as_dict()
            if decision.violation == "rate_limited":
                status_code = 429
                headers["Retry-After"] = str(decision.retry_after_seconds or 1)
            raise HTTPException(status_code=status_code, detail=detail, headers=headers)

        # 3. Upstream call; on failure give the reservation back.
        started = time.perf_counter()
        try:
            result = await provider.chat(
                req.model, messages, temperature=req.temperature, max_tokens=req.max_tokens
            )
        except Exception as exc:
            if decision.reserved_usd:
                ledger.release(attr.team, decision.reserved_usd)
            throttled = isinstance(exc, ProviderError) and exc.status_code == 429
            outcome = "upstream_rate_limited" if throttled else "upstream_error"
            metrics.requests.labels(attr.team, req.model, outcome).inc()
            raise
        metrics.upstream_latency.labels(provider.name, req.model).observe(
            time.perf_counter() - started
        )

        # 4. Price, record and settle the reservation in one transaction.
        cost = book.cost(
            req.model, result.input_tokens, result.output_tokens, result.cached_input_tokens
        )
        request_id = f"gw-{uuid.uuid4().hex}"
        ledger.record(
            CostEntry(
                request_id=request_id,
                team=attr.team,
                project=attr.project,
                use_case=attr.use_case,
                provider=provider.name,
                model=req.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                cost_usd=cost,
                price_book_version=book.version,
                key_id=attr.key_id,
            ),
            release_reserved=decision.reserved_usd,
        )
        metrics.observe_cost(
            attr.team, attr.project, req.model, cost, result.input_tokens, result.output_tokens
        )
        metrics.requests.labels(attr.team, req.model, "warned" if decision.warnings else "ok").inc()
        telemetry.annotate(
            cost_usd=float(cost),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            request_id=request_id,
        )

        # 5. Budget state after this call, so clients see where they stand now.
        status = engine.budget_status(attr.team)
        if status is not None:
            metrics.budget_used.labels(attr.team).set(float(status.used_pct) / 100)
        _budget_headers(response, status, decision.warnings)
        response.headers["X-FinOps-Cost-USD"] = money(cost)
        response.headers["X-FinOps-Request-Id"] = request_id

        return ChatResponse(
            id=request_id,
            created=int(time.time()),
            model=req.model,
            choices=[
                Choice(
                    message=ChatMessage(role="assistant", content=result.content),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=Usage(
                prompt_tokens=result.input_tokens,
                completion_tokens=result.output_tokens,
                total_tokens=result.input_tokens + result.output_tokens,
                prompt_tokens_details=(
                    PromptTokensDetails(cached_tokens=result.cached_input_tokens)
                    if result.cached_input_tokens
                    else None
                ),
            ),
            x_finops=FinOpsEnvelope(
                cost_usd=money(cost),
                price_book_version=book.version,
                team=attr.team,
                project=attr.project,
                use_case=attr.use_case,
                provider=provider.name,
                key_id=attr.key_id,
                policy=PolicyEnvelope(
                    version=engine.version,
                    budget=_budget_envelope(status),
                    warnings=decision.warnings,
                ),
            ),
        )

    @app.get("/v1/finops/summary", response_model=CostSummary)
    async def finops_summary(
        state: Annotated[Any, Depends(_state)],
        team: str | None = None,
        project: str | None = None,
    ) -> CostSummary:
        ledger: Ledger = state.ledger
        book: PriceBook = state.price_book
        rows = ledger.summary(team=team, project=project)
        return CostSummary(
            currency=book.currency,
            rows=[
                CostSummaryRow(
                    team=r.team,
                    project=r.project,
                    model=r.model,
                    calls=r.calls,
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                    cost_usd=money(r.cost_usd),
                )
                for r in rows
            ],
            total_cost_usd=money(sum((r.cost_usd for r in rows), start=Decimal(0))),
        )

    @app.get("/v1/finops/budgets", response_model=BudgetReport)
    async def finops_budgets(state: Annotated[Any, Depends(_state)]) -> BudgetReport:
        engine: PolicyEngine = state.policy_engine
        ledger: Ledger = state.ledger
        policies: PolicySet = state.policies
        names = set(policies.teams()) | {r.team for r in ledger.summary()}
        rows = []
        for name in sorted(names):
            effective = policies.resolve(name)
            status = engine.budget_status(name)
            if status is not None:
                state.metrics.budget_used.labels(name).set(float(status.used_pct) / 100)
            rows.append(
                TeamBudgetRow(
                    team=name,
                    allowed_models=effective.allowed_models,
                    requests_per_minute=(
                        effective.rate_limit.requests_per_minute if effective.rate_limit else None
                    ),
                    budget=_budget_envelope(status),
                )
            )
        return BudgetReport(
            policy_version=engine.version, month_start=month_start().isoformat(), teams=rows
        )

    @app.get("/v1/models")
    async def list_models(state: Annotated[Any, Depends(_state)]) -> dict[str, object]:
        book: PriceBook = state.price_book
        routes: dict[str, Provider] = state.routes
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "owned_by": routes[m].name} for m in book.models()
            ],
        }

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(state: Annotated[Any, Depends(_state)]) -> Response:
        if not state.settings.metrics_enabled:
            raise HTTPException(status_code=404)
        return Response(content=state.metrics.render(), media_type=CONTENT_TYPE)

    @app.get("/healthz")
    async def healthz(state: Annotated[Any, Depends(_state)]) -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "auth_mode": state.settings.auth_mode,
            "providers": ",".join(sorted(state.providers)),
            "default_provider": state.provider.name,
            "price_book": state.price_book.version,
            "policies": state.policy_engine.version,
        }

    return app
