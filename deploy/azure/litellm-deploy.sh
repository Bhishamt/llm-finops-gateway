#!/usr/bin/env bash
# Deploy the LiteLLM direction (proxy + FinOps plugin, report page, assistant) next to an
# existing main.bicep deployment: build the three images in ACR, read the secrets the first
# deployment put in Key Vault, create or reuse the LiteLLM master and salt keys there, deploy
# litellm.bicep, upload the config files, create the demo teams and keys.
#
#   RG=rg-llm-finops-gateway-dev ACR=lfgacr1325 deploy/azure/litellm-deploy.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
RG=${RG:-rg-llm-finops-gateway-dev}
ACR=${ACR:-$(az acr list -g "$RG" --query "[0].name" -o tsv)}
KV=${KV:-$(az keyvault list -g "$RG" --query "[0].name" -o tsv)}
PG=${PG:-$(az postgres flexible-server list -g "$RG" --query "[0].name" -o tsv)}
STORAGE=${STORAGE:-$(az storage account list -g "$RG" --query "[0].name" -o tsv)}
AOAI=${AOAI:-$(az cognitiveservices account list -g "$RG" --query "[?kind=='OpenAI'].name | [0]" -o tsv)}
TAG=${TAG:-main}
echo "rg=$RG acr=$ACR kv=$KV pg=$PG storage=$STORAGE aoai=$AOAI"

# 1. Images, built in the cloud from this checkout
az acr build -r "$ACR" -t "lfg-litellm:$TAG" -f examples/litellm/Dockerfile . --only-show-errors
az acr build -r "$ACR" -t "llm-finops-gateway:$TAG" . --only-show-errors
az acr build -r "$ACR" -t "lfg-assistant:$TAG" -f examples/assistant/Dockerfile . --only-show-errors
SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

# 2. Secrets: reuse what main.bicep stored; create the LiteLLM keys once
secret() { az keyvault secret show --vault-name "$KV" --name "$1" --query value -o tsv; }
ensure() { az keyvault secret show --vault-name "$KV" --name "$1" --query value -o tsv 2>/dev/null \
  || az keyvault secret set --vault-name "$KV" --name "$1" --value "$2" --query value -o tsv; }
PGPW=$(secret postgres-password)
PGHOST=$(az postgres flexible-server show -n "$PG" -g "$RG" --query fullyQualifiedDomainName -o tsv)
LEDGER_URL=$(secret database-url)
REPORT_KEY=$(secret team-key-finops-report)
MASTER=$(ensure litellm-master-key "sk-$(openssl rand -hex 16)")
SALT=$(ensure litellm-salt-key "sk-$(openssl rand -hex 16)")
AOAI_KEY=$(az cognitiveservices account keys list -n "$AOAI" -g "$RG" --query key1 -o tsv)
AOAI_ENDPOINT=$(az cognitiveservices account show -n "$AOAI" -g "$RG" --query properties.endpoint -o tsv)

# 3. The three container apps, the config share and the litellm database
az deployment group create -g "$RG" -f deploy/azure/litellm.bicep --only-show-errors \
  -p registryServer="$SERVER" litellmImage="$SERVER/lfg-litellm:$TAG" gatewayImage="$SERVER/llm-finops-gateway:$TAG" \
     assistantImage="$SERVER/lfg-assistant:$TAG" storageAccountName="$STORAGE" postgresServerName="$PG" \
     litellmDatabaseUrl="postgresql://lfgadmin:$PGPW@$PGHOST/litellm?sslmode=require" ledgerDatabaseUrl="$LEDGER_URL" \
     litellmMasterKey="$MASTER" litellmSaltKey="$SALT" azureOpenAiEndpoint="$AOAI_ENDPOINT" azureOpenAiApiKey="$AOAI_KEY" \
     keysYaml="$(cat deploy/azure/keys.yaml)" reportKey="$REPORT_KEY" reportRates='{"EUR":0.88}' \
  --query "properties.outputs" -o json > deploy/azure/.litellm-outputs.json
LITELLM_URL=$(python3 -c 'import json;print(json.load(open("deploy/azure/.litellm-outputs.json"))["litellmUrl"]["value"])')
REPORT_URL=$(python3 -c 'import json;print(json.load(open("deploy/azure/.litellm-outputs.json"))["reportUrl"]["value"])')
ASSISTANT_URL=$(python3 -c 'import json;print(json.load(open("deploy/azure/.litellm-outputs.json"))["assistantUrl"]["value"])')

# 4. Config files to the share (the proxy reads litellm.yaml at startup: restart it once after the first upload)
RG=$RG STORAGE=$STORAGE REPORT_URL=$REPORT_URL deploy/azure/push-config.sh
az containerapp revision restart -n lfg-litellm -g "$RG" --revision "$(az containerapp revision list -n lfg-litellm -g "$RG" --query "[0].name" -o tsv)" --only-show-errors >/dev/null || true

# 5. Demo teams and keys on the proxy (the same script as the local stack; keys in deploy/azure/.demo-keys/)
for i in $(seq 1 40); do curl -s -m 5 -o /dev/null -w "%{http_code}" "$LITELLM_URL/health/liveliness" | grep -q 200 && break; sleep 5; done
( cd examples/litellm && LITELLM_URL=$LITELLM_URL LITELLM_MASTER_KEY=$MASTER STATE_DIR=../../deploy/azure/.demo-keys REPORT_URL=$REPORT_URL ./demo.sh )
echo; echo "LiteLLM:   $LITELLM_URL/ui  (admin / the litellm-master-key secret in $KV)"
echo "Report:    $REPORT_URL/finops"; echo "Assistant: $ASSISTANT_URL"; echo "Keys:      deploy/azure/.demo-keys/*.key"
