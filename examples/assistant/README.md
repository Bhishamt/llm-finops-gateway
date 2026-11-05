# Assistant example

A Streamlit page with two halves: a chat that goes through the gateway or the LiteLLM plugin
and shows the FinOps receipt (cost, team and project budget, warnings, rejections) next to
every answer, and a panel that answers questions about this project from its documentation,
through the same gateway, so that traffic is attributed as well.

```bash
pip install "llm-finops-gateway[assistant]"
LFG_ASSISTANT_TARGET_URL=http://localhost:4001 \
LFG_ASSISTANT_REPORT_URL=http://localhost:8081 \
LFG_ASSISTANT_KEYS='{"customer-support":"sk-...","research":"sk-..."}' \
streamlit run examples/assistant/app.py
```

`examples/litellm/docker-compose.yml` runs it on port 8501 next to the LiteLLM stack; its
`demo.sh` writes the keys it created into `.demo-keys/assistant.env` for the container.
The environment variables are listed at the top of `app.py`.
