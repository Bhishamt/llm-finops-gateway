# Deploy to Azure Container Apps

The smallest shape that runs the gateway in Azure with its own identity: one Container App,
a user-assigned managed identity that may call Azure OpenAI (no provider key anywhere), an
Azure Database for PostgreSQL Flexible Server for the ledger (shared by replicas and readable
by BI tools), Log Analytics for logs. One Bicep file, no landing zone. Everything beyond this
(APIM, Private Link, Key Vault for team keys, VNet) is a deliberate next layer, not a
prerequisite. `usePostgres=false` swaps the database for an Azure Files share with SQLite
(one replica only).

## Cost, roughly (Sweden Central, late 2026)

| Item | Per day | Note |
| --- | ---: | --- |
| Container App, 0.25 vCPU / 0.5 GiB, one replica always on | ~0.45 USD | consumption plan after the monthly free grant; `minReplicas=0` makes it near zero when idle |
| Container Apps environment, Log Analytics | ~0 | 5 GB/month of logs are free |
| Container Registry Basic | ~0.17 USD | holds the image; `az acr build` builds in the cloud |
| PostgreSQL Flexible Server, Burstable B1ms, 32 GB | ~0.55 USD | the ledger; 7-day backups included (B2ms about 0.7 USD; some subscriptions get no Burstable SKU in some regions, hence `postgresLocation`) |
| Key Vault standard | ~0 | a few secrets, a few operations |
| Azure OpenAI | usage | the calls themselves |

About 1.2 USD a day for a demo that is always on with its own database; with
`usePostgres=false` (SQLite on a 5 GB Azure Files share, ~0.01 USD/day) about half that.

## Steps

Everything lands in one resource group, including the Azure OpenAI account and its model
deployments (`createAzureOpenAi=true` in the commands below). The intent is one region as well;
`postgresLocation` exists for the case where that is not possible. To reuse an existing account
instead, pass `createAzureOpenAi=false`, `azureOpenAiResourceId` and `azureOpenAiEndpoint`.

### Region decision for the reference deployment (2026-10-31)

The gateway and the Azure OpenAI account run in **Sweden Central**: it is the EU region where
the models we price (gpt-4.1-mini, gpt-5-mini) have pay-as-you-go `GlobalStandard` SKUs and
where the test account already lived. The PostgreSQL ledger runs in **North Europe**, because
on this free-trial subscription Sweden Central answers `SkuNotAvailable` for every Burstable
PostgreSQL SKU (B1ms and B2ms tried), West Europe is closed to new subscriptions ("the selected
region is currently not accepting new customers"), and North Europe has no pay-as-you-go SKU
for the models. One region for everything was therefore not available to this subscription,
not a design choice. The database round trip Sweden Central to North Europe adds a few
milliseconds per call, which is irrelevant next to the model latency. On a subscription
without these regional limits set `postgresLocation` to the app's region, or deploy to France
Central, which had both at the time of writing. Data residency: both regions are EU; if the
ledger must stay in the same country as the app, drop `usePostgres` to `false` (SQLite on
Azure Files in the app's region) until the subscription can create PostgreSQL there.

```bash
RG=rg-llm-finops-gateway
LOC=uksouth
ACR=lfgacr$RANDOM                       # globally unique, lowercase
az group create -n $RG -l $LOC

# 1. Registry and image (built in the cloud from this checkout; nothing to push from the laptop)
az acr create -n $ACR -g $RG -l $LOC --sku Basic
az acr build -r $ACR -t llm-finops-gateway:main .

# 2. Everything in Azure (first run: an empty keys file is fine, keys come in step 3)
printf 'version: "init"\nkeys: []\n' > deploy/azure/keys.yaml
az deployment group create -g $RG -f deploy/azure/main.bicep \
  -p image=$ACR.azurecr.io/llm-finops-gateway:main \
  -p createAzureOpenAi=true \
  -p keysYaml=@deploy/azure/keys.yaml -p reportRates='{"EUR":0.88}' \
  -p adminObjectId=$(az ad signed-in-user show --query id -o tsv) \
  -p clientIp=$(curl -s https://api.ipify.org) \
  --query properties.outputs -o json
# outputs: url, keyVaultName, postgresHost, azureOpenAiResourceId, azureOpenAiEndpoint
# modelDeployments defaults to one gpt-4.1-mini GlobalStandard deployment named gpt-4.1-mini;
# pass -p modelDeployments=@deployments.json for more (name, model, version, sku, capacity).

# 3. Team keys: hashes into keys.yaml, raw keys into Key Vault; then redeploy with the hashes
deploy/azure/keys.sh <keyVaultName> platform:platform-azure finops:finops-report:finops
az deployment group create -g $RG -f deploy/azure/main.bicep ... -p keysYaml=@deploy/azure/keys.yaml \
  -p postgresPassword=$(az keyvault secret show --vault-name <keyVaultName> --name postgres-password --query value -o tsv)
```

## Where the secrets are

Everything a person may need to log in or connect is in the Key Vault the deployment creates
(RBAC mode; the deployer gets Key Vault Secrets Officer, the app's identity Secrets User):

| Secret | What |
| --- | --- |
| `team-key-<id>` | the raw team keys from `keys.sh` (`lfg_...`): sign in to `/finops`, call the API |
| `postgres-password` | admin password of `lfgadmin`; generated on the first deployment, pass it back on later runs to keep it |
| `database-url` | the SQLAlchemy URL the app uses (`LFG_DATABASE_URL`, read by the app from Key Vault with its identity); `lfg_app` after `db_roles.py` |
| `postgres-app-password`, `postgres-reporting-password` | passwords of `lfg_app` and `lfg_reporting` (`db_roles.py`) |
| `database-url-reporting` | read-only URL for BI tools, psql, `ledger reconcile` |

```bash
az keyvault secret show --vault-name <keyVaultName> --name team-key-finops-report --query value -o tsv
```

The gateway itself stores only SHA-256 hashes of the team keys (the `keysYaml` parameter) and
never the raw values.

# 4. Database roles, once: the app stops running as the admin, readers get a read-only role
pip install -e ".[postgres]"
python deploy/azure/db_roles.py --vault <keyVaultName> --resource-group $RG
# creates lfg_app (gateway) and lfg_reporting (read-only, BI tools), stores their passwords and URLs in
# Key Vault, switches database-url to lfg_app and restarts the app. On later deployments pass
#   -p databaseUrl="$(az keyvault secret show --vault-name <keyVaultName> --name database-url --query value -o tsv)"
# so the template keeps the app-role URL. Schema and grants: docs/database.md.
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

## The LiteLLM direction: proxy, plugin, report, assistant

`litellm.bicep` deploys three more Container Apps into the same environment, for a team that
runs LiteLLM and wants the FinOps rules inside it (ADR-0019, ADR-0024):

| App | Image | What it serves |
| --- | --- | --- |
| `lfg-litellm` | `examples/litellm/Dockerfile` (LiteLLM with the plugin installed) | the proxy on `/v1/...`, the LiteLLM dashboard on `/ui`, LiteLLM metrics plus `lfg_*` on `/metrics/` |
| `lfg-report` | the gateway image | `/finops` over the shared ledger, in `api_key` mode, plus the dashboard-plugin endpoints |
| `lfg-assistant` | `examples/assistant/Dockerfile` | the Streamlit page people sign in to with a LiteLLM key |

It reuses what `main.bicep` created: the environment, the identity (AcrPull), the registry, the
PostgreSQL server (a new `litellm` database for LiteLLM's own tables; the plugin and the report
write the gateway's ledger through the `database-url` secret) and the Key Vault. The config
files live on an Azure Files share `finops-config` mounted read-only at `/config` in the proxy
and the report; the LiteLLM master and salt keys are created once as Key Vault secrets
`litellm-master-key` and `litellm-salt-key`.

```bash
RG=rg-llm-finops-gateway-dev ACR=lfgacr1325 deploy/azure/litellm-deploy.sh
# builds the three images, reads the secrets, deploys litellm.bicep, uploads the config files,
# restarts the proxy once (LiteLLM reads its own config at startup), creates the demo teams and
# keys with examples/litellm/demo.sh; the keys land in deploy/azure/.demo-keys/
```

Editing a budget afterwards is one command; the proxy and the report pick it up on the next
call, no restart:

```bash
# edit examples/litellm/policies.yaml (or config/pricing.yaml), then
RG=rg-llm-finops-gateway-dev deploy/azure/push-config.sh
```

A key that leaks spends on your Azure OpenAI account until its team's budget blocks it, so keep
the demo budgets tiny and give warn-only teams a second net on the LiteLLM side:
`POST /key/update {"key": "...", "max_budget": 2}` with the master key. Delete the demo keys
(`/key/delete`) and scale the apps to zero, or delete them, when the demo is over.

The dashboard plugin needs HTTPS for its session cookie; Container Apps ingress provides it.
Cost: about 0.5 USD a day on top of the gateway (three small apps on the consumption plan, the
proxy with 0.5 vCPU / 1 GiB; the database, registry and share are shared), plus the model calls.

## What to add for production

| Need | Add |
| --- | --- |
| Private database access | VNet-integrated environment and a private endpoint on the Flexible Server instead of the Azure-services firewall rule |
| Only signed-in staff on `/finops` | Container Apps authentication (Entra ID) in front of the app, or API Management |
| Private traffic to Azure OpenAI | VNet-integrated environment and a private endpoint on the account |
| Key rotation | add a new entry with `keys.sh`, redeploy, switch the client, set `disabled: true` on the old entry |
| Dashboards and alerts | Azure Monitor managed Prometheus scraping `/metrics`, Azure Managed Grafana with `grafana/llm-finops-gateway.json` |
