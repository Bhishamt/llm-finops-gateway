# Assistant example

A Streamlit page with two halves: a chat that goes through the gateway or the LiteLLM plugin
and shows the FinOps receipt (cost, team and project budget, warnings, rejections) next to
every answer, and a panel that answers questions about this project from its documentation,
through the same gateway, so that traffic is attributed as well.

Sign in with an API key (a gateway `lfg_` key or a LiteLLM virtual key) and the page reads
back what the policy says about it: the team the key belongs to, its allowed models, rate
limit and budget, and the team's registered projects with their use cases and budgets, so the
project and use case are picked from lists where a registry exists. A LiteLLM key that carries
a default project in its metadata has it preselected. Nothing is configured on the page itself.

```bash
pip install "llm-finops-gateway[assistant]"
LFG_ASSISTANT_TARGET_URL=http://localhost:4001 \
LFG_ASSISTANT_REPORT_URL=http://localhost:8081 \
streamlit run examples/assistant/app.py
```

`examples/litellm/docker-compose.yml` runs it on port 8501 next to the LiteLLM stack; its
`demo.sh` writes the keys it created into `.demo-keys/assistant.env`, which the page offers as
a "demo role" drop-down (`LFG_ASSISTANT_KEYS`) next to the key field. The environment
variables are listed at the top of `app.py`.
