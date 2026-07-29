from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.llm.base import LLMResponse, extract_chat_text, response_metadata
from app.llm.transient_network import (
    ROUTERAI_READ_TIMEOUT_NOT_RETRIED_MESSAGE,
    is_transient_http_status_code,
)


logger = logging.getLogger(__name__)
ROUTERAI_MAX_ATTEMPTS = 3
ROUTERAI_RETRY_BACKOFF_SECONDS = (5, 15)
ROUTERAI_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)
DEEPSEEK_MODEL_PREFIX = "deepseek/"


def is_deepseek_model(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith(DEEPSEEK_MODEL_PREFIX)


@dataclass
class RouterAIClient:
    api_key: str
    base_url: str
    model: str
    connect_timeout_seconds: int
    read_timeout_seconds: int
    write_timeout_seconds: int
    pool_timeout_seconds: int
    thinking_enabled: bool
    reasoning_effort: str | None = None
    provider: str = "routerai"

    @classmethod
    def from_settings(cls, *, model: str | None = None) -> "RouterAIClient":
        if not settings.routerai_api_key:
            raise RuntimeError("ROUTERAI_API_KEY is not set in .env")

        return cls(
            api_key=settings.routerai_api_key,
            base_url=settings.routerai_base_url.rstrip("/"),
            model=model or settings.llm_model or settings.routerai_model,
            connect_timeout_seconds=settings.routerai_connect_timeout_seconds,
            read_timeout_seconds=settings.routerai_read_timeout_seconds,
            write_timeout_seconds=settings.routerai_write_timeout_seconds,
            pool_timeout_seconds=settings.routerai_pool_timeout_seconds,
            thinking_enabled=settings.routerai_thinking_enabled,
            reasoning_effort=settings.routerai_reasoning_effort,
        )

    def request_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def timeout_config(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.write_timeout_seconds,
            pool=self.pool_timeout_seconds,
        )

    def supports_deepseek_thinking_payload(self) -> bool:
        return is_deepseek_model(self.model)

    def post_chat_completion(self, payload: dict[str, Any]) -> httpx.Response:
        for attempt in range(1, ROUTERAI_MAX_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=self.timeout_config()) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self.request_headers(),
                        json=payload,
                    )

                if (
                    is_transient_http_status_code(response.status_code)
                    and attempt < ROUTERAI_MAX_ATTEMPTS
                ):
                    backoff_seconds = ROUTERAI_RETRY_BACKOFF_SECONDS[attempt - 1]
                    self.log_retry(
                        attempt=attempt,
                        max_attempts=ROUTERAI_MAX_ATTEMPTS,
                        error_type=f"http_status_{response.status_code}",
                        backoff_seconds=backoff_seconds,
                    )
                    time.sleep(backoff_seconds)
                    continue

                return response
            except httpx.ReadTimeout as exc:
                raise RuntimeError(ROUTERAI_READ_TIMEOUT_NOT_RETRIED_MESSAGE) from exc
            except ROUTERAI_TRANSIENT_EXCEPTIONS as exc:
                if attempt >= ROUTERAI_MAX_ATTEMPTS:
                    raise

                backoff_seconds = ROUTERAI_RETRY_BACKOFF_SECONDS[attempt - 1]
                self.log_retry(
                    attempt=attempt,
                    max_attempts=ROUTERAI_MAX_ATTEMPTS,
                    error_type=exc.__class__.__name__,
                    backoff_seconds=backoff_seconds,
                )
                time.sleep(backoff_seconds)

        raise RuntimeError("RouterAI retry loop ended unexpectedly")

    def log_retry(
        self,
        *,
        attempt: int,
        max_attempts: int,
        error_type: str,
        backoff_seconds: int,
    ) -> None:
        logger.warning(
            "RouterAI transient request failure; retrying provider=%s attempt=%s "
            "max_attempts=%s error_type=%s backoff_seconds=%s",
            self.provider,
            attempt,
            max_attempts,
            error_type,
            backoff_seconds,
        )

    def build_payload(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        if self.supports_deepseek_thinking_payload():
            payload["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }

            effort = (self.reasoning_effort or "").strip().lower()
            if self.thinking_enabled and effort in {"high", "max"}:
                payload["reasoning_effort"] = effort

        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        return payload

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        payload = self.build_payload(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )

        started_at = time.perf_counter()
        response = self.post_chat_completion(payload)
        latency_seconds = time.perf_counter() - started_at

        if response.status_code != 200:
            raise RuntimeError(
                f"RouterAI chat error: status={response.status_code}, body={response.text[:4000]}"
            )

        raw_payload = response.json()
        response_id, usage = response_metadata(raw_payload)

        return LLMResponse(
            provider=self.provider,
            model=self.model,
            text=extract_chat_text(raw_payload, provider=self.provider),
            raw_payload=raw_payload,
            response_id=response_id,
            usage=usage,
            latency_seconds=latency_seconds,
        )
