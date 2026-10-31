# Deploy to Azure Container Apps

The smallest shape that runs the gateway in Azure with its own identity: one Container App,
a user-assigned managed identity that may call Azure OpenAI (no provider key anywhere), an
Azure Files share for the SQLite ledger, Log Analytics for logs. One Bicep file, no landing
zone. Everything beyond this (APIM, Private Link, Key Vault for team keys, PostgreSQL, VNet)
is a deliberate next layer, not a prerequisite.

## Cost, roughly (Sweden Central, late 2026)

| Item | Per day | Note |
| --- | ---: | --- |
| Container App, 0.25 vCPU / 0.5 GiB, one replica always on | ~0.45 USD | consumption plan after the monthly free grant; `minReplicas=0` makes it near zero when idle |
| Container Apps environment, Log Analytics | ~0 | 5 GB/month of logs are free |
| Container Registry Basic | ~0.17 USD | holds the image; `az acr build` builds in the cloud |
| Storage account, 5 GB Azure Files | ~0.01 USD | the ledger; mounted with `nobrl` so SQLite works over SMB, one replica only |
| Azure OpenAI | usage | the calls themselves |

About half a dollar a day for a demo that is always on. PostgreSQL (Flexible Server B1ms,
about 0.6 USD/day) is the first thing to add for anything shared between people.

## Steps

```bash
RG=rg-llm-finops-gateway-dev            # the Azure OpenAI resource's group, or a new one
LOC=swedencentral
ACR=lfgacr$RANDOM                       # globally unique, lowercase
AOAI_ID=$(az cognitiveservices account show -n <aoai-name> -g <aoai-rg> --query id -o tsv)
AOAI_EP=$(az cognitiveservices account show -n <aoai-name> -g <aoai-rg> --query properties.endpoint -o tsv)

# 1. Registry and image (built in the cloud from this checkout; nothing to push from the laptop)
az acr create -n $ACR -g $RG -l $LOC --sku Basic
az acr build -r $ACR -t llm-finops-gateway:main .

# 2. Team keys: generate, keep the raw keys in your secret store, pass the hashes file
llm-finops-gateway keys new --team platform --id platform-azure           # paste into keys.yaml
llm-finops-gateway keys new --team finops --id finops-report --role finops
cp config/keys.yaml deploy/azure/keys.yaml  # with the entries added; git-ignored

# 3. Everything else
az deployment group create -g $RG -f deploy/azure/main.bicep \
  -p image=$ACR.azurecr.io/llm-finops-gateway:main \
  -p azureOpenAiResourceId=$AOAI_ID -p azureOpenAiEndpoint=$AOAI_EP \
  -p deployments='{"gpt-4.1-mini":"gpt-4.1-mini-dev"}' \
  -p keysYaml=@deploy/azure/keys.yaml -p reportRates='{"EUR":0.88}' \
  --query properties.outputs.url.value -o tsv
```

The role assignment on the Azure OpenAI account can take up to half an hour to reach the
data plane; until then calls answer `502` with `PermissionDenied` from Azure. `/healthz`
works immediately.

## After deployment

```bash
URL=https://<the output above>
curl -s $URL/healthz
curl -s $URL/v1/chat/completions -H "Authorization: Bearer lfg_..." -H "X-Project: smoke" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
open $URL/finops            # sign in with the finops key
```

- Logs: `az containerapp logs show -n lfg-gateway -g $RG --follow`.
- New image: `az acr build -r $ACR -t llm-finops-gateway:main .` then
  `az containerapp update -n lfg-gateway -g $RG --image $ACR.azurecr.io/llm-finops-gateway:main`.
- Reconcile from the laptop as before: `llm-finops-gateway ledger reconcile --resource $AOAI_ID --day ...`
  with `LFG_DATABASE_URL` pointing at a copy of the ledger, or run it inside the app with
  `az containerapp exec`.
- Tear down: `az deployment group delete` does not remove resources; delete the resource
  group, or the individual resources, when done.

## What to add for production

| Need | Add |
| --- | --- |
| Several replicas, people reading reports | PostgreSQL Flexible Server and `LFG_DATABASE_URL=postgresql+psycopg://...` |
| Only signed-in staff on `/finops` | Container Apps authentication (Entra ID) in front of the app, or API Management |
| Private traffic to Azure OpenAI | VNet-integrated environment and a private endpoint on the account |
| Team keys as secrets with rotation | Key Vault secret referenced from the Container App instead of the inline parameter |
| Dashboards and alerts | Azure Monitor managed Prometheus scraping `/metrics`, Azure Managed Grafana with `grafana/llm-finops-gateway.json` |
