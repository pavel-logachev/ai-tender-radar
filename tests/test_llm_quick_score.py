from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import datetime
from decimal import Decimal
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

if "psycopg" not in sys.modules:
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *args, **kwargs: None
    sys.modules["psycopg"] = psycopg

if "psycopg.rows" not in sys.modules:
    psycopg_rows = types.ModuleType("psycopg.rows")
    psycopg_rows.dict_row = object()
    sys.modules["psycopg.rows"] = psycopg_rows

if "psycopg.types.json" not in sys.modules:
    psycopg_types = types.ModuleType("psycopg.types")
    psycopg_types_json = types.ModuleType("psycopg.types.json")

    class Jsonb:
        def __init__(self, value: object) -> None:
            self.value = value

    psycopg_types_json.Jsonb = Jsonb
    sys.modules["psycopg.types"] = psycopg_types
    sys.modules["psycopg.types.json"] = psycopg_types_json

if "httpx" not in sys.modules:
    httpx = types.ModuleType("httpx")

    class RequestError(Exception):
        pass

    class ConnectError(RequestError):
        pass

    class ConnectTimeout(RequestError):
        pass

    class ReadTimeout(RequestError):
        pass

    class HTTPStatusError(RequestError):
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

    httpx.RequestError = RequestError
    httpx.ConnectError = ConnectError
    httpx.ConnectTimeout = ConnectTimeout
    httpx.ReadTimeout = ReadTimeout
    httpx.HTTPStatusError = HTTPStatusError
    httpx.Request = Request
    httpx.Response = Response
    httpx.Client = Client
    httpx.Timeout = Timeout
    sys.modules["httpx"] = httpx

from app.llm import quick_score
from app.llm.base import LLMResponse


class FakeLLMClient:
    provider = "routerai"
    model = "deepseek/deepseek-v4-pro"

    def __init__(self, text: str) -> None:
        self.text = text
        self.call_kwargs: dict[str, object] = {}

    def generate_chat_completion(self, **kwargs: object) -> LLMResponse:
        self.call_kwargs = kwargs
        return LLMResponse(
            provider=self.provider,
            model=self.model,
            text=self.text,
            raw_payload={"choices": []},
        )


class LLMQuickScoreTest(unittest.TestCase):
    def test_build_input_uses_card_data_and_document_flags_only(self) -> None:
        payload = quick_score.build_quick_score_input(
            {
                "tender_id": "tender-1",
                "external_id": "EXT-1",
                "title": "Storage array supply",
                "customer_name": None,
                "initial_price": Decimal("12000000.00"),
                "deadline_at": datetime(2026, 5, 10, 12, 0),
                "region": "Moscow",
                "procedure_type": "Auction",
                "score": 85,
                "recommendation": "go",
                "confidence": "high",
                "result": {
                    "summary": "Hardware supply",
                    "why_relevant": ["storage"],
                    "risks": ["short deadline"],
                },
                "raw": {
                    "description": "Short API card text",
                    "full": {
                        "placerOrganization": {"fullName": "Customer LLC"},
                        "contactPerson": "Ivan Ivanov",
                        "contactPhone": "+7 000 000-00-00",
                        "contactEMail": "buyer@example.test",
                        "deliveryPlace": "Moscow",
                        "deliveryTerm": "30 days",
                        "documents": [{"title": "Terms of reference"}],
                    },
                    "short": {"etpName": "ETP"},
                },
                "docs_count": 2,
                "docs_with_text": 1,
                "document_risk_result": {
                    "tailoring_risk": "medium",
                    "risk_score": 45,
                    "documents_count": 2,
                    "documents_with_text": 1,
                },
            },
            business_signals={"market_access": "target_hardware"},
        )

        self.assertEqual(payload["tender"]["customer"], "Customer LLC")
        self.assertEqual(payload["tender"]["price_nmc"], 12000000.0)
        self.assertEqual(payload["contact_from_card"]["phone"], "+7 000 000-00-00")
        self.assertEqual(payload["business_relevance_signals"]["market_access"], "target_hardware")
        self.assertEqual(payload["document_availability_flags"]["card_documents_count"], 1)
        self.assertEqual(payload["document_availability_flags"]["downloaded_documents_with_text_count"], 1)
        self.assertNotIn("technical_spec_documents", payload)

    def test_normalize_quick_score_returns_stable_schema(self) -> None:
        normalized = quick_score.normalize_quick_score(
            {
                "report": {
                    "decision": "GO",
                    "confidence": "HIGH",
                    "why_interesting": ["a", "b", "c", "d", "e"],
                    "main_risks": "risk",
                    "missing_data": None,
                    "should_run_full_analysis": True,
                    "next_action_short": "Download docs and check spec",
                    "extra": "ignored",
                }
            }
        )

        self.assertEqual(
            list(normalized.keys()),
            [
                "decision",
                "confidence",
                "why_interesting",
                "main_risks",
                "missing_data",
                "should_run_full_analysis",
                "next_action_short",
            ],
        )
        self.assertEqual(normalized["decision"], "go")
        self.assertEqual(normalized["confidence"], "high")
        self.assertEqual(normalized["why_interesting"], ["a", "b", "c", "d"])
        self.assertEqual(normalized["main_risks"], ["risk"])
        self.assertEqual(normalized["should_run_full_analysis"], "yes")

    def test_normalize_quick_score_derives_full_analysis_gate(self) -> None:
        self.assertEqual(
            quick_score.normalize_quick_score({"decision": "go"})["should_run_full_analysis"],
            "yes",
        )
        self.assertEqual(
            quick_score.normalize_quick_score(
                {"decision": "maybe", "missing_data": ["technical spec"]}
            )["should_run_full_analysis"],
            "yes",
        )
        self.assertEqual(
            quick_score.normalize_quick_score(
                {"decision": "no_go", "should_run_full_analysis": "yes"}
            )["should_run_full_analysis"],
            "no",
        )

    def test_run_llm_quick_score_uses_json_mode_and_parses_fenced_json(self) -> None:
        fake_client = FakeLLMClient(
            """```json
{"decision":"maybe","confidence":"medium","why_interesting":["profile fit"],"main_risks":[],"missing_data":["spec"],"should_run_full_analysis":"yes","next_action_short":"Run docs stage"}
```"""
        )

        with patch.object(quick_score, "create_llm_client", return_value=fake_client):
            result = quick_score.run_llm_quick_score(
                {"tender_id": "1", "title": "Server supply", "raw": {}},
                max_output_tokens=321,
            )

        self.assertEqual(fake_client.call_kwargs["json_mode"], True)
        self.assertEqual(fake_client.call_kwargs["max_tokens"], 321)
        self.assertEqual(result.report["decision"], "maybe")
        self.assertEqual(result.report["should_run_full_analysis"], "yes")

    def test_run_llm_quick_score_raises_parse_error_with_detail(self) -> None:
        fake_client = FakeLLMClient("not json")

        with patch.object(quick_score, "create_llm_client", return_value=fake_client):
            with self.assertRaises(quick_score.QuickScoreParseError) as ctx:
                quick_score.run_llm_quick_score(
                    {"tender_id": "1", "title": "Server supply", "raw": {}},
                )

        self.assertIn("Cannot parse LLM quick-score JSON", ctx.exception.detail)
        self.assertIn("response_preview=not json", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
