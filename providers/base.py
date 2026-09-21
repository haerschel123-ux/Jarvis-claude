"""Provider-independent chat interface (Spec §10, §67).

Every model backend — OpenRouter, Ollama, any OpenAI-compatible endpoint — is reduced to the
same small surface so the model router, the agents and the tool engine never need to know
which one they are talking to.

Capability flags are deliberately **tri-state**: ``True``, ``False`` or ``None`` for unknown.
The specification is explicit that missing provider metadata must surface as *Unknown* rather
than be guessed (Spec §8).
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

# Where a piece of content came from. The assistant core uses this to keep untrusted text
# out of the instruction channel (Spec §90).
Trust = Literal["system", "user", "memory", "external", "tool_result"]


@dataclass(slots=True)
class ToolCall:
    """A model's request to run a tool."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    # Base64-encoded images (data URLs) for vision-capable models.
    images: list[str] = field(default_factory=list)
    trust: Trust = "user"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "images": len(self.images),
            "trust": self.trust,
        }


@dataclass(slots=True)
class ToolSpec:
    """A tool as offered to a model."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


ChunkType = Literal["text", "reasoning", "tool_call", "usage", "done", "error"]


@dataclass(slots=True)
class StreamChunk:
    """One piece of a streaming response."""

    type: ChunkType
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ChatResult:
    """The fully assembled result of a chat call."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model_id: str = ""
    provider: str = ""
    finish_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "reasoning": self.reasoning,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "usage": self.usage.to_dict(),
            "model_id": self.model_id,
            "provider": self.provider,
            "finish_reason": self.finish_reason,
        }


@dataclass(slots=True)
class ModelInfo:
    """One model in the catalogue.

    ``price_prompt``/``price_completion`` are kept as the provider's own strings (USD per
    token) so no precision is invented; ``None`` means the provider did not say.
    """

    provider: str
    id: str
    name: str = ""
    context_length: int | None = None
    price_prompt: str | None = None
    price_completion: str | None = None
    is_free: bool = False
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_structured: bool | None = None
    supports_reasoning: bool | None = None
    is_local: bool = False
    is_router: bool = False
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    def capability(self, flag: bool | None) -> str:
        """Render a tri-state flag for the UI: Yes / No / Unknown (Spec §8)."""
        if flag is None:
            return "Unknown"
        return "Yes" if flag else "No"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "id": self.id,
            "key": self.key,
            "name": self.name or self.id,
            "context_length": self.context_length,
            "price_prompt": self.price_prompt,
            "price_completion": self.price_completion,
            "is_free": self.is_free,
            "is_local": self.is_local,
            "is_router": self.is_router,
            "description": self.description,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_structured": self.supports_structured,
            "supports_reasoning": self.supports_reasoning,
            "display": {
                "tools": self.capability(self.supports_tools),
                "vision": self.capability(self.supports_vision),
                "structured_output": self.capability(self.supports_structured),
                "reasoning": self.capability(self.supports_reasoning),
                "price_input": self.price_prompt if self.price_prompt is not None else "Unknown",
                "price_output": self.price_completion if self.price_completion is not None else "Unknown",
                "context": self.context_length if self.context_length is not None else "Unknown",
            },
        }


class ChatProvider(abc.ABC):
    """Interface every model backend implements."""

    name: str = "base"
    label: str = "Base"
    is_local: bool = False

    @abc.abstractmethod
    async def is_available(self) -> tuple[bool, str]:
        """Return ``(available, reason)``. The reason explains *why* when unavailable."""

    @abc.abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Fetch the current model catalogue from the provider."""

    @abc.abstractmethod
    def stream_chat(
        self,
        messages: Sequence[Message],
        model: str,
        *,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion as :class:`StreamChunk` values."""

    async def chat(
        self,
        messages: Sequence[Message],
        model: str,
        **kwargs: Any,
    ) -> ChatResult:
        """Collect a streamed response into a single :class:`ChatResult`."""
        result = ChatResult(model_id=model, provider=self.name)
        parts: list[str] = []
        reasoning: list[str] = []
        async for chunk in self.stream_chat(messages, model, **kwargs):
            if chunk.type == "text":
                parts.append(chunk.text)
            elif chunk.type == "reasoning":
                reasoning.append(chunk.text)
            elif chunk.type == "tool_call" and chunk.tool_call is not None:
                result.tool_calls.append(chunk.tool_call)
            elif chunk.type == "usage" and chunk.usage is not None:
                result.usage = chunk.usage
            elif chunk.type == "done":
                result.finish_reason = chunk.finish_reason
            elif chunk.type == "error":
                from core.errors import ProviderError

                raise ProviderError(chunk.error or "Unbekannter Fehler des Anbieters")
        result.text = "".join(parts)
        result.reasoning = "".join(reasoning)
        return result

    async def close(self) -> None:
        """Release any network resources."""
        return None


def messages_to_openai(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert to the OpenAI chat format shared by OpenRouter, Ollama's /v1 and others."""
    payload: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"role": message.role}

        if message.images and message.role == "user":
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            for image in message.images:
                url = image if image.startswith("data:") else f"data:image/png;base64,{image}"
                content.append({"type": "image_url", "image_url": {"url": url}})
            entry["content"] = content
        else:
            entry["content"] = message.content

        if message.name:
            entry["name"] = message.name
        if message.tool_call_id:
            entry["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            import json

            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
            # An assistant turn that only calls tools has no textual content.
            entry.setdefault("content", "")
        payload.append(entry)
    return payload
