"""The assistant example's helper module (examples/assistant/finops_client.py)."""

import importlib.util
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "finops_client", ROOT / "examples" / "assistant" / "finops_client.py"
)
assert spec is not None and spec.loader is not None
finops_client = importlib.util.module_from_spec(spec)
sys.modules["finops_client"] = finops_client
spec.loader.exec_module(finops_client)


def test_receipt_reads_cost_and_both_budgets() -> None:
    r = finops_client.receipt_from_headers(
        200,
        {
            "X-FinOps-Request-Id": "lt-1",
            "X-FinOps-Cost-USD": "0.0000052",
            "X-FinOps-Currency": "USD",
            "X-FinOps-Cached-Input-Tokens": "1024",
            "X-FinOps-Budget-Used-Pct": "12.5",
            "X-FinOps-Budget-Status": "ok",
            "X-FinOps-Project-Budget-Used-Pct": "48.5",
            "X-FinOps-Project-Budget-Status": "warn",
            "X-FinOps-Warning": "project 'x' has used 65%",
        },
    )
    assert r.request_id == "lt-1" and r.cost == "0.0000052" and r.error is None
    assert r.cached_input_tokens == 1024
    assert r.team_budget["used-pct"] == "12.5" and r.team_budget["status"] == "ok"
    assert r.project_budget["status"] == "warn"
    assert r.warning is not None and r.warning.startswith("project 'x'")


def test_receipt_unwraps_gateway_and_litellm_error_shapes() -> None:
    gw = finops_client.receipt_from_headers(
        403, {}, {"detail": {"error": "budget_exceeded", "team": "t"}}
    )
    assert gw.error == {"error": "budget_exceeded", "team": "t"}
    ll = finops_client.receipt_from_headers(
        403,
        {},
        {
            "error": {
                "message": "budget_exceeded",
                "provider_specific_fields": {"error": "budget_exceeded"},
            }
        },
    )
    assert ll.error == {"error": "budget_exceeded"}
    plain = finops_client.receipt_from_headers(422, {}, {"detail": "No price configured"})
    assert plain.error == {"message": "No price configured"}


def test_docs_context_lists_the_selected_files_in_order(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# readme")
    (tmp_path / "docs" / "litellm.md").write_text("lite")
    (tmp_path / "docs" / "architecture.md").write_text("arch")
    (tmp_path / "docs" / "other.md").write_text("skip")
    (tmp_path / "docs" / "adr" / "0001-x.md").write_text("adr")
    ctx = finops_client.docs_context(tmp_path)
    assert (
        ctx.index("===== README.md")
        < ctx.index("===== docs/architecture.md")
        < ctx.index("===== docs/litellm.md")
    )
    assert "other.md" not in ctx and "adr" not in ctx and ctx.endswith("lite")
    # a header per file: README (32 chars) fits in 40, docs/architecture.md (39 more) does not
    assert finops_client.docs_context(tmp_path, max_chars=40).count("\n===== ") == 1


def _litellm_target(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization")
    if request.url.path == "/key/info" and auth == "Bearer sk-virtual":
        info = {
            "key_alias": "support-bot",
            "team_id": "t-1",
            "team_alias": None,
            "metadata": {"finops": {"project": "internal-tools", "use_case": "triage"}},
        }
        return httpx.Response(200, json={"key": "sk-...", "info": info})
    if request.url.path == "/team/info" and request.url.params.get("team_id") == "t-1":
        return httpx.Response(200, json={"team_id": "t-1", "team_info": {"team_alias": "support"}})
    return httpx.Response(401, json={"error": "nope"})


def _gateway_target(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization")
    if request.url.path == "/v1/finops/budgets" and auth == "Bearer lfg_team":
        return httpx.Response(200, json={"policy_version": "p", "teams": [{"team": "platform"}]})
    if request.url.path == "/v1/finops/budgets" and auth == "Bearer lfg_finops":
        teams = [{"team": "platform"}, {"team": "research"}]
        return httpx.Response(200, json={"policy_version": "p", "teams": teams})
    return httpx.Response(404 if request.url.path == "/key/info" else 401)


def test_identity_of_a_litellm_virtual_key_resolves_team_and_defaults() -> None:
    who = finops_client.identity(
        "http://litellm", "sk-virtual", transport=httpx.MockTransport(_litellm_target)
    )
    assert who is not None
    assert (who.source, who.team, who.key_alias) == ("litellm", "support", "support-bot")
    assert (who.default_project, who.default_use_case) == ("internal-tools", "triage")
    assert (
        finops_client.identity(
            "http://litellm", "sk-x", transport=httpx.MockTransport(_litellm_target)
        )
        is None
    )


def test_identity_of_gateway_keys_comes_from_the_budgets_scope() -> None:
    t = httpx.MockTransport(_gateway_target)
    team_key = finops_client.identity("http://gw", "lfg_team", transport=t)
    assert team_key is not None and (team_key.source, team_key.team) == ("gateway", "platform")
    finops_key = finops_client.identity("http://gw", "lfg_finops", transport=t)
    assert finops_key is not None and finops_key.team is None  # sees every team
    assert finops_client.identity("http://gw", "lfg_bad", transport=t) is None


def test_budgets_narrows_with_x_team_in_headers_mode() -> None:
    seen: dict[str, str] = {}

    def report(request: httpx.Request) -> httpx.Response:
        seen.update({k: v for k, v in request.headers.items() if k.startswith("x-")})
        return httpx.Response(200, json={"policy_version": "p", "teams": []})

    finops_client.budgets("http://report", None, "platform", transport=httpx.MockTransport(report))
    assert seen == {"x-team": "platform"}
