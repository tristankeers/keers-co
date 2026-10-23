"""The language model behind the agents, and nothing else: optional, replaceable, and never trusted with numbers.

The agents run without a model. Set ``ANTHROPIC_API_KEY`` (and ``pip install -e ".[agents]"``) to have Claude
write the analysis; without it, each agent uses its stated rules (``rules-v1``) and says so. A paid API call is
the only cost here, so nothing calls a model unless the key is set.

``KEERS_AGENT_MODEL`` picks the model (default ``claude-opus-5-5``), ``KEERS_AGENT_EFFORT`` the effort
(default ``medium``), and ``KEERS_AGENTS_OFFLINE=1`` turns the model off even when a key is present.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("keers.agents")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000


class LLMError(RuntimeError):
    """The model could not give a usable answer (no key, network, refusal, malformed reply)."""


@dataclass
class Reply:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Reply: ...


class Claude:
    """Claude through the official Anthropic SDK, with the reply held to a JSON schema (structured outputs)."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, client: Any = None) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - depends on the environment
                raise LLMError('the anthropic package is not installed: pip install -e ".[agents]"') from e
            client = anthropic.Anthropic(max_retries=2, timeout=300.0)
        self.client = client
        self.model = model
        self.effort = effort
        self.name = model

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Reply:
        try:
            msg = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as e:  # noqa: BLE001 - every SDK error means "no usable answer"; the caller falls back
            raise LLMError(f"model call failed: {type(e).__name__}: {e}") from e
        if msg.stop_reason == "refusal":
            raise LLMError("the model declined to answer")
        if msg.stop_reason == "max_tokens":
            raise LLMError("the model's answer was cut off")
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("the model's answer was not valid JSON") from e
        if not isinstance(data, dict):
            raise LLMError("the model's answer was not a JSON object")
        u = getattr(msg, "usage", None)
        usage = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens")} if u is not None else {}
        return Reply(data, str(getattr(msg, "model", self.model)), usage, text)


def from_env() -> tuple[LLM | None, str]:
    """The configured model, or None with the reason the agents will use their rules instead."""
    if os.environ.get("KEERS_AGENTS_OFFLINE", "").strip().lower() in ("1", "true", "yes"):
        return None, "KEERS_AGENTS_OFFLINE is set"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no ANTHROPIC_API_KEY set"
    try:
        llm = Claude(os.environ.get("KEERS_AGENT_MODEL", "").strip() or DEFAULT_MODEL,
                     os.environ.get("KEERS_AGENT_EFFORT", "").strip() or DEFAULT_EFFORT)  # fmt: skip
    except LLMError as e:
        return None, str(e)
    return llm, f"model {llm.name}"
