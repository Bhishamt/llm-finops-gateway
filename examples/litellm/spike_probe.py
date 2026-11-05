"""Spike for ADR-0019: print what each LiteLLM proxy hook can see. Enforces nothing.

Facts to establish before the plugin is written:
1. does the pre-call hook see the request headers (X-Project, X-Use-Case)?
2. does user_api_key_dict carry the team alias of the calling key?
3. does the success event carry the final token usage for a streamed call?
4. what cost does LiteLLM compute for a model priced at zero, and is the budget checked?
"""

from __future__ import annotations

import json
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


def _show(tag: str, **fields: Any) -> None:
    print(f"[probe] {tag} " + json.dumps(fields, default=str)[:1500], flush=True)


class Probe(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # type: ignore[override]
        req = data.get("proxy_server_request") or {}
        headers = req.get("headers") or {}
        _show(
            "pre_call",
            call_type=call_type,
            model=data.get("model"),
            stream=data.get("stream"),
            team_id=getattr(user_api_key_dict, "team_id", None),
            team_alias=getattr(user_api_key_dict, "team_alias", None),
            key_alias=getattr(user_api_key_dict, "key_alias", None),
            x_project=headers.get("x-project"),
            x_use_case=headers.get("x-use-case"),
            header_keys=sorted(headers.keys()),
            data_keys=sorted(data.keys()),
            metadata_keys=sorted((data.get("metadata") or {}).keys()),
        )
        data.setdefault("metadata", {})["finops_probe"] = "set-in-pre-call"
        return data

    async def async_post_call_response_headers_hook(  # type: ignore[override]
        self, data, user_api_key_dict, response, request_headers=None
    ):
        meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
        _show(
            "response_headers",
            probe_meta=meta.get("finops_probe"),
            response_type=type(response).__name__,
        )
        return {"X-FinOps-Probe": "hello"}

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # type: ignore[override]
        slo = kwargs.get("standard_logging_object") or {}
        lp = kwargs.get("litellm_params") or {}
        usage = getattr(response_obj, "usage", None)
        _show(
            "success",
            model=kwargs.get("model"),
            stream=kwargs.get("stream"),
            response_cost=kwargs.get("response_cost"),
            slo_cost=slo.get("response_cost"),
            slo_model=slo.get("model"),
            slo_provider=slo.get("custom_llm_provider"),
            slo_prompt_tokens=slo.get("prompt_tokens"),
            slo_completion_tokens=slo.get("completion_tokens"),
            slo_metadata_team=(slo.get("metadata") or {}).get("user_api_key_team_alias"),
            usage=usage.model_dump() if hasattr(usage, "model_dump") else usage,
            probe_meta=(lp.get("metadata") or {}).get("finops_probe"),
            slo_status=slo.get("status"),
        )

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # type: ignore[override]
        lp = kwargs.get("litellm_params") or {}
        _show(
            "failure",
            model=kwargs.get("model"),
            error=str(kwargs.get("exception"))[:200],
            probe_meta=(lp.get("metadata") or {}).get("finops_probe"),
        )


probe = Probe()
