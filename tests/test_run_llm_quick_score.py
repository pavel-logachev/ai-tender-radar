from __future__ import annotations

import contextlib
import importlib
import io
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

business_profile = types.ModuleType("app.business_profile")
business_profile.category_priority = lambda cfg: 0
business_profile.get_price = lambda row: int(row.get("initial_price") or 0)
business_profile.is_excluded_vertical = lambda row, profile: False
business_profile.is_full_deal_for_category = lambda row, profile, category_cfg: True
business_profile.is_low_priority_deal = lambda row, profile: False
business_profile.load_business_profile = lambda path: {}
business_profile.match_target_category = lambda row, profile: ("storage", {"label": "Storage"})
sys.modules.setdefault("app.business_profile", business_profile)

business_rules = types.ModuleType("app.business_rules")
business_rules.business_assessment = lambda row: {
    "commercial_interest": "high",
    "market_access": "target_hardware",
    "tailoring_effect": "neutral",
    "action": "proceed",
}
business_rules.effective_recommendation = lambda row: "go"
sys.modules.setdefault("app.business_rules", business_rules)

pipeline = importlib.import_module("app.pipeline.run_llm_quick_score")
from app.llm.base import LLMResponse


class RunLLMQuickScoreTest(unittest.TestCase):
    def test_main_continues_after_one_failed_candidate(self) -> None:
        rows = [
            {"tender_id": "tid-bad", "external_id": "bad", "title": "Bad LLM response", "initial_price": 10},
            {"tender_id": "tid-good", "external_id": "good", "title": "Good LLM response", "initial_price": 20},
        ]
        calls: list[str] = []

        def fake_run_quick_score_for_candidate(row: dict, **kwargs: object) -> object:
            external_id = str(row["external_id"])
            calls.append(external_id)
            if external_id == "bad":
                raise RuntimeError("Cannot parse JSON from LLM quick-score response: not json")

            return type(
                "Result",
                (),
                {
                    "report": {
                        "decision": "go",
                        "confidence": "medium",
                        "should_run_full_analysis": "yes",
                    }
                },
            )()

        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_quick_score", "--profile", "stub.yaml"]),
            patch.object(
                pipeline,
                "load_business_profile",
                return_value={"llm_quick_score": {"default_limit": 2, "default_pool_limit": 2}},
            ),
            patch.object(pipeline, "select_candidates", return_value=rows),
            patch.object(pipeline, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(pipeline, "get_price", side_effect=lambda row: int(row.get("initial_price") or 0)),
            patch.object(
                pipeline,
                "run_quick_score_for_candidate",
                side_effect=fake_run_quick_score_for_candidate,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            pipeline.main()

        output = stdout.getvalue()
        self.assertEqual(calls, ["bad", "good"])
        self.assertIn("LLM quick-score candidate failed: external_id=bad reason=quick_score_failed", output)
        self.assertIn("- succeeded: 1", output)
        self.assertIn("- failed: 1", output)
        self.assertIn("- top decisions: go=1, maybe=0, no_go=0", output)

    def test_run_candidate_saves_parse_failure_and_reraises(self) -> None:
        response = LLMResponse(
            provider="routerai",
            model="deepseek/deepseek-v4-pro",
            text="not json",
            raw_payload={},
        )
        error = pipeline.QuickScoreParseError(
            "Cannot parse LLM quick-score JSON: response_preview=not json",
            input_payload={"tender": {"external_id": "bad"}},
            response=response,
            raw_response="not json",
        )

        with (
            patch.object(pipeline, "run_llm_quick_score", side_effect=error),
            patch.object(pipeline, "save_llm_quick_score_error") as save_error,
        ):
            with self.assertRaises(pipeline.QuickScoreParseError):
                pipeline.run_quick_score_for_candidate(
                    {"tender_id": "tid-bad", "external_id": "bad", "raw": {}},
                    max_output_tokens=700,
                    json_mode=True,
                )

        save_error.assert_called_once()

    def test_dry_run_does_not_call_llm(self) -> None:
        rows = [
            {"tender_id": "tid-1", "external_id": "one", "title": "One", "initial_price": 10},
        ]
        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_quick_score", "--profile", "stub.yaml", "--dry-run"]),
            patch.object(pipeline, "load_business_profile", return_value={}),
            patch.object(pipeline, "select_candidates", return_value=rows),
            patch.object(pipeline, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(pipeline, "get_price", side_effect=lambda row: int(row.get("initial_price") or 0)),
            patch.object(pipeline, "run_quick_score_for_candidate") as run_mock,
            contextlib.redirect_stdout(stdout),
        ):
            pipeline.main()

        run_mock.assert_not_called()
        self.assertIn("Dry-run: no LLM calls were made", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
