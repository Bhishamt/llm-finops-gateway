"""The report page as a LiteLLM dashboard plugin (ADR-0024).

LiteLLM's dashboard loads a registered plugin's ``url`` in an iframe and posts a short-lived
identity claim to it (``{type: "litellm-auth", session_claim}``). The claim is a Fernet token
encrypted with a key LiteLLM derives per plugin: ``urlsafe_b64(HMAC-SHA256(LITELLM_SALT_KEY,
plugin_name))``, carrying ``{plugin, user_id, user_role, exp}`` and valid for 30 seconds.

This module verifies such claims, turns an accepted claim into an HttpOnly session cookie for
the report page, and serves the small shell page the iframe loads first. Only LiteLLM admin
roles get a session; the report then shows every team, as a ``role: finops`` key would.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

CLAIM_TTL_SECONDS = 30
SESSION_COOKIE = "lfg_plugin_session"
SESSION_TTL_SECONDS = 8 * 3600
ADMIN_ROLES = frozenset({"proxy_admin", "proxy_admin_viewer"})


def derive_key(salt_key: str, plugin_name: str) -> bytes:
    """The per-plugin Fernet key LiteLLM derives: HMAC-SHA256 of the plugin name under
    LITELLM_SALT_KEY, URL-safe base64."""
    digest = hmac.new(salt_key.encode(), plugin_name.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


def verify_claim(salt_key: str, plugin_name: str, ciphertext: str) -> dict[str, Any]:
    """Decrypt and check a LiteLLM session claim; raises ValueError when it is not for this
    plugin, is tampered with, or is older than 30 seconds."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "the LiteLLM UI plugin needs the cryptography package: "
            "pip install 'llm-finops-gateway[litellm-ui]'"
        ) from exc
    try:
        raw = Fernet(derive_key(salt_key, plugin_name)).decrypt(
            ciphertext.encode(), ttl=CLAIM_TTL_SECONDS
        )
    except InvalidToken as exc:
        raise ValueError("invalid, tampered or expired plugin session claim") from exc
    claim = json.loads(raw)
    if not isinstance(claim, dict) or claim.get("plugin") != plugin_name:
        raise ValueError("plugin session claim is for another plugin")
    if int(claim.get("exp", 0)) < int(time.time()):
        raise ValueError("plugin session claim expired")
    return claim


def _sign(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def issue_session(salt_key: str, plugin_name: str, user_id: str, user_role: str) -> str:
    """An HMAC-signed session token for the report page, bound to the same derived key."""
    payload = json.dumps(
        {"u": user_id, "r": user_role, "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode()
    return f"{body}.{_sign(derive_key(salt_key, plugin_name), payload)}"


def verify_session(salt_key: str, plugin_name: str, token: str | None) -> dict[str, Any] | None:
    """The session's ``{user_id, user_role}`` when the token is intact and unexpired."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(body.encode())
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(derive_key(salt_key, plugin_name), payload)):
        return None
    data = json.loads(payload)
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return {"user_id": str(data.get("u", "")), "user_role": str(data.get("r", ""))}


def manifest(plugin_name: str, version: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "display_name": "FinOps",
        "version": version,
        "nav_items": [
            {"key": "report", "label": "FinOps report", "icon": "BarChartOutlined", "path": "/"}
        ],
        "capabilities": ["reports"],
    }


SHELL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>FinOps</title>
<style>body{font-family:system-ui,sans-serif;color:#313131;padding:2rem}</style></head>
<body><p id="msg">Waiting for the LiteLLM dashboard to sign you in&hellip;</p>
<script>
(function () {
  var msg = document.getElementById('msg');
  function done(ok, text) { msg.textContent = text; if (ok) { location.replace('/finops'); } }
  window.addEventListener('message', function (ev) {
    var d = ev.data || {};
    if (d.type !== 'litellm-auth' || !d.session_claim) { return; }
    fetch('/api/plugin-auth', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_claim: d.session_claim})
    }).then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (p) {
        var why = 'Sign-in refused: ' + (p[1].detail || 'unknown');
        done(p[0], p[0] ? 'Signed in, opening the report' : why);
      })
      .catch(function (e) { done(false, 'Sign-in failed: ' + e); });
  });
  fetch('/api/plugin-session', {credentials: 'include'})
    .then(function (r) { if (r.ok) { done(true, 'Opening the report'); } }).catch(function () {});
})();
</script></body></html>
"""
