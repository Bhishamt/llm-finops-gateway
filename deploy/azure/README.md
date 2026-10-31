# Deploy to Azure Container Apps

The smallest shape that runs the gateway in Azure with its own identity: one Container App,
a user-assigned managed identity that may call Azure OpenAI (no provider key anywhere), an
Azure Database for PostgreSQL Flexible Server for the ledger (shared by replicas and readable
by Power BI), Log Analytics for logs. One Bicep file, no landing zone. Everything beyond this
(APIM, Private Link, Key Vault for team keys, VNet) is a deliberate next layer, not a
prerequisite. `usePostgres=false` swaps the database for an Azure Files share with SQLite
(one replica only).

## Cost, roughly (Sweden Central, late 2026)

| Item | Per day | Note |
| --- | ---: | --- |
| Container App, 0.25 vCPU / 0.5 GiB, one replica always on | ~0.45 USD | consumption plan after the monthly free grant; `minReplicas=0` makes it near zero when idle |
| Container Apps environment, Log Analytics | ~0 | 5 GB/month of logs are free |
| Container Registry Basic | ~0.17 USD | holds the image; `az acr build` builds in the cloud |
| PostgreSQL Flexible Server, Burstable B1ms, 32 GB | ~0.55 USD | the ledger; 7-day backups included |
| Azure OpenAI | usage | the calls themselves |

About 1.2 USD a day for a demo that is always on with its own database; with
`usePostgres=false` (SQLite on a 5 GB Azure Files share, ~0.01 USD/day) about half that.

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

# 3. Everything else (the PostgreSQL password goes to your secret store; clientIp is optional)
PGPASS=$(openssl rand -base64 24 | tr -d '/+=')
az deployment group create -g $RG -f deploy/azure/main.bicep \
  -p image=$ACR.azurecr.io/llm-finops-gateway:main \
  -p azureOpenAiResourceId=$AOAI_ID -p azureOpenAiEndpoint=$AOAI_EP \
  -p deployments='{"gpt-4.1-mini":"gpt-4.1-mini-dev"}' \
  -p keysYaml=@deploy/azure/keys.yaml -p reportRates='{"EUR":0.88}' \
  -p postgresPassword=$PGPASS -p clientIp=$(curl -s https://api.ipify.org) \
  --query properties.outputs.url.value -o tsv
```

The ledger tables are created on first start. For Power BI or any read-only consumer, add a
read-only role once (from a machine on the `clientIp` rule):

```sql
CREATE ROLE reporting LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE lfg TO reporting;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reporting;
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
- Reconcile from the laptop: `LFG_DATABASE_URL=postgresql+psycopg://... llm-finops-gateway ledger reconcile --resource $AOAI_ID --day ...`
  (the `clientIp` firewall rule lets the laptop reach the database).
- Tear down: `az deployment group delete` does not remove resources; delete the resource
  group, or the individual resources, when done.

## What to add for production

| Need | Add |
| --- | --- |
| Private database access | VNet-integrated environment and a private endpoint on the Flexible Server instead of the Azure-services firewall rule |
| Only signed-in staff on `/finops` | Container Apps authentication (Entra ID) in front of the app, or API Management |
| Private traffic to Azure OpenAI | VNet-integrated environment and a private endpoint on the account |
| Team keys as secrets with rotation | Key Vault secret referenced from the Container App instead of the inline parameter |
| Dashboards and alerts | Azure Monitor managed Prometheus scraping `/metrics`, Azure Managed Grafana with `grafana/llm-finops-gateway.json` |
