"""The assistant example's helper module (examples/assistant/finops_client.py)."""

import importlib.util
import sys
from pathlib import Path

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


def test_docs_context_lists_files_in_order(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# readme")
    (tmp_path / "docs" / "b.md").write_text("bee")
    (tmp_path / "docs" / "a.md").write_text("ay")
    (tmp_path / "docs" / "adr" / "0001-x.md").write_text("adr")
    ctx = finops_client.docs_context(tmp_path)
    assert (
        ctx.index("===== README.md") < ctx.index("===== docs/a.md") < ctx.index("===== docs/b.md")
    )
    assert "===== docs/adr/0001-x.md" in ctx and ctx.endswith("adr")
    # a header per file: README (32 chars) and docs/a.md (26) fit in 60, docs/b.md does not
    assert finops_client.docs_context(tmp_path, max_chars=60).count("\n===== ") == 2
