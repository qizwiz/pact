"""Unified LLM client factory — Anthropic API, bearer proxies (Bonsai), OpenRouter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=False)

DEFAULT_MODEL = "claude-sonnet-4-6"

_KEY_HELP = (
    "No LLM API key configured.\n"
    "  Anthropic direct:  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  OpenRouter:        export PACT_LLM_BASE_URL=https://openrouter.ai/api "
    "PACT_LLM_API_KEY=sk-or-...\n"
    "  Bonsai proxy:      export ANTHROPIC_BASE_URL=https://go.trybons.ai "
    "ANTHROPIC_AUTH_TOKEN=sk_cr_..."
)


def make_client(api_key: Optional[str] = None):
    """Return an anthropic.Anthropic client.

    Auth priority: explicit api_key → ANTHROPIC_API_KEY → PACT_LLM_API_KEY →
    PACT_ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN (bearer token, SDK reads env directly).
    Base-URL override: ANTHROPIC_BASE_URL or PACT_LLM_BASE_URL.
    """
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
