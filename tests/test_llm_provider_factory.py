from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://user:password@localhost:5432/db")

if "pydantic_settings" not in sys.modules:
    pydantic_settings = types.ModuleType("pydantic_settings")

    class BaseSettings:
        def __init__(self) -> None:
            annotations: dict[str, object] = {}
            for cls in reversed(self.__class__.mro()):
                annotations.update(getattr(cls, "__annotations__", {}))

            for name in annotations:
                default = getattr(self.__class__, name, None)
                setattr(self, name, os.environ.get(name.upper(), default))

    pydantic_settings.BaseSettings = BaseSettings
    sys.modules["pydantic_settings"] = pydantic_settings

stale_config = sys.modules.get("app.config")
if stale_config is not None and not hasattr(
    getattr(stale_config, "settings", None),
    "llm_provider",
):
    sys.modules.pop("app.config", None)

stale_factory = sys.modules.get("app.llm.factory")
if stale_factory is not None and getattr(
    getattr(stale_factory, "create_llm_client", None),
    "__module__",
    None,
) != "app.llm.factory":
    sys.modules.pop("app.llm.factory", None)

sys.modules.pop("app.llm.routerai_client", None)

if "httpx" not in sys.modules:
    httpx = types.ModuleType("httpx")

    class ConnectError(Exception):
        pass

    class ConnectTimeout(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    class RemoteProtocolError(Exception):
        pass

    class HTTPStatusError(Exception):
        def __init__(self, message: str, *, request: object, response: object) -> None:
            super().__init__(message)
            self.request = request
            self.response = response

    class Request:
        def __init__(self, method: str, url: str) -> None:
            self.method = method
            self.url = url

    class Response:
        def __init__(self, status_code: int, *, request: object, headers: dict | None = None) -> None:
            self.status_code = status_code
            self.request = request
            self.headers = headers or {}

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class Timeout:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    httpx.Client = Client
    httpx.ConnectError = ConnectError
    httpx.ConnectTimeout = ConnectTimeout
    httpx.ReadTimeout = ReadTimeout
    httpx.RemoteProtocolError = RemoteProtocolError
    httpx.HTTPStatusError = HTTPStatusError
    httpx.Request = Request
    httpx.Response = Response
    httpx.Timeout = Timeout
    sys.modules["httpx"] = httpx

from app.config import settings
from app.llm.factory import create_llm_client
from app.llm.routerai_client import RouterAIClient
import app.llm.routerai_client as routerai_client


class LLMProviderFactoryTest(unittest.TestCase):
    def test_default_provider_remains_gigachat(self) -> None:
        with (
            patch.object(settings, "llm_provider", "gigachat"),
            patch.object(settings, "llm_model", None),
            patch.object(settings, "gigachat_auth_key", "secret"),
            patch.object(settings, "gigachat_model", "GigaChat-2-Pro"),
        ):
            client = create_llm_client()

        self.assertEqual(client.provider, "gigachat")
        self.assertEqual(client.model, "GigaChat-2-Pro")

    def test_routerai_uses_qwen_default_model(self) -> None:
        with (
            patch.object(settings, "llm_provider", "routerai"),
            patch.object(settings, "llm_model", None),
            patch.object(settings, "routerai_api_key", "secret"),
            patch.object(settings, "routerai_model", "qwen/qwen3.7-plus"),
        ):
            client = create_llm_client()

        self.assertEqual(client.provider, "routerai")
        self.assertEqual(client.model, "qwen/qwen3.7-plus")

    def test_routerai_manual_deepseek_override_works(self) -> None:
        with (
            patch.object(settings, "llm_provider", "routerai"),
            patch.object(settings, "llm_model", None),
            patch.object(settings, "routerai_api_key", "secret"),
            patch.object(settings, "routerai_model", "qwen/qwen3.7-plus"),
        ):
            client = create_llm_client(
                provider="routerai",
                model="deepseek/deepseek-v4-pro",
            )

        self.assertEqual(client.provider, "routerai")
        self.assertEqual(client.model, "deepseek/deepseek-v4-pro")

    def test_routerai_payload_disables_thinking_by_default_and_keeps_json_mode(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="deepseek/deepseek-v4-pro",
            connect_timeout_seconds=30,
            read_timeout_seconds=300,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=False,
        )

        payload = client.build_payload(
            system_prompt="system",
            user_prompt="user",
            temperature=0.1,
            max_tokens=4096,
            json_mode=True,
        )

        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", payload)

    def test_routerai_qwen_payload_omits_deepseek_thinking_fields(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="qwen/qwen3.7-plus",
            connect_timeout_seconds=30,
            read_timeout_seconds=900,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=True,
            reasoning_effort="max",
        )

        payload = client.build_payload(
            system_prompt="system",
            user_prompt="user",
            temperature=0.1,
            max_tokens=4096,
            json_mode=True,
        )

        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_routerai_payload_can_enable_thinking_with_reasoning_effort(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="deepseek/deepseek-v4-pro",
            connect_timeout_seconds=30,
            read_timeout_seconds=300,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=True,
            reasoning_effort="max",
        )

        payload = client.build_payload(
            system_prompt="system",
            user_prompt="user",
            temperature=0.1,
            max_tokens=4096,
            json_mode=False,
        )

        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertNotIn("response_format", payload)

    def test_routerai_retries_connect_error_once_and_succeeds(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="deepseek/deepseek-v4-pro",
            connect_timeout_seconds=30,
            read_timeout_seconds=300,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=False,
        )
        calls: list[dict[str, object]] = []

        class FakeResponse:
            status_code = 200
            text = "{}"

            def json(self) -> dict:
                return {
                    "id": "ok",
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"total_tokens": 1},
                }

        responses = [
            routerai_client.httpx.ConnectError("Temporary failure in name resolution"),
            FakeResponse(),
        ]

        class FakeHTTPClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeHTTPClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def post(self, *args: object, **kwargs: object) -> object:
                calls.append(kwargs)
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        with (
            patch.object(routerai_client.httpx, "Client", FakeHTTPClient),
            patch.object(routerai_client.time, "sleep") as sleep,
        ):
            response = client.generate_chat_completion(
                system_prompt="system",
                user_prompt="user",
                temperature=0.1,
                max_tokens=4096,
                json_mode=True,
            )

        self.assertEqual(response.text, "done")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(5)

    def test_routerai_does_not_retry_read_timeout(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="deepseek/deepseek-v4-pro",
            connect_timeout_seconds=30,
            read_timeout_seconds=900,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=False,
        )
        calls = 0

        class FakeHTTPClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeHTTPClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def post(self, *args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                raise routerai_client.httpx.ReadTimeout("read timed out")

        with (
            patch.object(routerai_client.httpx, "Client", FakeHTTPClient),
            patch.object(routerai_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.generate_chat_completion(
                    system_prompt="system",
                    user_prompt="user",
                    temperature=0.1,
                    max_tokens=4096,
                    json_mode=True,
                )

        self.assertIn("RouterAI read timeout", str(ctx.exception))
        self.assertIn("not retrying automatically", str(ctx.exception))
        self.assertEqual(calls, 1)
        sleep.assert_not_called()

    def test_routerai_does_not_retry_json_parse_error(self) -> None:
        client = RouterAIClient(
            api_key="secret",
            base_url="https://routerai.ru/api/v1",
            model="deepseek/deepseek-v4-pro",
            connect_timeout_seconds=30,
            read_timeout_seconds=300,
            write_timeout_seconds=30,
            pool_timeout_seconds=30,
            thinking_enabled=False,
        )
        calls = 0

        class BadJSONResponse:
            status_code = 200
            text = "not json"

            def json(self) -> dict:
                raise ValueError("not json")

        class FakeHTTPClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeHTTPClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def post(self, *args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return BadJSONResponse()

        with (
            patch.object(routerai_client.httpx, "Client", FakeHTTPClient),
            patch.object(routerai_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(ValueError):
                client.generate_chat_completion(
                    system_prompt="system",
                    user_prompt="user",
                    temperature=0.1,
                    max_tokens=4096,
                    json_mode=True,
                )

        self.assertEqual(calls, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
