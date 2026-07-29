from __future__ import annotations

import argparse
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.llm.base import LLMResponse, extract_chat_text, response_metadata


@dataclass
class GigaChatClient:
    auth_key: str
    scope: str
    oauth_url: str
    api_base_url: str
    model: str
    verify_ssl: bool
    timeout_seconds: int

    access_token: str | None = None
    provider: str = "gigachat"

    @classmethod
    def from_settings(cls, *, model: str | None = None) -> "GigaChatClient":
        if not settings.gigachat_auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY is not set in .env")

        return cls(
            auth_key=settings.gigachat_auth_key,
            scope=settings.gigachat_scope,
            oauth_url=settings.gigachat_oauth_url,
            api_base_url=settings.gigachat_api_base_url.rstrip("/"),
            model=model or settings.llm_model or settings.gigachat_model,
            verify_ssl=settings.gigachat_verify_ssl,
            timeout_seconds=settings.gigachat_timeout_seconds,
        )

    def auth_header_value(self) -> str:
        value = self.auth_key.strip()

        if value.lower().startswith("basic "):
            return value

        return f"Basic {value}"

    def get_token(self) -> str:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": self.auth_header_value(),
        }

        data = {
            "scope": self.scope,
        }

        with httpx.Client(timeout=self.timeout_seconds, verify=self.verify_ssl) as client:
            response = client.post(
                self.oauth_url,
                headers=headers,
                data=data,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"GigaChat token error: status={response.status_code}, body={response.text[:2000]}"
            )

        payload = response.json()
        token = payload.get("access_token")

        if not token:
            raise RuntimeError(f"GigaChat token not found in response: {payload}")

        self.access_token = token
        return token

    def ensure_token(self) -> str:
        if self.access_token:
            return self.access_token

        return self.get_token()

    def request_headers(self) -> dict[str, str]:
        token = self.ensure_token()

        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def list_models(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_seconds, verify=self.verify_ssl) as client:
            response = client.get(
                f"{self.api_base_url}/v1/models",
                headers=self.request_headers(),
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"GigaChat models error: status={response.status_code}, body={response.text[:2000]}"
            )

        return response.json()

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
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

        if response_format:
            payload["response_format"] = response_format

        with httpx.Client(timeout=self.timeout_seconds, verify=self.verify_ssl) as client:
            response = client.post(
                f"{self.api_base_url}/v1/chat/completions",
                headers=self.request_headers(),
                json=payload,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"GigaChat chat error: status={response.status_code}, body={response.text[:4000]}"
            )

        return response.json()

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        started_at = time.perf_counter()
        payload = self.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"} if json_mode else None,
        )
        latency_seconds = time.perf_counter() - started_at
        response_id, usage = response_metadata(payload)

        return LLMResponse(
            provider=self.provider,
            model=self.model,
            text=extract_chat_text(payload, provider=self.provider),
            raw_payload=payload,
            response_id=response_id,
            usage=usage,
            latency_seconds=latency_seconds,
        )

    def chat_text(self, **kwargs) -> str:
        return self.generate_chat_completion(**kwargs).text or ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe GigaChat API")
    parser.add_argument("--models", action="store_true", help="List available models")
    args = parser.parse_args()

    client = GigaChatClient.from_settings()

    token = client.get_token()
    print("GigaChat auth OK")
    print(f"token_length: {len(token)}")

    if args.models:
        models = client.list_models()
        print("Models:")
        for item in models.get("data", []):
            print("-", item.get("id") or item)


if __name__ == "__main__":
    main()
