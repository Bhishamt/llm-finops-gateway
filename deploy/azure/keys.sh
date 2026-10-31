#!/usr/bin/env bash
# Generate the gateway's team keys for an Azure deployment and store them where people can find them.
#
#   deploy/azure/keys.sh <key vault name> [team:id[:role] ...]
#   deploy/azure/keys.sh lfg-kv-abc123 platform:platform-azure finops:finops-report:finops
#
# Writes deploy/azure/keys.yaml (hashes only, git-ignored; the Bicep parameter keysYaml) and
# puts each raw key into Key Vault as secret team-key-<id>. Read one later with:
#   az keyvault secret show --vault-name <vault> --name team-key-finops-report --query value -o tsv
set -euo pipefail
VAULT=${1:?key vault name}; shift
[ $# -gt 0 ] || set -- platform:platform-azure finops:finops-report:finops
OUT=$(dirname "$0")/keys.yaml
printf 'version: "%s"\nkeys:\n' "$(date +%Y-%m-%d)" > "$OUT"
for spec in "$@"; do
  IFS=: read -r team id role <<< "$spec"
  raw=$(python3 -c "import secrets; print('lfg_' + secrets.token_urlsafe(32))")
  digest=$(printf '%s' "$raw" | shasum -a 256 | cut -d' ' -f1)
  {
    printf '  - id: %s\n    team: %s\n    sha256: %s\n    created: %s\n' "$id" "$team" "$digest" "$(date +%Y-%m-%d)"
    [ -n "${role:-}" ] && printf '    role: %s\n' "$role"
  } >> "$OUT"
  az keyvault secret set --vault-name "$VAULT" --name "team-key-$id" --value "$raw" \
    --content-type "raw gateway key for team $team (${role:-team})" --query id -o tsv
done
echo "wrote $OUT; raw keys are in Key Vault $VAULT as team-key-<id>"
