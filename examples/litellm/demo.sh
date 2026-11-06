#!/usr/bin/env bash
# Walk through the FinOps plugin on a running LiteLLM proxy (see docker-compose.yml, port 4001).
#   ./demo.sh                      # creates the teams and keys on first run, then the seven calls
#   ./demo.sh --reset              # wipe the LiteLLM database and the ledger first (a fresh month)
# Needs: curl, python3; LITELLM_URL (default http://localhost:4001) and LITELLM_MASTER_KEY.
set -euo pipefail
if [ "${1:-}" = "--reset" ]; then
  # Fresh month: drop the LiteLLM database and the plugin ledger, forget the cached keys.
  docker compose --env-file ../../.env down -v
  rm -rf "${STATE_DIR:-.demo-keys}"
  docker compose --env-file ../../.env up -d --build
  for i in $(seq 1 40); do
    curl -s -m 3 -o /dev/null -w "%{http_code}" "${LITELLM_URL:-http://localhost:4001}/health/liveliness" 2>/dev/null | grep -q 200 && break
    sleep 3
  done
fi
B=${LITELLM_URL:-http://localhost:4001}
M=${LITELLM_MASTER_KEY:-sk-lfg-master-local-only}
CT="Content-Type: application/json"
STATE=${STATE_DIR:-.demo-keys}; mkdir -p "$STATE"

key_for() {  # team alias -> virtual key, created once and cached locally
  local team=$1 f="$STATE/$1.key"
  if [ ! -s "$f" ]; then
    local tid
    tid=$(curl -sf -X POST "$B/team/new" -H "Authorization: Bearer $M" -H "$CT" \
      -d "{\"team_alias\":\"$team\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["team_id"])')
    curl -sf -X POST "$B/key/generate" -H "Authorization: Bearer $M" -H "$CT" \
      -d "{\"team_id\":\"$tid\",\"key_alias\":\"$team-demo\"}" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])' > "$f"
  fi
  cat "$f"
}
show() { grep -i -E "^HTTP|^x-finops" | sed 's/^/    /'; }
step() { printf '\n== %s\n' "$1"; }

CS=$(key_for customer-support); RS=$(key_for research)
CEO=$(key_for ceo); TL=$(key_for team-leads); EMP=$(key_for employees)
# keys for the assistant container (examples/assistant), picked up on its next start
printf 'LFG_ASSISTANT_KEYS={"ceo":"%s","team-leads":"%s","employees":"%s","customer-support":"%s","research":"%s"}\n' \
  "$CEO" "$TL" "$EMP" "$CS" "$RS" > "$STATE/assistant.env"
SMALL='{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}'
# A nonce keeps the big prompt unique per run: a repeated prompt is served from Azure's prompt
# cache at the cached-input price, which is a nice effect but would stop the budget demo.
BIG=$(python3 -c 'import json,uuid;print(json.dumps({"model":"gpt-4.1-mini","messages":[{"role":"user","content":"run "+uuid.uuid4().hex+" "+("lorem ipsum dolor sit amet "*600)+" Reply with OK."}],"max_tokens":1}))')

step "1. No X-Project: rejected before the call (400 missing_attribution)"
curl -s -o /dev/stderr -w "    HTTP %{http_code}\n" "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "$CT" -d "$SMALL" 2>&1 | head -c 400; echo
step "2. Project not in the team's registry: rejected before the call (403 project_not_registered)"
curl -s -o /dev/stderr -w "    HTTP %{http_code}\n" "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "X-Project: pilot-7" -H "$CT" -d "$SMALL" 2>&1 | head -c 400; echo
step "3. Model without a price: rejected before the call (422)"
curl -s -o /dev/stderr -w "    HTTP %{http_code}\n" "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "X-Project: chat-assistant" -H "$CT" \
  -d '{"model":"mystery-model","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}' 2>&1 | head -c 300; echo
step "4. Priced call with a registered project: cost, team budget and project budget in the headers"
curl -s -D - -o /dev/null "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "X-Project: chat-assistant" -H "X-Use-Case: faq" -H "$CT" -d "$SMALL" | show
step "5. A big prompt takes the project past its warn threshold"
curl -s -D - -o /dev/null "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "X-Project: chat-assistant" -H "$CT" --data-binary "$BIG" | show
step "6. The next big prompt would exceed the project budget: blocked before the call (403)"
curl -s -o /dev/stderr -w "    HTTP %{http_code}\n" "$B/v1/chat/completions" -H "Authorization: Bearer $CS" -H "X-Project: chat-assistant" -H "$CT" --data-binary "$BIG" 2>&1 | head -c 400; echo
step "7. Research is warn-only and streams; the cost is settled when the stream ends"
curl -s -N -D /tmp/finops-demo.h -o /dev/null "$B/v1/chat/completions" -H "Authorization: Bearer $RS" -H "X-Project: experiments" -H "$CT" \
  -d '{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5,"stream":true,"stream_options":{"include_usage":true}}'
show < /tmp/finops-demo.h
step "8. Two teams ask the same long question: the second one is served from the provider's prompt cache"
# Azure OpenAI and OpenAI cache prompt prefixes of 1024+ tokens for a few minutes, across keys and
# teams. The plugin bills those tokens at the price book's cached_input price and reports them in
# X-FinOps-Cached-Input-Tokens; each call is still attributed to its own team and project.
SAME=$(python3 -c 'import json;print(json.dumps({"model":"gpt-4.1-mini","messages":[{"role":"system","content":"You are the support assistant. Knowledge base: "+("The return window is 30 days; refunds take 5 working days; exchanges are free in store. "*70)},{"role":"user","content":"How long is the return window? Answer in one sentence."}],"max_tokens":40}))')
echo "   employees / project onboarding, use case faq (first call, full input price):"
curl -s -D - -o /dev/null "$B/v1/chat/completions" -H "Authorization: Bearer $EMP" -H "X-Project: onboarding" -H "X-Use-Case: faq" -H "$CT" --data-binary "$SAME" | show
echo "   team-leads / project support-bot, use case eval (same prompt, cached prefix):"
curl -s -D - -o /dev/null "$B/v1/chat/completions" -H "Authorization: Bearer $TL" -H "X-Project: support-bot" -H "X-Use-Case: eval" -H "$CT" --data-binary "$SAME" | show
echo "   FOCUS rows for today (report page on :8081): one row per team and project, cached tokens in x_CachedInputTokens"
R=${REPORT_URL:-http://localhost:8081}; TODAY=$(date -u +%Y-%m-%d); TOMORROW=$(date -u -v+1d +%Y-%m-%d 2>/dev/null || date -u -d tomorrow +%Y-%m-%d)
curl -s "$R/v1/finops/export.focus.csv?from=$TODAY&to=$TOMORROW" | python3 -c '
import csv,json,sys
print("    team | project | use case | input tokens | cached | BilledCost")
for r in csv.DictReader(sys.stdin):
    t=json.loads(r["Tags"])
    if t.get("project") in ("onboarding","support-bot"):
        print("    " + " | ".join([r["SubAccountId"],t["project"],t.get("use_case") or "",r["x_InputTokens"],r["x_CachedInputTokens"],r["BilledCost"]]))' 2>/dev/null || echo "    (report page not reachable at $R)"
echo; echo "Ledger: the same tables the gateway writes; open the /finops report page against LFG_DATABASE_URL."
