"""A small chat assistant that shows what the gateway (or the LiteLLM plugin) does with every
call: pick a team key, a project and a use case, send a prompt, and read the receipt next to
the answer. The right-hand panel answers questions about the project from its documentation,
through the same gateway, so that traffic is attributed too.

    pip install "llm-finops-gateway[assistant]"
    LFG_ASSISTANT_TARGET_URL=http://localhost:4001 \\
    LFG_ASSISTANT_KEYS='{"customer-support":"sk-...","research":"sk-..."}' \\
    streamlit run examples/assistant/app.py

Environment:
  LFG_ASSISTANT_TARGET_URL   the gateway or the LiteLLM proxy (default http://localhost:8080)
  LFG_ASSISTANT_KEYS         JSON map team -> API key (gateway lfg_ keys or LiteLLM virtual keys)
  LFG_ASSISTANT_HEADERS_MODE "true" when the target is a gateway in headers mode (sends X-Team)
  LFG_ASSISTANT_MODELS       comma-separated model names (default gpt-4.1-mini,gpt-5-mini)
  LFG_ASSISTANT_REPORT_URL   where /v1/finops/budgets lives (default: the target URL)
  LFG_ASSISTANT_REPORT_KEY   key for the budgets API when the report needs one
  LFG_ASSISTANT_QA_TEAM      team key name used by the Q&A panel (default: the first key)
  LFG_ASSISTANT_QA_MODEL     model for the Q&A panel (default: the first model)
  LFG_ASSISTANT_DOCS_DIR     repository root with README.md and docs/ (default: autodetect)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from finops_client import QA_SYSTEM, Receipt, budgets, chat, docs_context

TARGET = os.environ.get("LFG_ASSISTANT_TARGET_URL", "http://localhost:8080")
REPORT = os.environ.get("LFG_ASSISTANT_REPORT_URL", TARGET)
REPORT_KEY = os.environ.get("LFG_ASSISTANT_REPORT_KEY") or None
HEADERS_MODE = os.environ.get("LFG_ASSISTANT_HEADERS_MODE", "").lower() in ("1", "true", "yes")
KEYS: dict[str, str] = json.loads(os.environ.get("LFG_ASSISTANT_KEYS", "{}"))
# Key names are teams in the policy file; these labels make the demo read as roles.
ROLE_LABELS = {"ceo": "CEO", "team-leads": "Team lead", "employees": "Employee"}

MODELS = [
    m.strip() for m in os.environ.get("LFG_ASSISTANT_MODELS", "gpt-4.1-mini,gpt-5-mini").split(",")
]
QA_TEAM = os.environ.get("LFG_ASSISTANT_QA_TEAM") or (next(iter(KEYS), ""))
QA_MODEL = os.environ.get("LFG_ASSISTANT_QA_MODEL") or MODELS[0]


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


def _receipt(r: Receipt) -> None:
    if r.error is not None:
        st.error(f"HTTP {r.status_code}: {r.error.get('error') or r.error.get('message')}")
        with st.expander("Details"):
            st.json(r.error)
        return
    cols = st.columns(3)
    cols[0].metric("Cost", f"{r.cost or '?'} {r.currency}")
    cols[1].metric("Team budget", f"{r.team_budget.get('used-pct', '?')}%")
    cols[2].metric("Project budget", f"{r.project_budget.get('used-pct', '-')}%")
    _pct_bar("Team", r.team_budget)
    _pct_bar("Project", r.project_budget)
    if r.warning:
        st.warning(r.warning)
    st.caption(f"request id {r.request_id}")


st.set_page_config(page_title="LLM FinOps assistant", layout="wide")
st.title("LLM FinOps assistant")
st.caption(f"Every call goes through {TARGET}; the receipt comes back in the X-FinOps headers.")

if not KEYS:
    st.error("Set LFG_ASSISTANT_KEYS to a JSON map of team -> API key.")
    st.stop()

with st.sidebar:
    st.header("Who is calling")
    team = st.selectbox("Role (team key)", list(KEYS), format_func=lambda k: ROLE_LABELS.get(k, k))
    project = st.text_input("Project (X-Project)", "chat-assistant")
    use_case = st.text_input("Use case (X-Use-Case)", "faq")
    model = st.selectbox("Model", MODELS)
    max_tokens = st.slider("max_tokens", 16, 1024, 200, 16)
    st.header("Budgets now")
    try:
        report = budgets(REPORT, REPORT_KEY)
        for t in report.get("teams", []):
            if t["team"] == team:
                allowed = ", ".join(t.get("allowed_models", []))
                st.caption(f"{ROLE_LABELS.get(team, team)} may use: {allowed}")
            b = t.get("budget") or {}
            if b:
                _pct_bar(
                    t["team"],
                    {
                        "used-pct": b["used_pct"],
                        "status": b["level"],
                        "spent-usd": b["spent_usd"],
                        "limit-usd": b["limit_usd"],
                    },
                )
        st.caption(f"policy {report.get('policy_version')}")
    except Exception as exc:  # the report may live elsewhere or need a key
        st.caption(f"budgets unavailable: {exc}")

left, right = st.columns([3, 2])

with left:
    st.subheader("Chat")
    if "chat" not in st.session_state:
        st.session_state.chat = []
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
        history = [{"role": t["role"], "content": t["content"]} for t in st.session_state.chat]
        with st.chat_message("assistant"):
            with st.spinner("calling the model through the gateway"):
                answer, receipt = chat(
                    TARGET,
                    KEYS[team],
                    model,
                    history,
                    team=team if HEADERS_MODE else None,
                    project=project.strip(),
                    use_case=use_case.strip() or None,
                    max_tokens=max_tokens,
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
    st.subheader("Ask about the gateway")
    st.caption(
        "Answers come from the repository's documentation, through the same gateway "
        f"(team {QA_TEAM or '?'}, project repo-qa)."
    )
    question = st.text_area("Question", placeholder="How is the budget reserved before a call?")
    if st.button("Ask") and question.strip():
        if not QA_TEAM or QA_TEAM not in KEYS:
            st.error("No team key for the Q&A panel (LFG_ASSISTANT_QA_TEAM).")
        else:
            ctx = _context(str(_docs_dir()))
            with st.spinner("reading the docs"):
                answer, receipt = chat(
                    TARGET,
                    KEYS[QA_TEAM],
                    QA_MODEL,
                    [
                        {"role": "system", "content": QA_SYSTEM + ctx},
                        {"role": "user", "content": question.strip()},
                    ],
                    team=QA_TEAM if HEADERS_MODE else None,
                    project="repo-qa",
                    use_case="docs-qa",
                    max_tokens=600,
                )
            if answer is not None:
                st.markdown(answer)
            _receipt(receipt)
