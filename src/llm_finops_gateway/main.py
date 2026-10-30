"""FastAPI application.

Use the factory so tests can inject a price book, policies, key ring, ledger and provider:

    uvicorn --factory llm_finops_gateway.main:create_app
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import __version__
from .alerts import Alerter
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
    CompletionTokensDetails,
    CostSummary,
    CostSummaryRow,
    FinOpsEnvelope,
    PolicyEnvelope,
    PromptTokensDetails,
    TeamBudgetRow,
    ToolCall,
    Usage,
)
from .policy import (
    BudgetStatus,
    PolicyEngine,
    PolicySet,
    estimate_cost,
    estimate_tokens,
    month_start,
)
from .pricing import PriceBook, UnknownModelError, money
from .providers import Provider, ProviderError
from .providers.azure_identity import EntraTokenProvider
from .providers.azure_openai import AzureOpenAIProvider
from .providers.base import TokenUsage
from .providers.ollama import OllamaProvider
from .providers.openai_compat import OpenAICompatProvider
from .report import (
    COOKIE,
    ReportContext,
    ReportScope,
    login_error,
    render_login,
    render_report,
    resolve_report_scope,
    scope_for,
    summary_csv,
)
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
    entra = settings.azure_openai_auth == "entra"
    if not entra and (endpoint is None) != (key is None):
        raise RuntimeError(
            "LFG_AZURE_OPENAI_ENDPOINT and LFG_AZURE_OPENAI_API_KEY must be set together "
            "(or set LFG_AZURE_OPENAI_AUTH=entra)"
        )
    if endpoint is not None:
        providers[ProviderKind.AZURE_OPENAI.value] = AzureOpenAIProvider(
            endpoint=endpoint,
            api_key=None if entra else (key.get_secret_value() if key else None),
            token_provider=EntraTokenProvider() if entra else None,
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


_SSE_DONE = b"data: [DONE]\n\n"


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def _stream_chunk(
    request_id: str,
    created: int,
    model: str,
    delta: str | None,
    finish_reason: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One ``chat.completion.chunk`` in the OpenAI wire shape."""
    choice: dict[str, Any] = {"index": 0, "finish_reason": finish_reason}
    choice["delta"] = {"role": "assistant", "content": delta} if delta is not None else {}
    if tool_calls:
        choice["delta"]["tool_calls"] = tool_calls
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }


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
    alerter: Alerter | None = None,
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
    alerter = alerter or Alerter(
        settings.alert_webhook_url.get_secret_value() if settings.alert_webhook_url else None,
        cooldown_seconds=settings.alert_cooldown_seconds,
        currency=price_book.currency,
    )

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
            "gateway up providers=%s default=%s auth=%s price_book=%s policies=%s keys=%d "
            "otel=%s alerts=%s",
            ",".join(sorted(providers)),
            default_provider.name,
            settings.auth_mode,
            price_book.version,
            policies.version,
            len(keyring),
            telemetry.enabled,
            alerter.enabled,
        )
        try:
            yield
        finally:
            for p in providers.values():
                await p.aclose()
            await alerter.aclose()
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
    ) -> ChatResponse | StreamingResponse:
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
        # 1. Fail before spending: a model we cannot price must not be called (ADR-0002).
        if not book.has(req.model):
            metrics.requests.labels(attr.team, req.model, "unpriced").inc()
            raise HTTPException(status_code=422, detail=str(UnknownModelError(req.model)))

        # 2. Policy: allow-list, rate limit, atomic budget reservation (ADR-0005, 0007).
        messages = [m.model_dump(exclude_none=True) for m in req.messages]
        extra = req.passthrough()
        policy = engine.resolve(attr.team)
        reserve_output = policy.budget.reserve_output_tokens if policy.budget else 1
        tool_chars = len(json.dumps(req.tools)) if req.tools else 0
        estimate = estimate_cost(
            book, req.model, messages, req.output_limit, reserve_output, tool_chars
        )
        decision = engine.check(attr, req.model, estimate)
        if not decision.allowed:
            metrics.requests.labels(attr.team, req.model, decision.violation or "blocked").inc()
            telemetry.annotate(policy_violation=decision.violation or "")
            if decision.violation == "budget_exceeded":
                alerter.budget_block(attr.team, decision.budget, decision.message or "")
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

        if decision.warnings and decision.budget is not None:
            alerter.budget_warn(attr.team, decision.budget, decision.warnings[0])

        # 3. Upstream call; on failure give the reservation back.
        # Reasoning models (gpt-5, o-series) take max_completion_tokens and only the default
        # temperature; the client's values are translated so OpenAI SDK defaults keep working.
        reasoning = book.is_reasoning(req.model)
        gen_params: dict[str, Any] = {
            "temperature": None if reasoning else req.temperature,
            "max_tokens": None if reasoning else req.max_tokens,
            "max_completion_tokens": req.output_limit if reasoning else req.max_completion_tokens,
            "extra": extra,
        }
        request_id = f"gw-{uuid.uuid4().hex}"

        def settle(usage: TokenUsage) -> tuple[Decimal, BudgetStatus | None]:
            """Step 4 and 5: price, record and settle in one transaction, then read the
            budget state after this call so clients see where they stand now."""
            cost = book.cost(
                req.model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
            )
            ledger.record(
                CostEntry(
                    request_id=request_id,
                    team=attr.team,
                    project=attr.project,
                    use_case=attr.use_case,
                    provider=provider.name,
                    model=req.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cost_usd=cost,
                    price_book_version=book.version,
                    key_id=attr.key_id,
                ),
                release_reserved=decision.reserved_usd,
            )
            metrics.observe_cost(
                attr.team, attr.project, req.model, cost, usage.input_tokens, usage.output_tokens
            )
            outcome = "warned" if decision.warnings else "ok"
            metrics.requests.labels(attr.team, req.model, outcome).inc()
            telemetry.annotate(
                cost_usd=float(cost),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                request_id=request_id,
            )
            status = engine.budget_status(attr.team)
            if status is not None:
                metrics.budget_used.labels(attr.team).set(float(status.used_pct) / 100)
            return cost, status

        def on_upstream_failure(exc: BaseException) -> str:
            if decision.reserved_usd:
                ledger.release(attr.team, decision.reserved_usd)
            throttled = isinstance(exc, ProviderError) and exc.status_code == 429
            outcome = "upstream_rate_limited" if throttled else "upstream_error"
            metrics.requests.labels(attr.team, req.model, outcome).inc()
            alerter.upstream_failure(provider.name, outcome, req.model, str(exc))
            return outcome

        def envelope(cost: Decimal, status: BudgetStatus | None) -> FinOpsEnvelope:
            return FinOpsEnvelope(
                cost_usd=money(cost),
                currency=book.currency,
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
            )

        def usage_model(usage: TokenUsage) -> Usage:
            return Usage(
                prompt_tokens=usage.input_tokens,
                completion_tokens=usage.output_tokens,
                total_tokens=usage.input_tokens + usage.output_tokens,
                prompt_tokens_details=(
                    PromptTokensDetails(cached_tokens=usage.cached_input_tokens)
                    if usage.cached_input_tokens
                    else None
                ),
                completion_tokens_details=(
                    CompletionTokensDetails(reasoning_tokens=usage.reasoning_tokens)
                    if usage.reasoning_tokens
                    else None
                ),
            )

        if req.stream:
            # Streaming (ADR-0012): the policy decision above already holds; cost is settled
            # from the final usage chunk and travels in the last event, not in headers.
            async def events() -> AsyncIterator[bytes]:
                created = int(time.time())
                finish = "stop"
                usage: TokenUsage | None = None
                settled = False
                started = time.perf_counter()
                try:
                    async for part in provider.stream(req.model, messages, **gen_params):
                        if part.delta or part.tool_calls:
                            chunk = _stream_chunk(
                                request_id,
                                created,
                                req.model,
                                part.delta if part.delta else None,
                                None,
                                part.tool_calls,
                            )
                            yield _sse(chunk)
                        if part.finish_reason:
                            finish = part.finish_reason
                        if part.usage is not None:
                            usage = part.usage
                    metrics.upstream_latency.labels(provider.name, req.model).observe(
                        time.perf_counter() - started
                    )
                    estimated = usage is None
                    if usage is None:
                        est_in, est_out = estimate_tokens(
                            messages, req.output_limit, reserve_output, tool_chars
                        )
                        usage = TokenUsage(input_tokens=est_in, output_tokens=est_out)
                        log.warning(
                            "stream from %s ended without usage request_id=%s team=%s model=%s; "
                            "billing the estimate (%d in, %d out)",
                            provider.name,
                            request_id,
                            attr.team,
                            req.model,
                            est_in,
                            est_out,
                        )
                    cost, status = settle(usage)
                    settled = True
                except Exception as exc:
                    settled = True
                    outcome = on_upstream_failure(exc)
                    log.warning(
                        "stream failed request_id=%s team=%s model=%s: %s",
                        request_id,
                        attr.team,
                        req.model,
                        exc,
                    )
                    yield _sse(
                        {
                            "error": outcome,
                            "provider": provider.name,
                            "detail": str(exc),
                            "request_id": request_id,
                        }
                    )
                    yield _SSE_DONE
                    return
                finally:
                    if not settled and decision.reserved_usd:  # client went away mid-stream
                        ledger.release(attr.team, decision.reserved_usd)
                yield _sse(_stream_chunk(request_id, created, req.model, None, finish))
                final: dict[str, Any] = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [],
                    "usage": usage_model(usage).model_dump(),
                    "x_finops": envelope(cost, status).model_dump(),
                }
                if estimated:
                    final["usage_estimated"] = True
                yield _sse(final)
                yield _SSE_DONE

            return StreamingResponse(
                events(),
                media_type="text/event-stream",
                headers={"X-FinOps-Request-Id": request_id, "Cache-Control": "no-cache"},
            )

        started = time.perf_counter()
        try:
            result = await provider.chat(req.model, messages, **gen_params)
        except Exception as exc:
            on_upstream_failure(exc)
            raise
        metrics.upstream_latency.labels(provider.name, req.model).observe(
            time.perf_counter() - started
        )

        # 4. Price, record and settle the reservation in one transaction.
        cost, status = settle(
            TokenUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                reasoning_tokens=result.reasoning_tokens,
            )
        )
        _budget_headers(response, status, decision.warnings)
        response.headers["X-FinOps-Cost-USD"] = money(cost)
        response.headers["X-FinOps-Currency"] = book.currency
        response.headers["X-FinOps-Request-Id"] = request_id

        return ChatResponse(
            id=request_id,
            created=int(time.time()),
            model=req.model,
            choices=[
                Choice(
                    message=ChatMessage(
                        role="assistant",
                        content=result.content if result.content or not result.tool_calls else None,
                        tool_calls=(
                            [ToolCall.model_validate(tc) for tc in result.tool_calls]
                            if result.tool_calls
                            else None
                        ),
                    ),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=usage_model(
                TokenUsage(
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cached_input_tokens=result.cached_input_tokens,
                    reasoning_tokens=result.reasoning_tokens,
                )
            ),
            x_finops=envelope(cost, status),
        )

    def cost_summary(team: str | None, project: str | None) -> CostSummary:
        rows = ledger.summary(team=team, project=project)
        return CostSummary(
            currency=price_book.currency,
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

    def budget_rows(team: str | None) -> list[TeamBudgetRow]:
        names = set(policies.teams()) | {r.team for r in ledger.summary()}
        if team is not None:
            names = {team}
        rows = []
        for name in sorted(names):
            effective = policies.resolve(name)
            status = engine.budget_status(name)
            if status is not None:
                metrics.budget_used.labels(name).set(float(status.used_pct) / 100)
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
        return rows

    # Reports (ADR-0016): a team key reads its own team, a finops key every team.
    @app.get("/v1/finops/summary", response_model=CostSummary)
    async def finops_summary(
        scope: Annotated[ReportScope, Depends(resolve_report_scope)],
        team: str | None = None,
        project: str | None = None,
    ) -> CostSummary:
        return cost_summary(scope.narrow(team), project)

    @app.get("/v1/finops/summary.csv", include_in_schema=True)
    async def finops_summary_csv(
        scope: Annotated[ReportScope, Depends(resolve_report_scope)],
        team: str | None = None,
        project: str | None = None,
    ) -> Response:
        summary = cost_summary(scope.narrow(team), project)
        return Response(
            content=summary_csv(summary.rows, summary.currency),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="llm-finops-summary.csv"'},
        )

    @app.get("/v1/finops/budgets", response_model=BudgetReport)
    async def finops_budgets(
        scope: Annotated[ReportScope, Depends(resolve_report_scope)],
    ) -> BudgetReport:
        return BudgetReport(
            policy_version=engine.version,
            month_start=month_start().isoformat(),
            teams=budget_rows(scope.team),
        )

    @app.get("/finops", response_class=HTMLResponse, include_in_schema=False)
    async def finops_page(request: Request, team: str | None = None) -> HTMLResponse:
        try:
            scope = scope_for(
                request, request.headers.get("authorization"), request.headers.get("x-team")
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(render_login())
            raise
        team_filter = scope.narrow(team)
        summary = cost_summary(team_filter, None)
        ctx = ReportContext(
            version=__version__,
            price_book_version=price_book.version,
            currency=price_book.currency,
            policy_version=engine.version,
            providers=",".join(sorted(providers)),
            month_start=month_start().date().isoformat(),
            scope=scope,
            team_filter=team if scope.team is None else None,
            budgets=budget_rows(team_filter),
            rows=summary.rows,
            total=summary.total_cost_usd,
        )
        return HTMLResponse(render_report(ctx))

    @app.post("/finops/login", include_in_schema=False)
    async def finops_login(request: Request, key: Annotated[str, Form()]) -> Response:
        try:
            scope_for(request, f"Bearer {key}", None)
        except HTTPException as exc:
            return HTMLResponse(render_login(login_error(exc)))
        response = RedirectResponse("/finops", status_code=303)
        response.set_cookie(COOKIE, key, httponly=True, samesite="strict", secure=False)
        return response

    @app.get("/finops/logout", include_in_schema=False)
    async def finops_logout() -> Response:
        response = RedirectResponse("/finops", status_code=303)
        response.delete_cookie(COOKIE)
        return response

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
