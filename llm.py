"""Unified LLM client factory — Anthropic API, bearer proxies (Bonsai), OpenRouter."""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_MODEL = "claude-sonnet-4-6"

_KEY_HELP = (
    "No LLM API key configured.\n"
    "  Anthropic direct:  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  OpenRouter:        export PACT_LLM_BASE_URL=https://openrouter.ai/api "
    "PACT_LLM_API_KEY=sk-or-...\n"
    "  Bonsai proxy:      export ANTHROPIC_BASE_URL=https://go.trybons.ai "
    "ANTHROPIC_AUTH_TOKEN=sk_cr_..."
)


def resolve_key(api_key: Optional[str] = None) -> str:
    """Resolve the API key string.

    Returns a non-empty key when one is available, or "" when ANTHROPIC_AUTH_TOKEN
    will provide credentials (SDK reads it automatically as Bearer auth).
    Raises RuntimeError if no auth is configured at all.
    """
    key = (
        api_key
        or os.environ.get("PACT_LLM_API_KEY")
        or os.environ.get("PACT_ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    if not key and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError(_KEY_HELP)
    return key


def resolve_model(model: Optional[str] = None) -> str:
    """Return model name, honouring PACT_LLM_MODEL env-var override."""
    return model or os.environ.get("PACT_LLM_MODEL") or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# MCP sampling backend — lets the MCP server delegate LLM calls to the host
# ---------------------------------------------------------------------------

from typing import Callable  # noqa: E402 (after stdlib imports above)

_sampling_fn: Optional[Callable[[list, int, str], str]] = None


def set_sampling_backend(fn: Callable[[list, int, str], str]) -> None:
    """Install a sampling function: (messages, max_tokens, system) -> text.

    When set, make_client() returns a mock client that uses this function
    instead of calling the Anthropic API directly. Used by the MCP server
    so LLM calls go through the host (Claude Code) via sampling/createMessage.
    """
    global _sampling_fn
    _sampling_fn = fn


def clear_sampling_backend() -> None:
    global _sampling_fn
    _sampling_fn = None


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class _SamplingResponse:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"


class _SamplingMessages:
    def __init__(self, fn: Callable) -> None:
        self._fn = fn

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system: str = "",
        tools: Optional[list] = None,
        **_: object,
    ) -> _SamplingResponse:
        # Tools are stripped — source context is pre-injected in the prompt.
        # Multi-turn tool use falls back gracefully: model works with prompt context.
        sys_str = system if isinstance(system, str) else ""
        text = self._fn(messages, max_tokens, sys_str)
        return _SamplingResponse(text)


class _SamplingClient:
    def __init__(self, fn: Callable) -> None:
        self.messages = _SamplingMessages(fn)


def make_client(api_key: Optional[str] = None):
    """Return an Anthropic client, or a sampling-backed mock when in MCP context."""
    if _sampling_fn is not None:
        return _SamplingClient(_sampling_fn)

    import anthropic

    key = (
        api_key
        or os.environ.get("PACT_LLM_API_KEY")
        or os.environ.get("PACT_ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    base_url = os.environ.get("PACT_LLM_BASE_URL") or os.environ.get(
        "ANTHROPIC_BASE_URL"
    )

    if not key and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError(_KEY_HELP)

    kw: dict = {}
    if key:
        kw["api_key"] = key
    if base_url:
        kw["base_url"] = base_url
    return anthropic.Anthropic(**kw)
