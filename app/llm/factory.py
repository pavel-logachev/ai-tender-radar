from __future__ import annotations

from app.config import settings
from app.llm.base import LLMClient
from app.llm.gigachat_client import GigaChatClient
from app.llm.routerai_client import RouterAIClient


def create_llm_client(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> LLMClient:
    selected_provider = (provider or settings.llm_provider or "gigachat").strip().lower()

    if selected_provider == "gigachat":
        return GigaChatClient.from_settings(model=model)

    if selected_provider == "routerai":
        return RouterAIClient.from_settings(model=model)

    raise RuntimeError(f"Unsupported LLM provider: {selected_provider}")
