# Testing against your own Azure tenant

Minimal, disposable setup for trying the gateway against Azure OpenAI. Everything below is
`az` CLI; adapt names and region. Delete the resource group when done.

## 1. Resources

```bash
RG=rg-llm-finops-gateway-dev
LOC=swedencentral            # pick a region with the model you want
NAME=aoai-lfg-dev-$RANDOM

az group create -n $RG -l $LOC
az cognitiveservices account create -n $NAME -g $RG -l $LOC \
  --kind OpenAI --sku S0 --custom-domain $NAME

# one small deployment; the deployment name is what the gateway maps to.
# gpt-4o-mini 2024-07-18 cannot be deployed any more (deprecated for new deployments since
# 2026-03-31); gpt-4.1-mini is the cheapest current model in the price book.
# Capacity is thousands of tokens per minute; 10 is too small for the demo's bursts and Azure
# answers 429, which the gateway reports as 502.
az cognitiveservices account deployment create -n $NAME -g $RG \
  --deployment-name gpt-4.1-mini-dev \
  --model-name gpt-4.1-mini --model-version 2025-04-14 --model-format OpenAI \
  --sku-name Standard --sku-capacity 50
```

Check what is deployable in your region before picking a model:

```bash
az cognitiveservices model list -l $LOC \
  --query "[?model.format=='OpenAI'].{name:model.name, version:model.version, lifecycle:model.lifecycleStatus}" -o table
```

## 2. Credentials into `.env`

```bash
ENDPOINT=$(az cognitiveservices account show -n $NAME -g $RG --query properties.endpoint -o tsv)
KEY=$(az cognitiveservices account keys list -n $NAME -g $RG --query key1 -o tsv)

cat > .env <<EOF
LFG_PROVIDER=azure_openai
LFG_AZURE_OPENAI_ENDPOINT=$ENDPOINT
LFG_AZURE_OPENAI_API_KEY=$KEY
LFG_AZURE_OPENAI_API_VERSION=2024-10-21
LFG_AZURE_OPENAI_DEPLOYMENTS={"gpt-4.1-mini": "gpt-4.1-mini-dev"}
EOF
```

The same resource also exposes the OpenAI v1 endpoint, which the generic adapter serves
(ADR-0010); the deployment name is the model there, so the map does the same job:

```bash
cat > .env <<EOF
LFG_PROVIDER=openai_compat
LFG_OPENAI_COMPAT_BASE_URL=${ENDPOINT%/}/openai/v1
LFG_OPENAI_COMPAT_API_KEY=$KEY
LFG_OPENAI_COMPAT_MODELS={"gpt-4.1-mini": "gpt-4.1-mini-dev"}
EOF
```

`.env` is git-ignored. For anything beyond a laptop, use the gateway's own identity instead of
a key (ADR-0014): give it the `Cognitive Services OpenAI User` role and set
`LFG_AZURE_OPENAI_AUTH=entra`. On a laptop the identity is your `az login` session:

```bash
pip install -e ".[azure]"
ME=$(az ad signed-in-user show --query id -o tsv)
RES=$(az cognitiveservices account show -n $NAME -g $RG --query id -o tsv)
az role assignment create --assignee-object-id $ME --assignee-principal-type User \
  --role "Cognitive Services OpenAI User" --scope $RES
# a few minutes later:
LFG_AZURE_OPENAI_AUTH=entra LFG_AZURE_OPENAI_API_KEY= llm-finops-gateway serve
```

In Container Apps the same two settings point at the app's managed identity; Key Vault is then
only needed for the team keys.

## 3. Run and call

```bash
llm-finops-gateway --port 8080
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Team: platform" -H "X-Project: azure-smoke" \
  -d '{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"One sentence about FinOps."}]}' | jq .x_finops
curl -s http://localhost:8080/v1/finops/summary | jq
```

Compare `cost_usd` with what Azure Cost Management shows for the resource the next day; the
numbers should match to the cent for pay-as-you-go deployments (Azure publishes per-1K prices,
the price book uses per-1M).

## 4. Tear down

```bash
az group delete -n $RG --yes --no-wait
```
