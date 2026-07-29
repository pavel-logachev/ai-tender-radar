from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    text: str | None
    raw_payload: dict[str, Any]
    response_id: str | None = None
    usage: dict[str, Any] | None = None
    latency_seconds: float | None = None


class LLMClient(Protocol):
    provider: str
    model: str

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        ...


def extract_chat_text(payload: dict[str, Any], *, provider: str) -> str | None:
    try:
        content = payload["choices"][0]["message"].get("content")
    except Exception as exc:
        raise RuntimeError(f"Cannot extract {provider} response text: {payload}") from exc

    return str(content) if content is not None else None


def response_metadata(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    response_id = payload.get("id")
    usage = payload.get("usage")

    return (
        str(response_id) if response_id else None,
        usage if isinstance(usage, dict) else None,
    )
