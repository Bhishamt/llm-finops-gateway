# 0014. Azure OpenAI with Entra ID: the gateway calls with its own identity

Date: 2026-10-30
Status: Accepted

## Context

Until now the Azure OpenAI adapter sent an API key in the `api-key` header. Keys are what a
laptop uses; an enterprise Azure estate disables them (`disableLocalAuth`) and expects every
caller to present an Entra ID token, so that access is a role assignment that can be audited
and revoked, not a secret that can leak. The deployment guide already said "managed identity
is on the roadmap and removes the key entirely".

The choice was between a bespoke token fetch against the Entra endpoint and the Azure SDK's
`DefaultAzureCredential`, which resolves environment service-principal variables, a workload
or managed identity, and the developer's `az login` session in that order.

## Decision

- `LFG_AZURE_OPENAI_AUTH=entra` switches the adapter to `Authorization: Bearer <token>` for the
  scope `https://cognitiveservices.azure.com/.default`; `LFG_AZURE_OPENAI_API_KEY` is then
  not required. The default stays `api_key`, so nothing changes for existing setups.
- Tokens come from the synchronous `DefaultAzureCredential`, called in a worker thread through
  a small `TokenProvider` protocol, and are cached until five minutes before expiry. A token is
  fetched about once an hour, so the thread hop is negligible and the async credential's extra
  HTTP stack (`aiohttp`) is not needed. The adapter asks for the token per request, so a
  rotated or expired token is picked up without a restart.
- `azure-identity` is an optional extra (`pip install "llm-finops-gateway[azure]"`); it is
  included in the Docker image because the image exists for Azure deployments. Selecting
  `entra` without the extra fails at startup with the install hint.
- The identity needs the built-in role `Cognitive Services OpenAI User` on the resource (or
  the account); the setup guide shows the `az role assignment create` line.

## Consequences

- No provider secret in the gateway's environment on Azure: the Container App's managed
  identity is the caller, and Key Vault is only needed for the team keys.
- The same configuration works on a developer laptop through `az login`.
- Startup cannot verify the role assignment; a missing role shows up as the upstream's 401,
  returned as `502 upstream_error` with Azure's message, and role propagation can take a few
  minutes after assignment.
- The generic OpenAI-compatible adapter keeps bearer API keys only; Entra tokens for the Azure
  v1 endpoint can reuse the same provider later if needed.
