"""Traffic simulation for llm-finops-gateway.

Two modes:

* ``inprocess`` (default): spins the gateway up inside this process with a simulated provider
  (no Ollama, no Azure, no network) and the demo policies in ``demo/policies.yaml``. Shows the
  full ok -> warn -> block lifecycle in a few seconds.
* ``live``: sends the same traffic pattern to a running gateway (``--url``) with whatever
  provider and policies it has configured. Costs real money on Azure OpenAI; keep ``--calls``
  small.

Writes ``demo/out/summary.json``, ``demo/out/report.md`` and, when matplotlib is installed,
``demo/out/chargeback.png``.

    python demo/run_demo.py
    python demo/run_demo.py --mode live --url http://localhost:8080 --model gpt-4o-mini --calls 30
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "out"

PROMPTS = [
    "Summarise this support ticket in one sentence.",
    "Draft a polite reply declining the refund request.",
    "Extract the invoice number and total from the text below.",
    "Classify the sentiment of this review as positive, neutral or negative.",
    "Generate three subject lines for a product launch email.",
    "Explain what FinOps is to a software engineer in two sentences.",
]

# (team, project, use_case, model, weight). Shadow-it uses the defaults; one team asks for a
# model it is not allowed to use; one asks for a model the price book does not know.
TRAFFIC: list[tuple[str, str, str, str, int]] = [
    ("customer-support", "ticket-triage", "summarise", "gpt-4o-mini", 10),
    ("customer-support", "ticket-triage", "reply-draft", "gpt-4o-mini", 6),
    ("customer-support", "ticket-triage", "escalation", "gpt-4o", 1),  # not allowed
    ("platform", "copilot", "code-review", "gpt-4o", 5),
    ("platform", "copilot", "docs", "gpt-4o-mini", 5),
    ("platform", "copilot", "eval", "llama3.1:8b", 4),
    ("research", "rag-eval", "experiment", "gpt-4o", 6),
    ("research", "rag-eval", "baseline", "llama3.1:8b", 3),
    ("marketing", "campaign-q4", "copy", "gpt-4o-mini", 6),
    ("marketing", "campaign-q4", "copy", "gpt-5-preview", 1),  # not in price book
    ("shadow-it", "unknown", "unspecified", "gpt-4o-mini", 4),
]


@dataclass
class Outcome:
    ok: int = 0
    warned: int = 0
    blocked_budget: int = 0
    blocked_model: int = 0
    rate_limited: int = 0
    unpriced: int = 0
    unattributed: int = 0
    cost_usd: Decimal = Decimal(0)
    models: Counter[str] = field(default_factory=Counter)
    model_cost: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))


class SimulatedProvider:
    """Looks like an LLM, costs nothing, returns plausible token counts."""

    name = "simulated"

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> Any:
        from llm_finops_gateway.providers.base import CompletionResult

        prompt_len = sum(len(m["content"]) for m in messages)
        return CompletionResult(
            content=f"[simulated {model}] {messages[-1]['content'][:40]}...",
            input_tokens=max(50, prompt_len // 4 + self._rng.randint(400, 1400)),
            output_tokens=self._rng.randint(80, 600),
        )

    async def aclose(self) -> None:
        return None


def build_inprocess_client(seed: int) -> Any:
    from fastapi.testclient import TestClient

    from llm_finops_gateway.auth import KeyRing
    from llm_finops_gateway.config import ProviderKind, Settings
    from llm_finops_gateway.ledger import Ledger
    from llm_finops_gateway.main import create_app
    from llm_finops_gateway.policy import PolicySet
    from llm_finops_gateway.pricing import PriceBook

    db = Path(tempfile.mkdtemp(prefix="lfg-demo-")) / "demo.db"
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=ROOT / "config" / "pricing.yaml",
        policies_path=ROOT / "demo" / "policies.yaml",
        database_url=f"sqlite:///{db}",
    )
    simulated = SimulatedProvider(seed)
    app = create_app(
        settings,
        price_book=PriceBook.from_yaml(settings.pricing_path),
        policies=PolicySet.from_yaml(settings.policies_path),
        keyring=KeyRing.empty(),
        ledger=Ledger(settings.database_url),
        provider=simulated,
        # the shipped price book routes by provider name; the simulation plays all of them
        providers={"azure_openai": simulated, "ollama": simulated},
    )
    return TestClient(app)


def run_traffic(
    client: Any, calls: int, seed: int, model_override: str | None, api_key: str | None = None
) -> dict[str, Outcome]:
    rng = random.Random(seed)
    outcomes: dict[str, Outcome] = defaultdict(Outcome)
    weights = [t[4] for t in TRAFFIC]
    for i in range(calls):
        team, project, use_case, model, _ = rng.choices(TRAFFIC, weights=weights)[0]
        if model_override and model not in ("gpt-5-preview",):
            model = model_override
        body = {"model": model, "messages": [{"role": "user", "content": rng.choice(PROMPTS)}]}
        headers = {"X-Project": project, "X-Use-Case": use_case}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif team != "shadow-it" or rng.random() < 0.5:
            headers["X-Team"] = team
        r = client.post("/v1/chat/completions", json=body, headers=headers)
        o = outcomes[team]
        if r.status_code == 200:
            x = r.json()["x_finops"]
            cost = Decimal(x["cost_usd"])
            o.cost_usd += cost
            o.models[model] += 1
            o.model_cost[model] += cost
            if r.headers.get("X-FinOps-Budget-Status") == "warn":
                o.warned += 1
            else:
                o.ok += 1
        elif r.status_code == 403:
            err = r.json()["detail"]["error"]
            if err == "budget_exceeded":
                o.blocked_budget += 1
            else:
                o.blocked_model += 1
        elif r.status_code == 429:
            o.rate_limited += 1
        elif r.status_code == 422:
            o.unpriced += 1
        elif r.status_code == 400:
            o.unattributed += 1
        else:
            print(f"unexpected {r.status_code}: {r.text[:120]}", file=sys.stderr)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{calls} calls sent", file=sys.stderr)
    return outcomes


def write_report(outcomes: dict[str, Outcome], budgets: dict[str, Any], mode: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    teams = sorted(outcomes)
    total_cost = sum((o.cost_usd for o in outcomes.values()), Decimal(0))
    rejected = sum(
        o.blocked_budget + o.blocked_model + o.rate_limited + o.unpriced + o.unattributed
        for o in outcomes.values()
    )
    sent = sum(o.ok + o.warned for o in outcomes.values()) + rejected

    lines = [
        f"# Demo report ({mode})",
        "",
        f"Calls sent: **{sent}**, served: **{sent - rejected}**, rejected: **{rejected}**. "
        f"Total attributed spend: **{total_cost:.4f} USD**. Unallocated spend: **0.00 USD**.",
        "",
        "| team | ok | warned | blocked (budget) | blocked (model) | rate limited | "
        "unpriced model | no owner | spend USD | budget used |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for t in teams:
        o = outcomes[t]
        b = budgets.get(t)
        used = f"{b['used_pct']}% ({b['level']})" if b else "n/a"
        lines.append(
            f"| {t} | {o.ok} | {o.warned} | {o.blocked_budget} | {o.blocked_model} | "
            f"{o.rate_limited} | {o.unpriced} | {o.unattributed} | {o.cost_usd:.4f} | {used} |"
        )
    lines += [
        "",
        "What the table shows:",
        "",
        "- Every served call has a team, project and use case. Calls without an owner were "
        "rejected (the `no owner` column), so there is no unallocated bucket.",
        "- Teams that crossed their warn threshold kept being served with a warning header; "
        "teams that crossed the block threshold were stopped with HTTP 403.",
        "- A team asking for a model outside its allow-list, or for a model the price book does "
        "not know, was rejected before any provider call. Bursts over a team's requests-per-"
        "minute limit got HTTP 429 with Retry-After.",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "teams": {
                    t: {
                        **{
                            k: v
                            for k, v in outcomes[t].__dict__.items()
                            if k not in ("models", "model_cost")
                        },
                        "cost_usd": str(outcomes[t].cost_usd),
                        "models": dict(outcomes[t].models),
                        "model_cost": {m: str(c) for m, c in outcomes[t].model_cost.items()},
                        "budget": budgets.get(t),
                    }
                    for t in teams
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines))


def write_chart(outcomes: dict[str, Outcome], budgets: dict[str, Any]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "matplotlib not installed; skipping chart (pip install -e '.[demo]')", file=sys.stderr
        )
        return

    teams = sorted(outcomes, key=lambda t: outcomes[t].cost_usd, reverse=True)
    model_totals: Counter[str] = Counter()
    for o in outcomes.values():
        for m, c in o.model_cost.items():
            model_totals[m] += float(c)
    top = [m for m, _ in model_totals.most_common(3)]
    series = top + (["other"] if len(model_totals) > 3 else [])
    colors = {"__slots__": ["#2a78d6", "#eb6834", "#1baf7a", "#898781"]}
    status = {"ok": "#0ca30c", "warn": "#fab219", "block": "#d03b3b"}

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9"
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "text.color": ink})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6), facecolor=surface)
    fig.subplots_adjust(wspace=0.45, left=0.08, right=0.98, top=0.80, bottom=0.18)

    # Left: attributed spend by team, stacked by model (fixed slot order, 2px gaps).
    bottoms = [0.0] * len(teams)
    for i, m in enumerate(series):
        vals = []
        for t in teams:
            o = outcomes[t]
            if m == "other":
                v = sum(float(c) for mm, c in o.model_cost.items() if mm not in top)
            else:
                v = float(o.model_cost.get(m, 0))
            vals.append(v)
        ax1.bar(
            teams,
            vals,
            bottom=bottoms,
            color=colors["__slots__"][i],
            width=0.55,
            label=m,
            edgecolor=surface,
            linewidth=1.5,
        )
        bottoms = [b + v for b, v in zip(bottoms, vals, strict=True)]
    for x, total in enumerate(bottoms):
        ax1.text(x, total, f"${total:.3f}", ha="center", va="bottom", fontsize=9, color=ink)
    ax1.set_ylim(0, max(bottoms) * 1.18 if bottoms else 1)
    ax1.set_title(
        "Attributed LLM spend by team and model",
        loc="left",
        fontsize=12,
        fontweight="bold",
        color=ink,
    )
    ax1.set_ylabel("USD (month to date)", color=muted)
    ax1.legend(frameon=False, fontsize=9, loc="upper right")
    ax1.tick_params(axis="x", rotation=20, colors=muted)
    ax1.tick_params(axis="y", colors=muted)

    # Right: budget used % with warn and block markers; status colour + label, never colour alone.
    bteams = [t for t in teams if budgets.get(t)]
    pcts = [float(budgets[t]["used_pct"]) for t in bteams]
    cols = [status[budgets[t]["level"]] for t in bteams]
    bars = ax2.barh(bteams, pcts, color=cols, height=0.55, edgecolor=surface, linewidth=1.5)
    for bar, t in zip(bars, bteams, strict=True):
        b = budgets[t]
        label = f"{b['used_pct']}%  {b['level'].upper()}"
        if b["enforcement"] == "warn":
            label += ", warn-only"
        ax2.text(
            bar.get_width() + 3,
            bar.get_y() + bar.get_height() / 2,
            label,
            va="center",
            fontsize=9,
            color=ink,
        )
    for t in bteams:
        b = budgets[t]
        y = bteams.index(t)
        ax2.plot([float(b["warn_at_pct"])] * 2, [y - 0.33, y + 0.33], color=muted, lw=1)
        ax2.plot([float(b["block_at_pct"])] * 2, [y - 0.33, y + 0.33], color=ink, lw=1)
    ax2.set_xlim(0, max(260.0, (max(pcts) if pcts else 0) + 110))
    ax2.axvline(100, color=grid, lw=1, zorder=0)
    ax2.set_title(
        "Monthly budget used, per team policy",
        loc="left",
        fontsize=12,
        fontweight="bold",
        color=ink,
    )
    ax2.set_xlabel("% of monthly budget (| warn threshold, | block threshold)", color=muted)
    ax2.invert_yaxis()
    ax2.tick_params(colors=muted)

    for ax in (ax1, ax2):
        ax.set_facecolor(surface)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(grid)
        ax.grid(axis="y" if ax is ax1 else "x", color=grid, lw=0.8, zorder=0)
        ax.set_axisbelow(True)

    fig.text(
        0.08,
        0.93,
        "llm-finops-gateway demo: every call has an owner, a price and a policy",
        fontsize=13,
        fontweight="bold",
        color=ink,
    )
    fig.savefig(OUT / "chargeback.png", dpi=160, facecolor=surface)
    print(f"chart written to {OUT / 'chargeback.png'}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mode", choices=["inprocess", "live"], default="inprocess")
    p.add_argument("--url", default="http://localhost:8080", help="gateway URL for live mode")
    p.add_argument("--model", default=None, help="live mode: force this model for all calls")
    p.add_argument(
        "--api-key",
        default=None,
        help="live mode: a team key (Bearer). The key's team overrides the simulated X-Team.",
    )
    p.add_argument("--calls", type=int, default=300)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-chart", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.mode == "inprocess":
        sys.path.insert(0, str(ROOT / "src"))
        client_cm = build_inprocess_client(args.seed)
        with client_cm as client:
            outcomes = run_traffic(client, args.calls, args.seed, args.model)
            budgets = {
                t["team"]: t["budget"] for t in client.get("/v1/finops/budgets").json()["teams"]
            }
    else:
        with httpx.Client(base_url=args.url, timeout=180) as client:
            outcomes = run_traffic(client, args.calls, args.seed, args.model, args.api_key)
            budgets = {
                t["team"]: t["budget"] for t in client.get("/v1/finops/budgets").json()["teams"]
            }

    write_report(outcomes, budgets, args.mode)
    if not args.no_chart:
        write_chart(outcomes, budgets)


if __name__ == "__main__":
    main()
