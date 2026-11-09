#!/usr/bin/env bash
# Upload the price book, the policy file and the LiteLLM config to the finops-config share of
# the Azure deployment. The proxy and the report reload the price book and the policies on the
# next call (ADR-0022); the LiteLLM config itself is read at startup only.
#
#   deploy/azure/push-config.sh                    # config/pricing.yaml + examples/litellm/policies.yaml
#   LFG_POLICIES=path/to/policies.yaml deploy/azure/push-config.sh
# Needs: RG (resource group), STORAGE (storage account), REPORT_URL (for litellm.yaml), az login.
set -euo pipefail
cd "$(dirname "$0")/../.."
RG=${RG:-rg-llm-finops-gateway-dev}
STORAGE=${STORAGE:-$(az storage account list -g "$RG" --query "[0].name" -o tsv)}
SHARE=${SHARE:-finops-config}
PRICING=${LFG_PRICING:-config/pricing.yaml}
POLICIES=${LFG_POLICIES:-examples/litellm/policies.yaml}
KEY=$(az storage account keys list -n "$STORAGE" -g "$RG" --query "[0].value" -o tsv)
up() { az storage file upload --account-name "$STORAGE" --account-key "$KEY" --share-name "$SHARE" --source "$1" --path "$2" --only-show-errors >/dev/null; echo "uploaded $1 -> $SHARE/$2"; }
up "$PRICING" pricing.yaml
up "$POLICIES" policies.yaml
if [ -n "${REPORT_URL:-}" ]; then
  tmp=$(mktemp); sed "s|REPORT_URL|$REPORT_URL|" deploy/azure/litellm.config.yaml > "$tmp"
  up "$tmp" litellm.yaml; rm -f "$tmp"
fi
