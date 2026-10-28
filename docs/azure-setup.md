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

# one small deployment; the deployment name is what the gateway maps to
az cognitiveservices account deployment create -n $NAME -g $RG \
  --deployment-name gpt-4o-mini-dev \
  --model-name gpt-4o-mini --model-version 2024-07-18 --model-format OpenAI \
  --sku-name Standard --sku-capacity 10
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
LFG_AZURE_OPENAI_DEPLOYMENTS={"gpt-4o-mini": "gpt-4o-mini-dev"}
EOF
```

`.env` is git-ignored. For anything beyond a laptop, put the key in Key Vault and inject it as
an environment variable at runtime (Container Apps secret reference, or a managed identity and
`DefaultAzureCredential`, which is a planned provider option).

## 3. Run and call

```bash
llm-finops-gateway --port 8080
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Team: platform" -H "X-Project: azure-smoke" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"One sentence about FinOps."}]}' | jq .x_finops
curl -s http://localhost:8080/v1/finops/summary | jq
```

Compare `cost_usd` with what Azure Cost Management shows for the resource the next day; the
numbers should match to the cent for pay-as-you-go deployments (Azure publishes per-1K prices,
the price book uses per-1M).

## 4. Tear down

```bash
az group delete -n $RG --yes --no-wait
```
