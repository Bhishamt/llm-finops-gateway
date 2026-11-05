# 0024. The report page runs inside the LiteLLM dashboard as a plugin

Date: 2026-11-05
Status: Proposed

## Context

With the FinOps rules inside LiteLLM (ADR-0019), the operator's console is the LiteLLM
dashboard: keys, teams, usage and logs live there. The FinOps view (attribution, budgets,
price book version, the ledger) lives on the gateway's report page, which means a second URL
and a second login. LiteLLM's dashboard can embed an external page as a "plugin": the
dashboard loads the plugin's URL in an iframe and posts it a short-lived identity claim,
encrypted with a key derived per plugin from the proxy's salt key.

## Decision

- The gateway serves the report page as such a plugin when `LFG_LITELLM_PLUGIN_NAME` and
  `LFG_LITELLM_SALT_KEY` are set: `GET /` is the shell the iframe loads, `POST
  /api/plugin-auth` verifies the dashboard's claim, `GET /api/plugin-manifest` describes the
  plugin, `GET /api/plugin-session` tells the shell whether a session already exists. The
  LiteLLM side is one entry under `general_settings.plugins` with the plugin's name and the
  URL browsers reach the report at.
- The claim is verified exactly as LiteLLM issues it: a Fernet token under
  `urlsafe_b64(HMAC-SHA256(LITELLM_SALT_KEY, plugin_name))`, audience equal to the plugin
  name, expiry 30 seconds. The gateway therefore holds only the per-plugin derived input
  (the salt key is shared with it by the operator); it never sees a LiteLLM API key.
- Only LiteLLM admin roles (`proxy_admin`, `proxy_admin_viewer`) get a session; the
  session is an HttpOnly cookie signed with the same derived key, valid for eight hours,
  and it grants the report page the scope of a `role: finops` key: every team. Other roles
  are refused with 403; the LiteLLM user id is recorded as the report viewer.
- The cookie is set with `SameSite=None; Secure`, because the report is a third-party frame
  inside the dashboard. Browsers accept it on `localhost` and over HTTPS; a deployment that
  serves the report over plain HTTP on another host will not get a session, by design.
- Nothing else changes: the report page, its APIs and the team-key or finops-key access
  keep working outside the dashboard.

## Consequences

- One console for the operator: LiteLLM for keys and traffic, the FinOps tab for who spent
  what against which budget and price book version.
- The plugin surface is three small endpoints and no JavaScript beyond the shell; the
  LiteLLM contract (iframe, postMessage, Fernet claim) is pinned by a test that derives the
  key the way LiteLLM's documentation shows.
- The `cryptography` package becomes a dependency of this mode (`[litellm-ui]` extra, in the
  container image by default).
