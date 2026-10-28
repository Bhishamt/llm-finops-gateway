"""Upstream LLM providers. Each adapter maps the OpenAI-style request to a vendor API."""

from .base import CompletionResult, Provider, ProviderError

__all__ = ["CompletionResult", "Provider", "ProviderError"]
