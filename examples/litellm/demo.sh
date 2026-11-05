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
  docker compose --env-file ../../.env up -d
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
SMALL='{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}'
BIG=$(python3 -c 'import json;print(json.dumps({"model":"gpt-4.1-mini","messages":[{"role":"user","content":("lorem ipsum dolor sit amet "*600)+" Reply with OK."}],"max_tokens":1}))')

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
echo; echo "Ledger: the same tables the gateway writes; open the /finops report page against LFG_DATABASE_URL."
