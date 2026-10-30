# Examples

Point the official OpenAI SDKs at the gateway. The only changes against calling the model
provider directly are `base_url`, the team key as `api_key`, and two headers for attribution.

| | Python | TypeScript |
| --- | --- | --- |
| Directory | [`python/`](python/) | [`typescript/`](typescript/) |
| Install | `pip install openai` | `npm install` |
| Run | `python chat.py` | `npm start` |

Both scripts do the same four things against `http://localhost:8080/v1` (override with
`LFG_URL`):

1. a plain completion and the `x_finops` block the gateway adds (cost, budget state, warnings);
2. a streamed completion, with the cost arriving in the final chunk;
3. a tool call round trip (`get_weather`);
4. a call that the gateway rejects (unknown model), to show the error shape.

Authentication:

- `LFG_AUTH_MODE=api_key` (default): set `LFG_API_KEY` to a team key (`llm-finops-gateway keys
  new --team platform --id example`). The team comes from the key; only `X-Project` is needed.
- `LFG_AUTH_MODE=headers` (development): any `LFG_API_KEY` value works; the scripts also send
  `X-Team` so the call has an owner.

Start a gateway for the examples:

```bash
LFG_AUTH_MODE=headers llm-finops-gateway serve --port 8080      # Ollama, or set the Azure variables
```
