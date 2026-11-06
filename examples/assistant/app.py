"""A small chat assistant that shows what the gateway (or the LiteLLM plugin) does with every
call: sign in with an API key, and the page reads back who that key is (team, allowed models,
rate limit, budget, registered projects and use cases) from the same policy the gateway
enforces; pick a project and a use case, send a prompt, and read the receipt next to the
answer. The right-hand panel answers questions about the project from its documentation,
through the same gateway, so that traffic is attributed too.

    pip install "llm-finops-gateway[assistant]"
    LFG_ASSISTANT_TARGET_URL=http://localhost:4001 \\
    LFG_ASSISTANT_REPORT_URL=http://localhost:8081 \\
    streamlit run examples/assistant/app.py

Environment:
  LFG_ASSISTANT_TARGET_URL   the gateway or the LiteLLM proxy (default http://localhost:8080)
  LFG_ASSISTANT_REPORT_URL   where /v1/finops/budgets lives (default: the target URL)
  LFG_ASSISTANT_REPORT_KEY   key for the budgets API when the report needs one
  LFG_ASSISTANT_HEADERS_MODE "true" when the target is a gateway in headers mode: the page asks
                             for a team name instead of a key and sends X-Team
  LFG_ASSISTANT_MODELS       model names offered when the policy allows any ("*")
  LFG_ASSISTANT_QA_MODEL     model for the Q&A panel (default: the first allowed model)
  LFG_ASSISTANT_DOCS_DIR     repository root with README.md and docs/ (default: autodetect)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st
from finops_client import QA_SYSTEM, Identity, Receipt, budgets, chat, docs_context, identity

TARGET = os.environ.get("LFG_ASSISTANT_TARGET_URL", "http://localhost:8080")
REPORT = os.environ.get("LFG_ASSISTANT_REPORT_URL", TARGET)
REPORT_KEY = os.environ.get("LFG_ASSISTANT_REPORT_KEY") or None
HEADERS_MODE = os.environ.get("LFG_ASSISTANT_HEADERS_MODE", "").lower() in ("1", "true", "yes")
MODELS = [
    m.strip() for m in os.environ.get("LFG_ASSISTANT_MODELS", "gpt-4.1-mini,gpt-5-mini").split(",")
]
QA_MODEL = os.environ.get("LFG_ASSISTANT_QA_MODEL") or None
MAX_TOKENS = 300
CHAT_SYSTEM = (
    "You are a general assistant in a demo of an LLM FinOps gateway: every call you answer is "
    "attributed to a team and a project and priced, and the receipt is shown next to your "
    "answer. Answer the user's questions normally and briefly. If the user asks about 'this "
    "repository', 'the gateway' or 'this project', say that the panel on the right answers "
    "those from the documentation, and that this chat is a plain assistant."
)


def _docs_dir() -> Path:
    env = os.environ.get("LFG_ASSISTANT_DOCS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "README.md").exists() and (parent / "docs").is_dir():
            return parent
    return here.parent


@st.cache_data(show_spinner=False)
def _context(root: str) -> str:
    return docs_context(Path(root))


@st.cache_data(ttl=60, show_spinner=False)
def _who(target: str, key: str) -> Identity | None:
    return identity(target, key)


def _team_row(team: str | None, user_key: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The team's row of the budgets report, plus the report header (policy version)."""
    report = budgets(REPORT, REPORT_KEY or (user_key if REPORT == TARGET else None), team)
    for row in report.get("teams", []):
        if row["team"] == team:
            return row, report
    return {}, report


def _pct_bar(label: str, budget: dict[str, str]) -> None:
    if not budget:
        return
    try:
        used = float(budget.get("used-pct", "0"))
    except ValueError:
        used = 0.0
    level = budget.get("status", "ok")
    icon = {"ok": "🟢", "warn": "🟡", "block": "🔴"}.get(level, "⚪")
    st.caption(
        f"{icon} {label}: {budget.get('spent-usd', '?')} of {budget.get('limit-usd', '?')} USD "
        f"({used:.1f}%, {level})"
    )
    st.progress(min(used / 100.0, 1.0))


def _envelope_bar(label: str, b: dict[str, Any] | None) -> None:
    if b:
        _pct_bar(
            label,
            {
                "used-pct": b["used_pct"],
                "status": b["level"],
                "spent-usd": b["spent_usd"],
                "limit-usd": b["limit_usd"],
            },
        )


def _receipt(r: Receipt) -> None:
    if r.error is not None:
        st.error(f"HTTP {r.status_code}: {r.error.get('error') or r.error.get('message')}")
        with st.expander("Details"):
            st.json(r.error)
        return
    st.markdown(
        f"**Cost {r.cost or '?'} {r.currency}** · team budget "
        f"{r.team_budget.get('used-pct', '?')}% · project budget "
        f"{r.project_budget.get('used-pct', '-')}%"
    )
    _pct_bar("Team", r.team_budget)
    _pct_bar("Project", r.project_budget)
    if r.warning:
        st.warning(r.warning)
    if r.cached_input_tokens:
        st.caption(
            f"{r.cached_input_tokens} prompt tokens came from the provider's cache "
            "and were billed at the cached-input price"
        )
    st.caption(f"request id {r.request_id}")


st.set_page_config(page_title="LLM FinOps assistant", layout="wide")
st.title("LLM FinOps assistant")
st.caption(f"Every call goes through {TARGET}; the receipt comes back in the X-FinOps headers.")

# ---- sign in: a key (or a team name in headers mode), then what the policy says about it ----
with st.sidebar:
    st.header("Sign in")
    api_key = ""
    team: str | None = None
    who: Identity | None = None
    if HEADERS_MODE:
        team = st.text_input("Team (X-Team)", "platform").strip() or None
    else:
        api_key = str(st.session_state.get("api_key") or "")
        if not api_key:
            with st.form("sign_in"):
                typed = st.text_input(
                    "API key",
                    type="password",
                    help="A gateway lfg_ key or a LiteLLM virtual key",
                )
                if st.form_submit_button("Sign in") and typed.strip():
                    st.session_state["api_key"] = typed.strip()
                    st.rerun()
            st.info("Paste the key you were given and sign in to see what it may do.")
            st.stop()
        who = _who(TARGET, api_key)
        if who is None:
            st.error(f"{TARGET} does not recognise this key.")
            if st.button("Try another key"):
                st.session_state.pop("api_key", None)
                st.rerun()
            st.stop()
        if st.button("Sign out"):
            for k in ("api_key", "chat", "chat_key"):
                st.session_state.pop(k, None)
            st.rerun()
        team = who.team
        st.success(
            f"{who.key_alias or 'key'} ({who.source}), team **{team or '?'}**"
            if team
            else f"{who.key_alias or 'key'} ({who.source}): no team, calls will be rejected"
        )

    st.header("What this team may do")
    row: dict[str, Any] = {}
    try:
        row, report = _team_row(team, api_key or None)
        st.caption(f"policy {report.get('policy_version')}")
    except Exception as exc:  # the report may live elsewhere or need a key
        st.caption(f"policy unavailable: {exc}")
    allowed = list(row.get("allowed_models") or ["*"])
    st.caption("Models: " + (", ".join(allowed) if allowed != ["*"] else "any in the price book"))
    if row.get("requests_per_minute"):
        st.caption(f"Rate limit: {row['requests_per_minute']} requests per minute")
    b = row.get("budget")
    if b:
        st.caption(
            f"Budget: {b['limit_usd']} USD per month, warn at {b['warn_at_pct']}%, "
            f"{'block' if b['enforcement'] == 'block' else 'warn only'} at {b['block_at_pct']}%"
        )
        _envelope_bar("Team", b)
    else:
        st.caption("Budget: none")

    st.header("This call")
    registry = row.get("projects")
    if not registry:
        st.caption("This team has no project registry: any project name is accepted.")
    default_project = (who.default_project if who else None) or ""
    default_use_case = (who.default_use_case if who else None) or ""
    project_row: dict[str, Any] = {}
    if registry:
        names = [p["project"] for p in registry]
        idx = names.index(default_project) if default_project in names else 0
        project = st.selectbox("Project (registered)", names, index=idx)
        project_row = next(p for p in registry if p["project"] == project)
        cases = project_row.get("use_cases")
        if cases:
            cidx = cases.index(default_use_case) if default_use_case in cases else 0
            use_case = st.selectbox("Use case (registered)", cases, index=cidx)
        else:
            use_case = st.text_input("Use case (X-Use-Case)", default_use_case or "faq")
        _envelope_bar("Project", project_row.get("budget"))
    else:
        project = st.text_input("Project (X-Project)", default_project or "chat-assistant")
        use_case = st.text_input("Use case (X-Use-Case)", default_use_case or "faq")
    offered = list(project_row.get("allowed_models") or allowed)
    if offered == ["*"]:
        offered = MODELS
    model = st.selectbox("Model", offered)

left, right = st.columns([3, 2])

with left:
    st.subheader("Chat with the model")
    st.caption("A plain assistant; every answer comes with its FinOps receipt.")
    # a new key (or team) starts a fresh conversation: no receipts of someone else's budget
    if st.session_state.get("chat_key") != (api_key or team):
        st.session_state.chat = []
        st.session_state.chat_key = api_key or team
    for turn in st.session_state.chat:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("receipt") is not None:
                _receipt(turn["receipt"])
    prompt = st.chat_input("Ask the assistant something")
    if prompt:
        st.session_state.chat.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        history = [{"role": "system", "content": CHAT_SYSTEM}] + [
            {"role": t["role"], "content": t["content"]} for t in st.session_state.chat
        ]
        with st.chat_message("assistant"):
            with st.spinner("calling the model through the gateway"):
                answer, receipt = chat(
                    TARGET,
                    api_key,
                    model,
                    history,
                    team=team if HEADERS_MODE else None,
                    project=project.strip(),
                    use_case=use_case.strip() or None,
                    max_tokens=MAX_TOKENS,
                )
            text = (
                answer
                if answer is not None
                else "_(the call was rejected before the model was called)_"
            )
            st.markdown(text)
            _receipt(receipt)
        st.session_state.chat.append({"role": "assistant", "content": text, "receipt": receipt})

with right:
    st.subheader("Ask about the gateway (from its docs)")
    st.caption(
        "Answers come from the repository's documentation, through the same gateway, charged "
        "to the project and use case selected on the left (about 40k prompt tokens a question)."
    )
    question = st.text_area("Question", placeholder="How is the budget reserved before a call?")
    if st.button("Ask") and question.strip():
        ctx = _context(str(_docs_dir()))
        with st.spinner("reading the docs"):
            answer, receipt = chat(
                TARGET,
                api_key,
                QA_MODEL or model,
                [
                    {"role": "system", "content": QA_SYSTEM + ctx},
                    {"role": "user", "content": question.strip()},
                ],
                team=team if HEADERS_MODE else None,
                project=project.strip(),
                use_case=use_case.strip() or None,
                max_tokens=600,
            )
        if answer is not None:
            st.markdown(answer)
        _receipt(receipt)
