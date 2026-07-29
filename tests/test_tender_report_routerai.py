from __future__ import annotations

import json
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

yaml = types.ModuleType("yaml")
yaml.safe_load = lambda text: {}
sys.modules.setdefault("yaml", yaml)

psycopg = types.ModuleType("psycopg")
psycopg.rows = types.ModuleType("psycopg.rows")
psycopg.rows.dict_row = object()
psycopg.types = types.ModuleType("psycopg.types")
psycopg.types.json = types.ModuleType("psycopg.types.json")
psycopg.types.json.Jsonb = lambda value: value
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", psycopg.rows)
sys.modules.setdefault("psycopg.types", psycopg.types)
sys.modules.setdefault("psycopg.types.json", psycopg.types.json)

config = types.ModuleType("app.config")
config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
sys.modules.setdefault("app.config", config)

factory = types.ModuleType("app.llm.factory")
factory.create_llm_client = lambda **kwargs: None
sys.modules.setdefault("app.llm.factory", factory)

from app.llm.base import LLMResponse
from app.llm import tender_report


class FakeRouterAIClient:
    provider = "routerai"
    model = "fake-routerai"
    thinking_enabled = True

    def __init__(self) -> None:
        self.analysis_calls = 0
        self.packaging_calls = 0

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        if system_prompt == tender_report.ROUTERAI_ANALYSIS_SYSTEM_PROMPT:
            self.analysis_calls += 1
            payload = {
                "summary": "analysis summary",
                "what_is_required": ["storage"],
                "top_risks": ["delivery"],
                "delivery_feasibility": "check stock",
                "potential_offer": ["storage offer"],
                "problems_in_tz": [],
                "questions_to_customer": ["deadline?"],
                "next_action": ["ask presales"],
                "verdict": "maybe",
                "confidence": "medium",
            }
            return self.response(payload)

        self.packaging_calls += 1
        if self.packaging_calls == 1:
            return LLMResponse(
                provider=self.provider,
                model=self.model,
                text="Here is the report, not JSON.",
                raw_payload={"choices": [{"finish_reason": "stop"}]},
            )

        payload = {
            "summary": "final summary",
            "verdict": "maybe",
            "confidence": "medium",
            "what_is_required": ["storage"],
            "top_risks": ["delivery"],
            "delivery_feasibility": "check stock",
            "potential_offer": ["storage offer"],
            "problems_in_tz": [],
            "questions_to_customer": ["deadline?"],
            "next_action": ["ask presales"],
        }
        return self.response(payload)

    def response(self, payload: dict) -> LLMResponse:
        return LLMResponse(
            provider=self.provider,
            model=self.model,
            text=json.dumps(payload),
            raw_payload={"choices": [{"finish_reason": "stop"}]},
        )


class FakeRouterAIAnalysisRetryClient(FakeRouterAIClient):
    def __init__(self) -> None:
        super().__init__()
        self.analysis_max_tokens: list[int] = []

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        if system_prompt == tender_report.ROUTERAI_ANALYSIS_SYSTEM_PROMPT:
            self.analysis_calls += 1
            self.analysis_max_tokens.append(max_tokens)
            if self.analysis_calls == 1:
                return LLMResponse(
                    provider=self.provider,
                    model=self.model,
                    text='{"summary": "cut", "delivery_feasibility": {',
                    raw_payload={"choices": [{"finish_reason": "length"}]},
                )

            payload = {
                "summary": "retry analysis summary",
                "what_is_required": ["storage"],
                "top_risks": ["delivery"],
                "delivery_feasibility": "check stock",
                "potential_offer": ["storage offer"],
                "problems_in_tz": [],
                "questions_to_customer": ["deadline?"],
                "next_action": ["ask presales"],
                "verdict": "maybe",
                "confidence": "medium",
            }
            return self.response(payload)

        self.packaging_calls += 1
        payload = {
            "summary": "final summary",
            "verdict": "maybe",
            "confidence": "medium",
            "what_is_required": ["storage"],
            "top_risks": ["delivery"],
            "delivery_feasibility": "check stock",
            "potential_offer": ["storage offer"],
            "problems_in_tz": [],
            "questions_to_customer": ["deadline?"],
            "next_action": ["ask presales"],
        }
        return self.response(payload)


class FakeRouterAIAnalysisRetryFailsClient(FakeRouterAIAnalysisRetryClient):
    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        if system_prompt == tender_report.ROUTERAI_ANALYSIS_SYSTEM_PROMPT:
            self.analysis_calls += 1
            return LLMResponse(
                provider=self.provider,
                model=self.model,
                text='{"summary": "cut"',
                raw_payload={"choices": [{"finish_reason": "length"}]},
            )

        return super().generate_chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )


class FakeLeadClient:
    provider = "routerai"
    model = "fake-routerai"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
            }
        )
        payload = {
            "lead_summary": "lead summary",
            "customer_signal": "storage refresh signal",
            "likely_customer_story": "capacity pressure",
            "possible_needs": ["backup", "monitoring"],
            "target_roles": ["IT director", "infrastructure owner"],
            "opening_phrase": "Saw your storage purchase and wanted to compare roadmap notes.",
            "discovery_questions": ["What triggered the refresh?"],
            "handoff_to_manager_when": ["Customer confirms a live project"],
            "lead_priority": "high",
            "why_priority": "Relevant infrastructure signal",
            "next_action": "Find infrastructure owner",
        }
        return LLMResponse(
            provider=self.provider,
            model=self.model,
            text=json.dumps(payload),
            raw_payload={"choices": [{"finish_reason": "stop"}]},
        )


class CapturingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "CapturingCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))


class CapturingConnection:
    def __init__(self) -> None:
        self.cursor_obj = CapturingCursor()
        self.committed = False

    def __enter__(self) -> "CapturingConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> CapturingCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True


class RouterAITwoPassTest(unittest.TestCase):
    def test_report_kind_normalization_and_analysis_type_are_backward_compatible(self) -> None:
        self.assertEqual(tender_report.normalize_report_kind(None), "technical")
        self.assertEqual(tender_report.normalize_report_kind("customer_lead"), "lead")
        self.assertEqual(tender_report.analysis_type_for_label(None), "llm_presales_report")
        self.assertEqual(
            tender_report.analysis_type_for_label(None, report_kind="lead"),
            "llm_customer_lead_report",
        )
        self.assertEqual(
            tender_report.analysis_type_for_label("smoke", report_kind="lead"),
            "llm_customer_lead_report_smoke",
        )
        self.assertEqual(tender_report.output_stem("EXT-1", None), "EXT-1")
        self.assertEqual(
            tender_report.output_stem("EXT-1", None, report_kind="lead"),
            "EXT-1.lead",
        )

    def test_lead_prompt_declares_customer_development_schema(self) -> None:
        prompt = tender_report.build_lead_report_user_prompt("Tender context")

        for field in tender_report.LEAD_REPORT_FIELDS:
            self.assertIn(field, prompt)
        self.assertIn("customer lead report", prompt)
        self.assertIn("not a bid-participation task", prompt)
        self.assertIn("Do not write a technical TZ report", prompt)
        self.assertIn("relationship-first", prompt)
        self.assertIn("confidence must be one of", prompt)
        self.assertIn("procurement_contact_role", prompt)
        self.assertIn("target_end_customer_roles", prompt)
        self.assertIn("at most 3", prompt)
        self.assertIn("Do not open with", prompt)
        self.assertIn("call-first", prompt)
        self.assertIn("LinkedIn", prompt)
        self.assertIn("Do not sell every tender as a good lead", prompt)
        self.assertIn("hypothesis as weak", prompt)
        self.assertIn("Do not invent a large data center", prompt)
        self.assertIn("limited account-development signal", prompt)
        self.assertIn("document_status.analysis_basis", prompt)
        self.assertIn("lead_card_only", prompt)
        self.assertIn("primary technical document", prompt)

    def test_normalize_lead_report_keeps_exact_schema_and_priority_enum(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "summary": "fallback summary",
                "customer_signal": 123,
                "likely_customer_story": "story",
                "possible_needs": "need one",
                "target_roles": ("CIO", "CTO"),
                "procurement_contact_role": "procurement specialist",
                "target_end_customer_roles": "end customer IT owner",
                "opening_phrase": "hello",
                "discovery_questions": ["question"],
                "handoff_to_manager_when": "confirmed need",
                "lead_priority": "urgent",
                "confidence": "HIGH",
                "why_priority": "why",
                "next_action": "call",
                "extra": "ignored",
            }
        )

        self.assertEqual(tuple(normalized), tender_report.LEAD_REPORT_FIELDS)
        self.assertEqual(normalized["lead_summary"], "fallback summary")
        self.assertEqual(normalized["customer_signal"], "123")
        self.assertEqual(normalized["possible_needs"], ["need one"])
        self.assertEqual(normalized["target_roles"], ["CIO", "CTO"])
        self.assertEqual(normalized["procurement_contact_role"], "procurement specialist")
        self.assertEqual(normalized["target_end_customer_roles"], ["end customer IT owner"])
        self.assertEqual(normalized["handoff_to_manager_when"], ["confirmed need"])
        self.assertEqual(normalized["lead_priority"], "medium")
        self.assertEqual(normalized["confidence"], "high")

    def test_normalize_lead_report_downgrades_confidence_for_card_only_context(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "lead_summary": "lead summary",
                "customer_signal": "server signal",
                "likely_customer_story": "story",
                "lead_priority": "high",
                "confidence": "high",
                "next_action": "call",
            },
            package={
                "meta": {
                    "report_kind": "lead",
                    "lead_context_mode": "card_only",
                    "lead_documents_missing_allowed": True,
                }
            },
        )

        self.assertEqual(normalized["confidence"], "medium")
        self.assertEqual(
            normalized["document_status"]["analysis_basis"],
            "lead_card_only",
        )

    def test_lead_report_persistence_maps_medium_priority_to_maybe_with_confidence(self) -> None:
        connection = CapturingConnection()
        with patch.object(tender_report.psycopg, "connect", return_value=connection, create=True):
            tender_report.save_llm_report(
                tender_id="tender-id",
                provider="routerai",
                model="qwen/qwen3.7-plus",
                parsed_report={"lead_priority": "medium"},
                raw_response="{}",
                context_chars=10,
                analysis_type=tender_report.LLM_CUSTOMER_LEAD_REPORT_ANALYSIS_TYPE,
                metadata={"report_kind": "lead"},
            )

        insert_params = [
            params
            for sql, params in connection.cursor_obj.calls
            if "INSERT INTO analysis_results" in sql
        ][0]
        self.assertEqual(insert_params[-2], "maybe")
        self.assertEqual(insert_params[-1], "medium")
        self.assertEqual(
            tender_report.persistence_recommendation_and_confidence(
                {"lead_priority": "low"},
                analysis_type=tender_report.LLM_CUSTOMER_LEAD_REPORT_ANALYSIS_TYPE,
            ),
            ("maybe", "low"),
        )
        self.assertTrue(connection.committed)

    def test_lead_next_action_replaces_linkedin_and_telegram_primary_action(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": ["Who owns infrastructure?"],
                "next_action": (
                    "Find the IT owner through LinkedIn and send a "
                    "Telegram message with the purchase context."
                ),
            }
        )

        self.assertEqual(normalized["next_action"], tender_report.LEAD_DEFAULT_NEXT_ACTION)
        self.assertNotIn("LinkedIn", normalized["next_action"])
        self.assertNotIn("Telegram", normalized["next_action"])

        allowed = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": ["Who owns infrastructure?"],
                "next_action": (
                    "Call the procurement contact and ask for the IT owner; "
                    "LinkedIn can be used only as an additional contact source."
                ),
            }
        )
        self.assertIn("Call the procurement contact", allowed["next_action"])
        self.assertIn("LinkedIn", allowed["next_action"])

    def test_lead_target_roles_are_deduplicated_and_limited(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": ["Who owns infrastructure?"],
                "target_roles": [
                    "IT",
                    "IT",
                    "IT director",
                    "IT director",
                    "Infrastructure owner",
                    "Technical customer",
                    "Procurement contact",
                    "Regional IT curator",
                ],
                "target_end_customer_roles": [
                    "IT",
                    "End-customer IT responsible",
                    "Technical customer",
                    "Technical customer",
                    "Regional IT curator",
                    "Infrastructure owner",
                ],
            }
        )

        self.assertLessEqual(len(normalized["target_roles"]), 4)
        self.assertEqual(len(normalized["target_roles"]), len(set(normalized["target_roles"])))
        self.assertNotIn("IT", normalized["target_roles"])
        self.assertIn("IT director", normalized["target_roles"])

        self.assertLessEqual(len(normalized["target_end_customer_roles"]), 4)
        self.assertEqual(
            len(normalized["target_end_customer_roles"]),
            len(set(normalized["target_end_customer_roles"])),
        )
        self.assertNotIn("IT", normalized["target_end_customer_roles"])
        self.assertIn("End-customer IT responsible", normalized["target_end_customer_roles"])

    def test_lead_opening_phrase_with_depo_and_procedure_wording_uses_relationship_fallback(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": (
                    "\u0417\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435, "
                    "\u043f\u043e \u0432\u0430\u0448\u0435\u0439 \u0437\u0430\u043a\u0443\u043f\u043a\u0435 "
                    "\u043f\u043b\u0430\u043d\u0438\u0440\u0443\u0435\u0442\u0441\u044f "
                    "\u043c\u043e\u0434\u0435\u0440\u043d\u0438\u0437\u0430\u0446\u0438\u044f "
                    "\u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432 DEPO Storm "
                    "\u0441 \u0441\u0435\u0440\u0438\u0439\u043d\u044b\u043c\u0438 "
                    "\u043d\u043e\u043c\u0435\u0440\u0430\u043c\u0438."
                ),
                "discovery_questions": ["Who owns infrastructure?"],
            },
            package={
                "tender": {
                    "title": "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432"
                }
            },
        )

        phrase = normalized["opening_phrase"]
        self.assertTrue(phrase.startswith("Добрый день."))
        self.assertIn(
            "\u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e "
            "\u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443",
            phrase,
        )
        self.assertIn("коротко познакомиться", phrase)
        self.assertIn("будущим задачам", phrase)
        self.assertNotIn("DEPO Storm", phrase)
        self.assertNotIn("\u0437\u0430\u043a\u0443\u043f", phrase.casefold())
        self.assertNotIn("\u043f\u0440\u043e\u0446\u0435\u0434\u0443\u0440", phrase.casefold())
        self.assertNotIn("\u0441\u0435\u0440\u0438\u0439\u043d", phrase.casefold())

    def test_lead_opening_phrase_storage_uses_storage_subject_not_server(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": (
                    "\u0414\u043e\u0431\u0440\u044b\u0439 \u0434\u0435\u043d\u044c. "
                    "\u041f\u043e\u0434\u0441\u043a\u0430\u0436\u0438\u0442\u0435, "
                    "\u043a\u0442\u043e \u0443 \u0432\u0430\u0441 "
                    "\u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442 "
                    "\u0437\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e "
                    "\u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443?"
                ),
                "discovery_questions": ["Who owns infrastructure?"],
            },
            package={
                "tender": {
                    "title": (
                        "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 "
                        "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u0430 "
                        "\u043c\u043e\u0434\u0435\u0440\u043d\u0438\u0437\u0430\u0446\u0438\u0438 "
                        "\u0440\u0435\u043f\u043e\u0437\u0438\u0442\u043e\u0440\u0438\u044f "
                        "\u0434\u0430\u043d\u043d\u044b\u0445 \u0434\u043b\u044f "
                        "\u0440\u0430\u0437\u0432\u0438\u0442\u0438\u044f \u0426\u041e\u041c\u0414"
                    )
                }
            },
        )

        phrase = normalized["opening_phrase"].casefold()
        self.assertIn("\u0445\u0440\u0430\u043d\u0435\u043d\u0438", phrase)
        self.assertIn("\u0441\u0445\u0434", phrase)
        self.assertNotIn(
            "\u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e "
            "\u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443",
            phrase,
        )

    def test_lead_discovery_questions_are_limited_to_three(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": [
                    "Who owns server infrastructure?",
                    "Who plans infrastructure development?",
                    "Can we meet the IT owner?",
                    "What adjacent projects are planned?",
                    "Who owns budget?",
                ],
            }
        )

        self.assertEqual(
            normalized["discovery_questions"],
            [
                "Who owns server infrastructure?",
                "Who plans infrastructure development?",
                "Can we meet the IT owner?",
            ],
        )

    def test_lead_discovery_questions_remove_procedure_first_items(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": [
                    "\u041a\u0430\u043a\u043e\u0439 \u0441\u0440\u043e\u043a \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438?",
                    "\u041a\u0430\u043a \u0438\u0434\u0435\u0442 \u043f\u043e\u0434\u0430\u0447\u0430 \u0437\u0430\u044f\u0432\u043a\u0438?",
                    "\u041d\u0443\u0436\u0435\u043d \u043b\u0438 \u0440\u0435\u0435\u0441\u0442\u0440 / \u041f\u041f 1875?",
                    "\u0422\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f \u043b\u0438 \u043f\u0438\u0441\u044c\u043c\u043e \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044f?",
                    "\u041a\u0430\u043a\u0438\u0435 \u0441\u0435\u0440\u0438\u0439\u043d\u044b\u0435 \u043d\u043e\u043c\u0435\u0440\u0430?",
                    "\u041a\u0442\u043e \u0431\u0443\u0434\u0435\u0442 \u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0442\u044c \u0440\u0430\u0431\u043e\u0442\u044b?",
                    "\u041a\u0442\u043e \u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442 \u0437\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e \u0438\u043d\u0444\u0440\u0430\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0443?",
                ],
            },
            package={
                "tender": {
                    "title": "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432"
                }
            },
        )

        questions_text = " ".join(normalized["discovery_questions"]).casefold()
        self.assertEqual(len(normalized["discovery_questions"]), 1)
        self.assertIn("\u0441\u0435\u0440\u0432\u0435\u0440\u043d\u0443\u044e", questions_text)
        for banned in (
            "\u0441\u0440\u043e\u043a \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438",
            "\u043f\u043e\u0434\u0430\u0447\u0430 \u0437\u0430\u044f\u0432\u043a\u0438",
            "\u0440\u0435\u0435\u0441\u0442\u0440",
            "\u043f\u043f 1875",
            "\u043f\u0438\u0441\u044c\u043c\u043e \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044f",
            "\u0441\u0435\u0440\u0438\u0439\u043d",
            "\u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0442\u044c \u0440\u0430\u0431\u043e\u0442",
        ):
            self.assertNotIn(banned, questions_text)

    def test_procurement_center_normalization_separates_entry_contact_and_end_customer_roles(self) -> None:
        normalized = tender_report.normalize_lead_report(
            {
                "opening_phrase": "hello",
                "discovery_questions": ["Who owns infrastructure?"],
                "target_roles": ["procurement specialist"],
            },
            package={
                "tender": {
                    "customer_name": (
                        "\u041a\u043e\u043c\u0438\u0442\u0435\u0442 "
                        "\u0433\u043e\u0441\u0443\u0434\u0430\u0440\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0445 "
                        "\u0437\u0430\u043a\u0443\u043f\u043e\u043a "
                        "\u0433\u043e\u0440\u043e\u0434\u0430 \u041c\u043e\u0441\u043a\u0432\u044b"
                    ),
                    "title": "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432",
                }
            },
        )

        target_text = " ".join(normalized["target_end_customer_roles"])
        self.assertIn("\u0432\u0445\u043e\u0434", normalized["procurement_contact_role"])
        self.assertIn("\u0418\u0422-\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d", target_text)
        self.assertIn("\u043a\u043e\u043d\u0435\u0447\u043d\u043e\u0433\u043e \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044f", target_text)
        self.assertIn("\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a", target_text)

    def test_technical_report_persistence_behavior_unchanged(self) -> None:
        connection = CapturingConnection()
        with patch.object(tender_report.psycopg, "connect", return_value=connection, create=True):
            tender_report.save_llm_report(
                tender_id="tender-id",
                provider="routerai",
                model="deepseek/deepseek-v4-pro",
                parsed_report={
                    "recommendation": {"decision": "go", "confidence": "high"},
                    "verdict": "maybe",
                    "confidence": "low",
                },
                raw_response="{}",
                context_chars=10,
                analysis_type=tender_report.LLM_PRESALES_REPORT_ANALYSIS_TYPE,
                metadata={"report_kind": "technical"},
            )

        insert_params = [
            params
            for sql, params in connection.cursor_obj.calls
            if "INSERT INTO analysis_results" in sql
        ][0]
        self.assertEqual(insert_params[-2], "go")
        self.assertEqual(insert_params[-1], "high")

    def test_run_lead_report_uses_lead_prompt_and_analysis_type(self) -> None:
        client = FakeLeadClient()
        package = {
            "tender": {
                "external_id": "EXT-1",
                "customer_name": "Customer",
                "title": "Storage purchase",
            },
            "contact": {},
        }

        with (
            patch.object(tender_report, "REPORT_DIR", Path("llm_reports")),
            patch.object(tender_report, "CONTEXT_DIR", Path("llm_contexts")),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch.object(tender_report, "package_to_markdown", return_value="Tender context"),
            patch.object(tender_report, "create_llm_client", return_value=client),
            patch.object(tender_report, "save_llm_report") as save_report,
        ):
            result = tender_report.run_lead_report(
                external_id="EXT-1",
                tender_id="tender-id",
                package=package,
                max_output_tokens=2048,
                provider="routerai",
                model=None,
                json_mode=True,
                result_label=None,
                save_to_db=True,
            )

        self.assertEqual(result["analysis_type"], "llm_customer_lead_report")
        self.assertEqual(result["report_kind"], "lead")
        self.assertEqual(Path(result["json_path"]).name, "EXT-1.lead.json")
        self.assertEqual(client.calls[0]["system_prompt"], tender_report.LEAD_REPORT_SYSTEM_PROMPT)
        self.assertIn("lead_summary", str(client.calls[0]["user_prompt"]))
        self.assertTrue(client.calls[0]["json_mode"])
        save_report.assert_called_once()
        self.assertEqual(save_report.call_args.kwargs["analysis_type"], "llm_customer_lead_report")

    def test_tender_report_cli_accepts_analysis_depth_standard(self) -> None:
        result = {
            "context_chars": 10,
            "provider": "routerai",
            "model": "deepseek",
            "analysis_type": "llm_presales_report",
            "json_path": "report.json",
            "md_path": "report.md",
            "context_path": "context.md",
        }

        with (
            patch.object(
                sys,
                "argv",
                [
                    "tender_report",
                    "--external-id",
                    "EXT-1",
                    "--analysis-depth",
                    "standard",
                    "--no-save-db",
                ],
            ),
            patch.object(tender_report, "build_package_from_database", return_value=("tender-id", {})) as build_package,
            patch.object(tender_report, "run_presales_report", return_value=result) as run_report,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            tender_report.main()

        build_package.assert_called_once_with(
            external_id="EXT-1",
            max_spec_chars=90000,
            max_other_chars=12000,
            analysis_depth="standard",
            report_kind="technical",
        )
        self.assertEqual(run_report.call_args.kwargs["max_output_tokens"], 4096)
        self.assertEqual(run_report.call_args.kwargs["analysis_depth"], "standard")

    def test_tender_report_cli_defaults_to_deep_limits(self) -> None:
        result = {
            "context_chars": 10,
            "provider": "routerai",
            "model": "deepseek",
            "analysis_type": "llm_presales_report",
            "json_path": "report.json",
            "md_path": "report.md",
            "context_path": "context.md",
        }

        with (
            patch.object(
                sys,
                "argv",
                ["tender_report", "--external-id", "EXT-1", "--no-save-db"],
            ),
            patch.object(tender_report, "build_package_from_database", return_value=("tender-id", {})) as build_package,
            patch.object(tender_report, "run_presales_report", return_value=result) as run_report,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            tender_report.main()

        build_package.assert_called_once_with(
            external_id="EXT-1",
            max_spec_chars=240000,
            max_other_chars=40000,
            analysis_depth="deep",
            report_kind="technical",
        )
        self.assertEqual(run_report.call_args.kwargs["max_output_tokens"], 16384)
        self.assertEqual(run_report.call_args.kwargs["analysis_depth"], "deep")
        self.assertEqual(run_report.call_args.kwargs["report_kind"], "technical")

    def test_tender_report_cli_accepts_report_kind_lead(self) -> None:
        result = {
            "context_chars": 10,
            "provider": "routerai",
            "model": "qwen/qwen3.7-plus",
            "analysis_type": "llm_customer_lead_report",
            "report_kind": "lead",
            "json_path": "report.json",
            "md_path": "report.md",
            "context_path": "context.md",
        }

        with (
            patch.object(
                sys,
                "argv",
                [
                    "tender_report",
                    "--external-id",
                    "EXT-1",
                    "--report-kind",
                    "lead",
                    "--no-save-db",
                ],
            ),
            patch.object(tender_report, "build_package_from_database", return_value=("tender-id", {})) as build_package,
            patch.object(tender_report, "run_presales_report", return_value=result) as run_report,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            tender_report.main()

        build_package.assert_called_once_with(
            external_id="EXT-1",
            max_spec_chars=240000,
            max_other_chars=40000,
            analysis_depth="deep",
            report_kind="lead",
        )
        self.assertEqual(run_report.call_args.kwargs["report_kind"], "lead")
        self.assertEqual(run_report.call_args.kwargs["analysis_depth"], "deep")

    def test_tender_report_cli_max_output_tokens_override_wins(self) -> None:
        result = {
            "context_chars": 10,
            "provider": "routerai",
            "model": "deepseek",
            "analysis_type": "llm_presales_report",
            "json_path": "report.json",
            "md_path": "report.md",
            "context_path": "context.md",
        }

        with (
            patch.object(
                sys,
                "argv",
                [
                    "tender_report",
                    "--external-id",
                    "EXT-1",
                    "--max-output-tokens",
                    "12345",
                    "--no-save-db",
                ],
            ),
            patch.object(tender_report, "build_package_from_database", return_value=("tender-id", {})),
            patch.object(tender_report, "run_presales_report", return_value=result) as run_report,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            tender_report.main()

        self.assertEqual(run_report.call_args.kwargs["max_output_tokens"], 12345)
        self.assertEqual(run_report.call_args.kwargs["analysis_depth"], "deep")

    def test_build_package_blocks_llm_when_primary_technical_doc_is_not_processed(self) -> None:
        tender = {
            "id": "tender-id",
            "external_id": "95748890",
            "title": "Поставка серверного оборудования",
            "_llm_category": "servers",
            "raw": {
                "full": {
                    "documents": [
                        {
                            "id": "445000630",
                            "title": "Приложение 1 Описание Объекта закупки.docx",
                        },
                        {"id": "445000632", "title": "Проект контракта.docx"},
                    ]
                }
            },
        }
        documents = [
            {
                "id": "local-contract",
                "filename": "445000632_Проект контракта.docx",
                "extracted_text": "contract text",
            }
        ]

        with (
            patch.object(tender_report, "get_tender_by_external_id", return_value=tender),
            patch.object(tender_report, "get_documents", return_value=documents),
            patch.object(tender_report, "build_llm_package") as build_package,
            self.assertRaisesRegex(RuntimeError, "primary_technical_document_missing"),
        ):
            tender_report.build_package_from_database(
                external_id="95748890",
                max_spec_chars=1000,
                max_other_chars=1000,
            )

        build_package.assert_not_called()

    def test_build_package_allows_lead_card_context_without_primary_technical_doc(self) -> None:
        tender = {
            "id": "tender-id",
            "external_id": "95748890",
            "title": "Поставка серверного оборудования",
            "raw": {
                "full": {
                    "documents": [
                        {
                            "id": "445000630",
                            "title": "Приложение 1 Описание Объекта закупки.docx",
                        }
                    ]
                }
            },
        }
        documents = []
        package = {"meta": {}, "documents_summary": []}
        readiness = {
            "external_id": "95748890",
            "docs_before": 0,
            "docs_with_text_before": 0,
            "docs_after": 0,
            "docs_with_text_after": 0,
            "targeted_download_triggered": True,
            "preparation_status": "failed",
            "warning_reason": "documents_missing",
            "non_blocking_warning_reason": "lead_documents_missing_allowed_after_prepare",
        }

        with (
            patch.object(tender_report, "get_tender_by_external_id", return_value=tender),
            patch.object(tender_report, "get_documents", return_value=documents),
            patch.object(tender_report, "build_llm_package", return_value=package) as build_package,
            patch.object(
                tender_report,
                "prepare_lead_documents_for_report",
                return_value=(readiness, documents),
            ) as prepare_documents,
        ):
            tender_id, built_package = tender_report.build_package_from_database(
                external_id="95748890",
                max_spec_chars=1000,
                max_other_chars=1000,
                report_kind="lead",
            )

        self.assertEqual(tender_id, "tender-id")
        build_package.assert_called_once()
        prepare_documents.assert_called_once()
        self.assertEqual(built_package["meta"]["report_kind"], "lead")
        self.assertEqual(built_package["meta"]["lead_context_mode"], "card_only")
        self.assertTrue(built_package["meta"]["lead_documents_missing_allowed"])
        self.assertTrue(
            built_package["meta"]["document_preparation"]["targeted_download_triggered"]
        )
        self.assertTrue(built_package["meta"]["primary_technical_document_required"])
        self.assertFalse(built_package["meta"]["primary_technical_document_ready"])
        self.assertEqual(
            built_package["available_document_titles"][0]["title"],
            "Приложение 1 Описание Объекта закупки.docx",
        )

    def test_direct_lead_preparation_uses_targeted_path_before_card_only(self) -> None:
        prepare_calls = []
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")

        def prepare(**kwargs):
            prepare_calls.append(kwargs)
            return SimpleNamespace(
                status=SimpleNamespace(value="ready_for_llm"),
                summary="prepared",
                errors=[],
                document_download_was_run=True,
                document_readiness="documents_ready_with_text",
                steps={"text_extraction": SimpleNamespace(attempted=True)},
            )

        prepare_module.prepare_tender_for_analysis = prepare
        prepared_documents = [{"id": "tz", "filename": "tz.docx", "extracted_text": "TZ text"}]

        with (
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            patch.object(tender_report, "get_documents", return_value=prepared_documents),
        ):
            readiness, documents = tender_report.prepare_lead_documents_for_report(
                external_id="95748890",
                tender={"id": "tender-id", "external_id": "95748890"},
                documents=[],
                primary_status={"required": False, "ready": True, "reason": None},
            )

        self.assertEqual(documents, prepared_documents)
        self.assertEqual(len(prepare_calls), 1)
        self.assertEqual(prepare_calls[0]["external_id"], "95748890")
        self.assertEqual(prepare_calls[0]["limit_docs"], 5)
        self.assertTrue(readiness["targeted_download_triggered"])
        self.assertTrue(readiness["extraction_retry_triggered"])
        self.assertEqual(readiness["docs_before"], 0)
        self.assertEqual(readiness["docs_with_text_after"], 1)
        self.assertIsNone(readiness["warning_reason"])

    def test_domestic_vendor_guardrail_replaces_foreign_offer(self) -> None:
        report = {
            "potential_offer": {
                "what_to_offer": [
                    "HPE ProLiant DL360",
                    "Dell PowerEdge R760",
                ],
                "possible_analogs_or_approach": ["Lenovo ThinkSystem как аналог"],
                "what_presales_should_check": [],
            },
            "vendor_fit": {
                "candidate_vendors_or_lines": [
                    {
                        "vendor_or_line": "HPE ProLiant",
                        "why_may_fit": "сервер",
                        "what_to_verify": [],
                        "risk": "medium",
                    }
                ],
                "notes": [],
            },
            "commercial_assessment": {"risks": []},
            "risk_map": {"legal_or_registry": {"level": "unknown", "evidence": []}},
        }
        package = {
            "domestic_registry_context": {
                "detected": True,
                "matched_signals": ["российская платформа"],
            }
        }

        applied = tender_report.apply_domestic_vendor_guardrail(report, package)

        self.assertTrue(applied)
        offer_text = json.dumps(report["potential_offer"], ensure_ascii=False).lower()
        self.assertNotIn("hpe", offer_text)
        self.assertNotIn("dell", offer_text)
        self.assertIn("российское или реестровое оборудование", offer_text)
        self.assertIn("реестровой записи", offer_text)
        self.assertIn(
            "Проверить нацрежим",
            "\n".join(report["potential_offer"]["what_presales_should_check"]),
        )

    def test_problems_guardrail_removes_plain_hardware_requirements(self) -> None:
        cleaned = tender_report.clean_problems_in_tz(
            ["CPU: ≥24 cores, RAM: DDR5, RAID 50/60"],
            ["CPU: ≥24 cores", "RAM: DDR5", "RAID 50/60"],
        )

        self.assertEqual(cleaned, [tender_report.DEFAULT_TZ_PROBLEMS_ITEM])
        self.assertNotIn("CPU", "\n".join(cleaned))

    def test_problems_guardrail_keeps_real_2u_sff_lff_contradiction(self) -> None:
        problem = "Требуется одновременно 12 SFF и 12 LFF в корпусе 2U."

        cleaned = tender_report.clean_problems_in_tz(
            [problem],
            ["12 SFF", "12 LFF", "корпус 2U"],
        )

        self.assertEqual(cleaned, [problem])

    def test_problems_guardrail_keeps_hot_swap_m2_issue(self) -> None:
        problem = "Требуется hot-swap M.2, что надо проверить как сомнительное требование."

        cleaned = tender_report.clean_problems_in_tz([problem], ["M.2 hot-swap"])

        self.assertEqual(cleaned, [problem])

    def test_problems_guardrail_keeps_missing_delivery_term(self) -> None:
        problem = "Срок поставки в ТЗ не указан, нужна проверка у заказчика."

        cleaned = tender_report.clean_problems_in_tz([problem], [])

        self.assertEqual(cleaned, [problem])

    def test_problems_guardrail_keeps_authorization_no_equivalent_and_local_engineer(self) -> None:
        problems = [
            "Требуется авторизационное письмо производителя.",
            "Эквивалент не допускается.",
            "Требуется локальный инженер в регионе заказчика.",
        ]

        cleaned = tender_report.clean_problems_in_tz(problems, [])

        self.assertEqual(cleaned, problems)

    def test_routerai_markdown_uses_precise_tz_problems_heading(self) -> None:
        markdown = tender_report.routerai_two_pass_report_to_markdown(
            {
                "summary": "summary",
                "problems_in_tz": ["Срок поставки не указан."],
            },
            {"tender": {}, "contact": {}},
        )

        self.assertIn("## Проблемы и неясности ТЗ", markdown)
        self.assertNotIn("## Problems in TZ", markdown)

    def test_packaging_pass_retries_once_without_repeating_analysis(self) -> None:
        client = FakeRouterAIClient()

        package = {
            "tender": {
                "customer_name": "Customer",
                "initial_price": "1000000",
                "deadline_at": "2026-05-10",
            },
            "contact": {},
        }

        with (
            patch.object(tender_report, "REPORT_DIR", Path("llm_reports")),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch.object(tender_report, "save_llm_error_metadata") as save_error,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = tender_report.run_routerai_two_pass_report(
                external_id="95561178",
                tender_id="tender-id",
                package=package,
                context_markdown="Tender context",
                context_path=Path("context.md"),
                client=client,
                max_output_tokens=1024,
                effective_json_mode=True,
                result_label=None,
                save_to_db=False,
            )

            self.assertEqual(client.analysis_calls, 1)
            self.assertEqual(client.packaging_calls, 2)
            self.assertEqual(result["report"]["summary"], "final summary")
            self.assertEqual(result["llm_metadata"]["packaging_retry_count"], 1)
            self.assertIn("packaging_first_parse_error", result["llm_metadata"])
            save_error.assert_not_called()

    def test_analysis_pass_retries_once_with_deep_fallback_context(self) -> None:
        client = FakeRouterAIAnalysisRetryClient()
        seen_caps: list[tuple[int, int]] = []

        package = {
            "tender": {
                "customer_name": "Customer",
                "initial_price": "1000000",
                "deadline_at": "2026-05-10",
            },
            "contact": {},
            "technical_spec_documents": [{"text": "s" * 200000}],
            "price_documents": [{"text": "p" * 30000}],
            "other_documents_short": [{"text_preview": "o" * 25000}],
        }

        def fake_package_to_markdown(value: dict) -> str:
            seen_caps.append(
                (
                    len(value["technical_spec_documents"][0]["text"]),
                    len(value["price_documents"][0]["text"]),
                )
            )
            return "retry context"

        with (
            patch.object(tender_report, "REPORT_DIR", Path("llm_reports")),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch.object(tender_report, "save_llm_error_metadata") as save_error,
            patch.object(tender_report, "package_to_markdown", side_effect=fake_package_to_markdown),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = tender_report.run_routerai_two_pass_report(
                external_id="95543713",
                tender_id="tender-id",
                package=package,
                context_markdown="initial large context",
                context_path=Path("context.md"),
                client=client,
                max_output_tokens=16384,
                effective_json_mode=True,
                result_label=None,
                save_to_db=False,
                analysis_depth="deep",
            )

            self.assertEqual(client.analysis_calls, 2)
            self.assertEqual(client.analysis_max_tokens, [16384, 16384])
            self.assertEqual(client.packaging_calls, 1)
            self.assertEqual(seen_caps, [(120000, 20000)])
            self.assertEqual(result["report"]["summary"], "final summary")
            self.assertTrue(result["llm_metadata"]["analysis_retry_used"])
            self.assertEqual(result["llm_metadata"]["analysis_retry_max_spec_chars"], 120000)
            self.assertEqual(result["llm_metadata"]["analysis_retry_max_other_chars"], 20000)
            self.assertEqual(result["llm_metadata"]["analysis_retry_max_output_tokens"], 16384)
            self.assertEqual(result["llm_metadata"]["analysis_retry_context_strategy"], "reduced_context")
            self.assertEqual(result["llm_metadata"]["analysis_initial_finish_reason"], "length")
            self.assertEqual(result["llm_metadata"]["analysis_retry_finish_reason"], "stop")
            save_error.assert_not_called()

    def test_analysis_pass_retries_same_deep_context_with_larger_output_budget(self) -> None:
        client = FakeRouterAIAnalysisRetryClient()
        package = {
            "meta": {
                "context_limits": {
                    "max_spec_chars": 240000,
                    "max_other_chars": 40000,
                }
            },
            "tender": {
                "customer_name": "Customer",
                "initial_price": "1000000",
                "deadline_at": "2026-05-10",
            },
            "contact": {},
            "technical_spec_documents": [{"text": "s" * 200000}],
            "price_documents": [{"text": "p" * 30000}],
            "other_documents_short": [{"text_preview": "o" * 25000}],
        }

        with (
            patch.object(tender_report, "REPORT_DIR", Path("llm_reports")),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch.object(tender_report, "package_to_markdown") as package_to_markdown,
            patch.object(tender_report, "save_llm_error_metadata") as save_error,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = tender_report.run_routerai_two_pass_report(
                external_id="95543713",
                tender_id="tender-id",
                package=package,
                context_markdown="initial large context",
                context_path=Path("context.md"),
                client=client,
                max_output_tokens=8192,
                effective_json_mode=True,
                result_label=None,
                save_to_db=False,
                analysis_depth="deep",
            )

        self.assertEqual(client.analysis_calls, 2)
        self.assertEqual(client.analysis_max_tokens, [8192, 16384])
        package_to_markdown.assert_not_called()
        self.assertEqual(result["report"]["summary"], "final summary")
        self.assertTrue(result["llm_metadata"]["analysis_retry_used"])
        self.assertEqual(result["llm_metadata"]["analysis_retry_max_spec_chars"], 240000)
        self.assertEqual(result["llm_metadata"]["analysis_retry_max_other_chars"], 40000)
        self.assertEqual(result["llm_metadata"]["analysis_retry_max_output_tokens"], 16384)
        self.assertEqual(
            result["llm_metadata"]["analysis_retry_context_strategy"],
            "same_context_increased_output_tokens",
        )
        save_error.assert_not_called()

    def test_analysis_retry_failure_saves_error_metadata(self) -> None:
        client = FakeRouterAIAnalysisRetryFailsClient()
        package = {
            "tender": {"customer_name": "Customer"},
            "contact": {},
            "technical_spec_documents": [{"text": "s" * 70000}],
            "price_documents": [],
            "other_documents_short": [],
        }

        with (
            patch.object(tender_report, "REPORT_DIR", Path("llm_reports")),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch.object(tender_report, "package_to_markdown", return_value="retry context"),
            patch.object(tender_report, "save_llm_error_metadata", return_value=Path("error.json")) as save_error,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(RuntimeError),
        ):
            tender_report.run_routerai_two_pass_report(
                external_id="95543713",
                tender_id="tender-id",
                package=package,
                context_markdown="initial large context",
                context_path=Path("context.md"),
                client=client,
                max_output_tokens=1024,
                effective_json_mode=True,
                result_label=None,
                save_to_db=False,
            )

        self.assertEqual(client.analysis_calls, 2)
        save_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
