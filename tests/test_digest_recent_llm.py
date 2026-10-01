from __future__ import annotations

import contextlib
import importlib
import io
import json
import runpy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


FORBIDDEN_ROLE_WORD = "".join(
    chr(code) for code in (0x0437, 0x0432, 0x043E, 0x043D, 0x0430, 0x0440, 0x044C)
)


class FakeCursor:
    def __init__(self, rows: list[dict] | None = None, on_execute=None) -> None:
        self.rows = rows or []
        self.query = ""
        self.params = None
        self.on_execute = on_execute

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, query: str, params: tuple) -> None:
        self.query = query
        self.params = params
        if self.on_execute:
            self.on_execute(query, params)

    def fetchall(self) -> list[dict]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_obj = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return self.cursor_obj


def row(
    tender_id: str,
    report_created_at: datetime,
    llm_recommendation: str | None = None,
    *,
    deadline_at: datetime | None = None,
    latest_sales_status: str | None = None,
) -> dict:
    return {
        "tender_id": tender_id,
        "external_id": tender_id,
        "title": tender_id,
        "customer_name": None,
        "initial_price": None,
        "region": None,
        "law": None,
        "procedure_type": None,
        "published_at": None,
        "deadline_at": deadline_at,
        "url": None,
        "raw": {"full": {}},
        "score": 50,
        "recommendation": "maybe",
        "confidence": "high",
        "result": {},
        "document_risk_result": None,
        "llm_report_result": {"report": {"verdict": llm_recommendation}} if llm_recommendation else {},
        "llm_report_recommendation": llm_recommendation,
        "llm_report_created_at": report_created_at,
        "latest_sales_status": latest_sales_status,
    }


def today_row(
    tender_id: str,
    *,
    recommendation: str = "maybe",
    deadline_at: datetime | None = None,
    llm_recommendation: str | None = None,
    preparation_blocked_event: str | None = None,
    preparation_requeue_event: str | None = None,
    docs_count: int = 1,
    docs_with_text: int = 1,
    latest_sales_status: str | None = None,
    latest_sales_status_created_at: datetime | None = None,
) -> dict:
    return {
        "tender_id": tender_id,
        "id": tender_id,
        "external_id": tender_id,
        "title": tender_id,
        "customer_name": None,
        "initial_price": None,
        "region": None,
        "law": None,
        "procedure_type": None,
        "published_at": None,
        "created_at": datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        "deadline_at": deadline_at,
        "url": None,
        "raw": {"full": {}},
        "score": 50,
        "recommendation": recommendation,
        "confidence": "high",
        "result": {"why_relevant": ["profile"]},
        "rule_based_result": {"why_relevant": ["profile"]},
        "document_risk_result": None,
        "llm_report_result": (
            {"report": {"recommendation": {"decision": llm_recommendation}}}
            if llm_recommendation
            else {}
        ),
        "llm_report_recommendation": llm_recommendation,
        "llm_report_created_at": None,
        "docs_count": docs_count,
        "docs_with_text": docs_with_text,
        "latest_sales_status": latest_sales_status,
        "latest_sales_status_created_at": latest_sales_status_created_at,
        "preparation_blocked_event": preparation_blocked_event,
        "preparation_requeue_event": preparation_requeue_event,
    }


def lead_report_row(
    tender_id: str,
    *,
    priority: str = "high",
    report_recommendation: str = "go",
    latest_sales_status: str | None = None,
    created_at: datetime | None = None,
    triage_decision: str | None = None,
    triage_created_at: datetime | None = None,
) -> dict:
    created = created_at or datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    row = {
        "tender_id": tender_id,
        "id": tender_id,
        "external_id": tender_id,
        "title": tender_id,
        "customer_name": "ООО Заказчик",
        "initial_price": 1_000_000,
        "deadline_at": created + timedelta(days=7),
        "url": f"https://example.test/{tender_id}",
        "raw": {"full": {}},
        "recommendation": "go",
        "llm_report_analysis_type": "llm_customer_lead_report",
        "llm_report_recommendation": report_recommendation,
        "llm_report_created_at": created,
        "llm_report_result": {
            "report": {
                "lead_summary": "Клиентский инфраструктурный сигнал.",
                "customer_signal": "Заказчик обновляет инфраструктуру.",
                "possible_needs": ["серверы", "резервное копирование"],
                "target_end_customer_roles": ["ИТ-директор"],
                "opening_phrase": "Добрый день. Хотели обсудить инфраструктурную задачу.",
                "discovery_questions": ["Кто отвечает за инфраструктуру?"],
                "next_action": "Взять лид в первичную обработку.",
                "lead_priority": priority,
                "confidence": "high",
            }
        },
        "latest_sales_status": latest_sales_status,
        "latest_sales_status_created_at": created,
    }
    if triage_decision is not None:
        row.update(
            {
                "lead_triage_analysis_type": "llm_customer_lead_triage",
                "lead_triage_recommendation": {
                    "go": "go",
                    "maybe": "maybe",
                    "reject": "no_go",
                }.get(triage_decision, "no_go"),
                "lead_triage_decision": triage_decision,
                "lead_triage_created_at": triage_created_at or created - timedelta(minutes=5),
                "lead_triage_result": {"report": {"lead_decision": triage_decision}},
            }
        )
    return row


def lead_triage_row(
    tender_id: str,
    decision: str,
    *,
    reason: str = "недостаточно подтвержден инфраструктурный владелец",
) -> dict:
    return {
        "tender_id": tender_id,
        "id": tender_id,
        "external_id": tender_id,
        "title": tender_id,
        "customer_name": "ООО Заказчик",
        "initial_price": 1_000_000,
        "url": f"https://example.test/{tender_id}",
        "raw": {"full": {}},
        "lead_triage_recommendation": "maybe" if decision == "maybe" else "no_go",
        "lead_triage_result": {
            "report": {
                "lead_decision": decision,
                "lead_summary": "Сигнал требует проверки.",
                "likely_customer_story": "Возможна инфраструктурная задача.",
                "reject_reason": reason,
            }
        },
    }


def assert_no_unsafe_pyformat_percent(query: str) -> None:
    index = 0
    while index < len(query):
        if query[index] != "%":
            index += 1
            continue

        next_char = query[index + 1] if index + 1 < len(query) else ""
        if next_char in {"%", "s"}:
            index += 2
            continue

        raise AssertionError(
            f"unsafe raw percent in SQL near: {query[max(0, index - 30):index + 30]!r}"
        )


class DigestRecentLLMTest(unittest.TestCase):
    def setUp(self) -> None:
        psycopg = types.ModuleType("psycopg")
        psycopg.connect = lambda *args, **kwargs: None

        psycopg_rows = types.ModuleType("psycopg.rows")
        psycopg_rows.dict_row = object()

        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(
            database_url="postgresql://user:password@localhost:5432/db",
            llm_json_mode=True,
        )

        business_rules = types.ModuleType("app.business_rules")
        business_rules.business_assessment = lambda tender: {"action": "quick_check"}
        business_rules.business_assessment_block = lambda tender: (
            "Коммерческий интерес: средний\n"
            "Доступность сделки: гибридный инфраструктурный проект с оборудованием и работами\n"
            "Эффект заточки: нейтрально\n"
            "Бизнес-действие: быстрый ручной чек перед пресейлом"
        )
        business_rules.digest_sort_key = lambda tender: (0, 0, 0, str(tender.get("title") or ""))
        business_rules.effective_recommendation = lambda tender: tender.get("recommendation") or "go"
        business_rules.lead_hard_noise_reason_for_row = (
            lambda tender, assessment=None: "generic_antivirus_license"
            if "kaspersky" in str(tender.get("title") or "").lower()
            else None
        )
        business_rules.selection_warnings = lambda tender: []

        self.previous_digest = sys.modules.pop("app.digest", None)
        self.module_patcher = patch.dict(
            sys.modules,
            {
                "psycopg": psycopg,
                "psycopg.rows": psycopg_rows,
                "app.config": config,
                "app.business_rules": business_rules,
            },
        )
        self.module_patcher.start()

        def cleanup() -> None:
            sys.modules.pop("app.digest", None)
            self.module_patcher.stop()
            if self.previous_digest is not None:
                sys.modules["app.digest"] = self.previous_digest

        self.addCleanup(cleanup)
        self.digest = importlib.import_module("app.digest")

    def outreach_block(self, text: str) -> str:
        marker = "📞 Для знакомства"
        self.assertIn(marker, text)
        return marker + text.split(marker, 1)[1]

    def test_main_without_recent_flag_uses_regular_digest(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["python -m app.digest"]),
            patch.object(self.digest, "build_digest_messages", return_value=["ordinary digest"]) as regular_digest,
            patch.object(self.digest, "build_recent_llm_digest_messages") as recent_digest,
            contextlib.redirect_stdout(stdout),
        ):
            self.digest.main()

        regular_digest.assert_called_once_with(
            deadline_min_days=3.0,
            include_deadline_risk=False,
        )
        recent_digest.assert_not_called()
        self.assertIn("ordinary digest", stdout.getvalue())

    def test_module_cli_backlog_path_handles_manual_document_rows(self) -> None:
        cursor = FakeCursor(
            [
                today_row(
                    "RST32615987873",
                    deadline_at=datetime(2030, 5, 5, 12, 0, tzinfo=timezone.utc),
                    preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                    docs_count=0,
                    docs_with_text=0,
                ),
                today_row(
                    "service-noise",
                    deadline_at=datetime(2030, 5, 5, 12, 0, tzinfo=timezone.utc),
                    docs_count=0,
                    docs_with_text=0,
                ),
                today_row(
                    "hidden-no-go",
                    recommendation="no_go",
                    deadline_at=datetime(2030, 5, 5, 12, 0, tzinfo=timezone.utc),
                    docs_count=0,
                    docs_with_text=0,
                ),
            ]
        )
        cursor.rows[0]["external_id"] = "RST32615987873"
        cursor.rows[0]["title"] = "Поставка серверного оборудования виртуализации для проектов"
        cursor.rows[0]["initial_price"] = 875_174_000
        cursor.rows[0]["url"] = "https://www.roseltorg.ru/procedure/32615987873"
        cursor.rows[1]["title"] = "Антивирусное продление"

        business_rules = sys.modules["app.business_rules"]

        def fake_assessment(tender: dict) -> dict:
            if tender.get("tender_id") == "service-noise":
                return {
                    "action": "no_go",
                    "market_access": "service_noise",
                    "commercial_interest": "low",
                    "tailoring_effect": "negative",
                }
            return {
                "action": "proceed",
                "market_access": "target_hardware",
                "commercial_interest": "high",
                "tailoring_effect": "neutral",
            }

        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["python -m app.digest"]),
            patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)),
            patch.object(business_rules, "business_assessment", side_effect=fake_assessment),
            contextlib.redirect_stdout(stdout),
        ):
            runpy.run_module("app.digest", run_name="__main__", alter_sys=True)

        output = stdout.getvalue()
        self.assertIn("AI Tender Radar - утренний shortlist", output)
        self.assertIn("📄 Нужны документы для LLM-разбора", output)
        self.assertIn("документы закрыты на площадке, нужно скачать КД/ТЗ вручную", output)
        self.assertIn("https://www.roseltorg.ru/procedure/32615987873", output)
        self.assertNotIn("service-noise", output)
        self.assertNotIn("Антивирусное продление", output)
        self.assertNotIn("hidden-no-go", output)

    def test_main_with_recent_flag_uses_recent_digest(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["python -m app.digest", "--recent-llm-hours", "6"]),
            patch.object(self.digest, "build_digest_messages") as regular_digest,
            patch.object(self.digest, "build_recent_llm_digest_messages", return_value=["recent digest"]) as recent_digest,
            contextlib.redirect_stdout(stdout),
        ):
            self.digest.main()

        regular_digest.assert_not_called()
        recent_digest.assert_called_once_with(
            6.0,
            include_no_go=False,
            include_labeled=False,
            deadline_min_days=3.0,
            include_deadline_risk=False,
        )
        self.assertIn("recent digest", stdout.getvalue())

    def test_main_with_include_no_go_passes_flag_to_recent_digest(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                ["python -m app.digest", "--recent-llm-hours", "6", "--debug-include-no-go"],
            ),
            patch.object(self.digest, "build_recent_llm_digest_messages", return_value=["recent digest"]) as recent_digest,
            contextlib.redirect_stdout(stdout),
        ):
            self.digest.main()

        recent_digest.assert_called_once_with(
            6.0,
            include_no_go=True,
            include_labeled=False,
            deadline_min_days=3.0,
            include_deadline_risk=False,
        )
        self.assertIn("recent digest", stdout.getvalue())

    def test_legacy_include_no_go_still_enters_debug_recent_digest(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                ["python -m app.digest", "--recent-llm-hours", "6", "--include-no-go"],
            ),
            patch.object(self.digest, "build_recent_llm_digest_messages", return_value=["recent digest"]) as recent_digest,
            contextlib.redirect_stdout(stdout),
        ):
            self.digest.main()

        recent_digest.assert_called_once_with(
            6.0,
            include_no_go=True,
            include_labeled=False,
            deadline_min_days=3.0,
            include_deadline_risk=False,
        )
        self.assertIn("recent digest", stdout.getvalue())

    def test_main_with_include_labeled_passes_debug_flag_to_recent_digest(self) -> None:
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                ["python -m app.digest", "--recent-llm-hours", "6", "--include-labeled"],
            ),
            patch.object(self.digest, "build_recent_llm_digest_messages", return_value=["recent digest"]) as recent_digest,
            contextlib.redirect_stdout(stdout),
        ):
            self.digest.main()

        recent_digest.assert_called_once_with(
            6.0,
            include_no_go=False,
            include_labeled=True,
            deadline_min_days=3.0,
            include_deadline_risk=False,
        )
        self.assertIn("recent digest", stdout.getvalue())

    def test_regular_digest_hides_no_go_rows(self) -> None:
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {"tender_id": "hidden", "external_id": "hidden", "title": "hidden", "recommendation": "no_go"},
        ]

        with (
            patch.object(self.digest, "get_digest_rows", return_value=rows),
            patch.object(self.digest, "format_card", side_effect=lambda tender, index=None: tender["title"]),
        ):
            cards = self.digest.build_digest_cards(limit=5)

        self.assertEqual([card.tender_id for card in cards], ["visible"])

    def test_regular_digest_hides_current_hard_noise_candidate(self) -> None:
        noise = today_row("hard-noise")
        noise["title"] = "Kaspersky antivirus license renewal"
        visible = today_row("visible")

        with patch.object(self.digest, "get_digest_rows", return_value=[noise, visible]):
            selected, stats = self.digest.select_digest_rows_with_stats(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(stats.hidden_by_lead_hard_noise, 1)

    def test_regular_digest_hides_latest_lead_triage_reject_without_full_report(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {
                "tender_id": "triage-reject",
                "external_id": "triage-reject",
                "title": "triage-reject",
                "recommendation": "go",
                "lead_triage_analysis_type": self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                "lead_triage_decision": "reject",
                "lead_triage_created_at": now,
                "lead_triage_result": {"report": {"lead_decision": "reject"}},
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_hides_latest_lead_triage_reject_with_only_older_positive_feedback(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "triage-reject",
                "external_id": "triage-reject",
                "title": "triage-reject",
                "recommendation": "go",
                "lead_triage_analysis_type": self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                "lead_triage_decision": "reject",
                "lead_triage_created_at": now,
                "lead_triage_result": {"report": {"lead_decision": "reject"}},
                "latest_sales_status": "interesting",
                "latest_sales_status_created_at": now - timedelta(minutes=1),
            }
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, stats = self.digest.select_digest_rows_with_stats(limit=None, now=now)

        self.assertEqual(selected, [])
        self.assertEqual(stats.hidden_by_lead_triage_reject, 1)

    def test_regular_digest_keeps_latest_lead_triage_reject_with_newer_positive_feedback(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "triage-reject",
                "external_id": "triage-reject",
                "title": "triage-reject",
                "recommendation": "go",
                "lead_triage_analysis_type": self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                "lead_triage_decision": "reject",
                "lead_triage_created_at": now,
                "lead_triage_result": {"report": {"lead_decision": "reject"}},
                "latest_sales_status": "interesting",
                "latest_sales_status_created_at": now + timedelta(minutes=1),
            }
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, stats = self.digest.select_digest_rows_with_stats(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["triage-reject"])
        self.assertEqual(stats.hidden_by_lead_triage_reject, 0)

    def test_regular_digest_keeps_maybe_lead_triage_visible(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "triage-maybe",
                "external_id": "triage-maybe",
                "title": "triage-maybe",
                "recommendation": "go",
                "lead_triage_analysis_type": self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                "lead_triage_decision": "maybe",
                "lead_triage_created_at": now,
                "lead_triage_result": {"report": {"lead_decision": "maybe"}},
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["triage-maybe"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_dedupes_external_id_and_prefers_operational_lead_card(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        manual = today_row(
            "manual-row",
            preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
            docs_count=0,
            docs_with_text=0,
        )
        lead = lead_report_row("lead-row", report_recommendation="go", created_at=now)
        manual["external_id"] = "same-external"
        lead["external_id"] = "same-external"

        with patch.object(self.digest, "get_digest_rows", return_value=[manual, lead]):
            selected, _ = self.digest.select_digest_rows(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["lead-row"])
        self.assertFalse(selected[0].get(self.digest.MANUAL_DOCUMENT_REASON_KEY))

    def test_regular_digest_hides_latest_sales_in_work(self) -> None:
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {
                "tender_id": "in-work",
                "external_id": "in-work",
                "title": "in-work",
                "recommendation": "go",
                "latest_sales_status": "sales_in_work",
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_hides_latest_sales_not_relevant(self) -> None:
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {
                "tender_id": "not-relevant",
                "external_id": "not-relevant",
                "title": "not-relevant",
                "recommendation": "go",
                "latest_sales_status": "sales_not_relevant",
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_hides_negative_sales_feedback_statuses(self) -> None:
        rows = [{"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"}]
        negative_statuses = tuple(sorted(self.digest.NEGATIVE_SALES_FEEDBACK_STATUSES))
        rows.extend(
            {
                "tender_id": status,
                "external_id": status,
                "title": status,
                "recommendation": "go",
                "latest_sales_status": status,
            }
            for status in negative_statuses
        )

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, stats = self.digest.select_digest_rows_with_stats(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(stats.hidden_by_feedback, len(negative_statuses))

    def test_regular_digest_hides_existing_client_customer_marker(self) -> None:
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {
                "tender_id": "same-customer-new",
                "external_id": "same-customer-new",
                "title": "same-customer-new",
                "recommendation": "go",
                "existing_client_customer_status": "sales_existing_client",
                "existing_client_source_tender_id": "source-tender",
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, stats = self.digest.select_digest_rows_with_stats(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(stats.hidden_by_feedback, 1)

    def test_regular_digest_shows_latest_sales_returned(self) -> None:
        rows = [
            {
                "tender_id": "returned",
                "external_id": "returned",
                "title": "returned",
                "recommendation": "go",
                "latest_sales_status": "sales_returned",
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["returned"])
        self.assertEqual(hidden_deadline, 0)

    def test_filter_feedback_statuses_do_not_hide_regular_digest_rows(self) -> None:
        rows = [
            {
                "tender_id": "filter-feedback",
                "external_id": "filter-feedback",
                "title": "filter-feedback",
                "recommendation": "go",
                "latest_sales_status": "filter_hidden_correct",
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["filter-feedback"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_hides_unknown_generic_ict_without_target_category(self) -> None:
        rows = [
            {
                "tender_id": "generic-ict",
                "external_id": "95722308",
                "title": "Монтаж и настройка оборудования ИКТ",
                "recommendation": "go",
            },
        ]
        business_profile = types.ModuleType("app.business_profile")
        business_profile.load_business_profile = lambda path=None: {}
        business_profile.match_target_category = lambda tender, profile: (None, None)

        with (
            patch.object(self.digest, "get_digest_rows", return_value=rows),
            patch.object(
                self.digest,
                "business_assessment",
                return_value={
                    "action": "quick_check",
                    "market_access": "unknown",
                    "commercial_interest": "medium",
                    "tailoring_effect": "neutral",
                },
            ),
            patch.dict(sys.modules, {"app.business_profile": business_profile}),
        ):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual(selected, [])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_full_backlog_can_return_more_than_15_cards(self) -> None:
        rows = [
            {
                "tender_id": f"visible-{index:02d}",
                "external_id": f"visible-{index:02d}",
                "title": f"visible-{index:02d}",
                "recommendation": "go",
                "deadline_at": None,
            }
            for index in range(20)
        ]

        with (
            patch.object(self.digest, "get_digest_rows", return_value=rows) as getter,
            patch.object(self.digest, "format_card", side_effect=lambda tender, index=None: tender["title"]),
        ):
            cards, hidden_deadline = self.digest.build_digest_cards_with_stats(limit=None)

        getter.assert_called_once_with(limit=None)
        self.assertEqual(len(cards), 20)
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_full_backlog_still_hides_no_go_rows(self) -> None:
        rows = [
            {"tender_id": "visible", "external_id": "visible", "title": "visible", "recommendation": "go"},
            {"tender_id": "hidden", "external_id": "hidden", "title": "hidden", "recommendation": "no_go"},
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, hidden_deadline = self.digest.select_digest_rows(limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_deadline, 0)

    def test_regular_digest_full_backlog_keeps_deadline_risk_rows(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "too-soon",
                "external_id": "too-soon",
                "title": "too-soon",
                "recommendation": "go",
                "deadline_at": now + timedelta(days=2),
            },
            {
                "tender_id": "fresh",
                "external_id": "fresh",
                "title": "fresh",
                "recommendation": "go",
                "deadline_at": now + timedelta(days=4),
            },
            {
                "tender_id": "no-deadline",
                "external_id": "no-deadline",
                "title": "no-deadline",
                "recommendation": "go",
                "deadline_at": None,
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, deadline_warning_count = self.digest.select_digest_rows(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["fresh", "no-deadline", "too-soon"])
        self.assertEqual(deadline_warning_count, 1)

    def test_full_backlog_includes_target_marketplace_auth_as_manual_document_item(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "RST32615987873",
                deadline_at=now + timedelta(days=7),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            )
        ]
        rows[0]["external_id"] = "RST32615987873"
        rows[0]["title"] = "Поставка серверного оборудования виртуализации для проектов"
        rows[0]["initial_price"] = 875_174_000
        rows[0]["url"] = "https://www.roseltorg.ru/procedure/32615987873"

        with (
            patch.object(self.digest, "get_digest_rows", return_value=rows),
            patch.object(
                self.digest,
                "business_assessment",
                return_value={
                    "action": "proceed",
                    "market_access": "target_hardware",
                    "commercial_interest": "high",
                    "tailoring_effect": "neutral",
                },
            ),
        ):
            cards, hidden_deadline = self.digest.build_digest_cards_with_stats(limit=None, now=now)

        self.assertEqual(hidden_deadline, 0)
        self.assertEqual([card.tender_id for card in cards], ["RST32615987873"])
        self.assertIn("📄 Нужны документы для LLM-разбора", cards[0].message_text)
        self.assertIn(
            "Причина: документы закрыты на площадке, нужно скачать КД/ТЗ вручную.",
            cards[0].message_text,
        )
        self.assertIn("Почему важно: Закупка выглядит как сигнал активной задачи: серверная инфраструктура.", cards[0].message_text)
        self.assertIn("📞 Для знакомства", cards[0].message_text)
        self.assertNotIn("Действие:", cards[0].message_text)
        self.assertNotIn("Статус обработки:", cards[0].message_text)
        self.assertIn("Ссылка: https://www.roseltorg.ru/procedure/32615987873", cards[0].message_text)

    def test_full_backlog_hides_no_go_but_keeps_deadline_risk_manual_document_items(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "no-go-marketplace-auth",
                recommendation="no_go",
                deadline_at=now + timedelta(days=7),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "deadline-risk-marketplace-auth",
                deadline_at=now + timedelta(days=2),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
        ]

        with (
            patch.object(self.digest, "get_digest_rows", return_value=rows),
            patch.object(
                self.digest,
                "business_assessment",
                return_value={
                    "action": "proceed",
                    "market_access": "target_hardware",
                    "commercial_interest": "high",
                    "tailoring_effect": "neutral",
                },
            ),
        ):
            selected, deadline_warning_count = self.digest.select_digest_rows(limit=None, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["deadline-risk-marketplace-auth"])
        self.assertEqual(deadline_warning_count, 1)

    def test_regular_digest_keeps_deadline_risk_rows(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "expired",
                "external_id": "expired",
                "title": "expired",
                "recommendation": "go",
                "deadline_at": now - timedelta(hours=1),
            },
            {
                "tender_id": "too-soon",
                "external_id": "too-soon",
                "title": "too-soon",
                "recommendation": "go",
                "deadline_at": now + timedelta(days=2, hours=23),
            },
            {
                "tender_id": "maybe-too-soon",
                "external_id": "maybe-too-soon",
                "title": "maybe-too-soon",
                "recommendation": "maybe",
                "deadline_at": now + timedelta(days=2),
            },
            {
                "tender_id": "fresh",
                "external_id": "fresh",
                "title": "fresh",
                "recommendation": "go",
                "deadline_at": now + timedelta(days=3),
            },
            {
                "tender_id": "no-deadline",
                "external_id": "no-deadline",
                "title": "no-deadline",
                "recommendation": "go",
                "deadline_at": None,
            },
        ]

        with patch.object(self.digest, "get_digest_rows", return_value=rows):
            selected, deadline_warning_count = self.digest.select_digest_rows(limit=10, now=now)

        self.assertCountEqual(
            [item["tender_id"] for item in selected],
            ["expired", "too-soon", "maybe-too-soon", "fresh", "no-deadline"],
        )
        self.assertEqual(deadline_warning_count, 3)

    def test_llm_block_suppresses_report_without_extracted_document_text(self) -> None:
        tender = today_row("stale-llm", llm_recommendation="maybe")
        tender["document_risk_result"] = {
            "documents_with_text": "0.0",
            "key_findings": ["Нет извлеченного текста для анализа документов."],
        }
        tender["llm_report_result"] = {
            "report": {
                "summary": "Старый card-only анализ",
                "potential_offer": {"what_to_offer": ["любой сервер"]},
                "recommendation": {"decision": "go", "confidence": "high"},
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("LLM-анализ не показывается", block)
        self.assertIn("нет извлеченного текста документов", block)
        self.assertNotIn("Старый card-only анализ", block)

    def test_llm_block_marks_report_stale_after_primary_technical_doc_processed(self) -> None:
        report_created_at = datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc)
        tender = today_row("95748890", llm_recommendation="go")
        tender["llm_report_created_at"] = report_created_at
        tender["latest_primary_technical_document_created_at"] = (
            report_created_at + timedelta(minutes=15)
        )
        tender["llm_report_result"] = {
            "report": {
                "summary": "Старый анализ по проекту контракта",
                "recommendation": {"decision": "go", "confidence": "high"},
                "next_action": ["Готовить предложение"],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("LLM-анализ устарел", block)
        self.assertIn("основной технический документ", block)
        self.assertNotIn("Старый анализ по проекту контракта", block)
        self.assertIsNone(self.digest.recent_llm_recommendation(tender))
        self.assertFalse(self.digest.has_llm_report(tender))

    def test_stale_llm_helper_treats_bad_document_count_as_zero(self) -> None:
        tender = {
            "document_risk_result": {"documents_with_text": "not-a-number"},
        }
        result = {
            "report": {
                "summary": "Старый анализ",
            }
        }

        self.assertTrue(
            self.digest.llm_report_is_stale_without_document_text(tender, result)
        )

    def test_full_card_is_compact_call_first_outreach_queue_item(self) -> None:
        tender = today_row("95622481", recommendation="go", llm_recommendation="go")
        tender.update(
            {
                "title": "Поставка серверов",
                "initial_price": 1_000_000,
                "url": "https://example.test/procedure/95622481",
                "published_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
                "deadline_at": datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
                "raw": {
                    "full": {
                        "orderNumber": "95622481",
                        "tenderTypeName": "Электронный аукцион",
                        "tenderStageName": "Подача заявок",
                        "short": {"etpName": "ЭТП"},
                        "contactPerson": "Иван Иванов",
                        "contactPhone": "+7 999 000-00-00",
                        "contactEMail": "customer@example.test",
                        "deliveryPlace": "Москва",
                        "deliveryTerm": "30 календарных дней",
                        "guaranteeOrder": 10_000,
                        "guaranteeExecuteContract": 50_000,
                        "documents": [{"title": "Техническое задание", "id": "doc-1"}],
                    }
                },
                "document_risk_result": {
                    "documents_with_text": 1,
                    "doc_summaries": [
                        {
                            "filename": "ТЗ.pdf",
                            "doc_type": "technical_spec",
                            "doc_type_text": "ТЗ / описание объекта закупки",
                            "text_len": 1000,
                        }
                    ],
                },
                "llm_report_result": {
                    "report": {
                        "summary": "Нужно поставить серверы.",
                        "recommendation": {"decision": "go", "confidence": "high"},
                        "technical_spec_analysis": {"what_is_required": ["сервер"]},
                    }
                },
            }
        )

        card = self.digest.format_card(tender)

        self.assertIn("🟢 Поставка серверов", card)
        self.assertIn("Рекомендация: передать в первичную обработку", card)
        self.assertIn("НМЦК: 1 000 000 ₽", card)
        self.assertIn("Срок подачи: 10.05.2026 12:00", card)
        self.assertIn("Заказчик:", card)
        self.assertIn("Контакт закупки: Иван Иванов, +7 999 000-00-00, customer@example.test", card)
        self.assertIn("Ссылка: https://example.test/procedure/95622481", card)

        outreach = self.outreach_block(card)
        self.assertIn("Кого искать:", outreach)
        self.assertIn("Почему звонить:", outreach)
        self.assertIn("Первая фраза:", outreach)
        self.assertIn("Что выяснить:", outreach)
        field_labels = [line.split(":", 1)[0] for line in outreach.splitlines() if ":" in line]
        self.assertEqual(field_labels, ["Кого искать", "Почему звонить", "Первая фраза", "Что выяснить"])
        self.assertIn("ответственным за ИТ-инфраструктуру", outreach)
        self.assertNotIn("—", outreach)
        self.assertNotIn("{", outreach)
        self.assertNotIn("}", outreach)
        self.assertNotIn("Номер:", card)
        self.assertNotIn("Состав поставки", card)
        self.assertNotIn("Критичные требования ТЗ", card)
        self.assertNotIn("Проблемы и неясности ТЗ", card)
        self.assertNotIn("Фрагменты/сигналы", card)
        self.assertNotIn("Документов с текстом", card)
        self.assertNotIn("Оценка риска", card)
        self.assertNotIn("LLM-пресейл-анализ", card)
        self.assertNotIn("Документы:", card)
        self.assertNotIn("Резюме: Нужно поставить серверы.", card)

    def test_full_card_without_url_says_link_not_found(self) -> None:
        tender = today_row("no-url")

        card = self.digest.format_card(tender)

        self.assertIn("Ссылка: не найдена", card)

    def test_procedure_url_uses_223_order_number_before_stale_notice_info_id(self) -> None:
        tender = lead_report_row("95510304")
        broken_url = (
            "https://zakupki.gov.ru/epz/order/notice/notice223/"
            "common-info.html?noticeInfoId=19716023"
        )
        expected_url = (
            "https://zakupki.gov.ru/epz/order/notice/notice223/"
            "common-info.html?regNumber=32615959798"
        )
        tender["url"] = broken_url
        tender["raw"] = {
            "full": {
                "lawId": 223,
                "orderNumber": "32615959798",
                "tenderUrl": broken_url,
            }
        }

        self.assertEqual(self.digest.procedure_url(tender), expected_url)

        card = self.digest.format_caller_lead_card(tender)

        self.assertIn(f"Ссылка: {expected_url}", card)
        self.assertNotIn(broken_url, card)

    def test_procedure_url_keeps_non_223_stored_url(self) -> None:
        tender = today_row("44fz-url")
        tender["url"] = "https://example.test/44fz-url"
        tender["raw"] = {
            "full": {
                "lawId": 44,
                "orderNumber": "123456789012",
                "tenderUrl": "https://example.test/from-z360",
            }
        }

        self.assertEqual(
            self.digest.procedure_url(tender),
            "https://example.test/44fz-url",
        )

    def test_outreach_block_uses_existing_brief_but_keeps_call_first_wording(self) -> None:
        tender = today_row("outreach-brief", llm_recommendation="go")
        tender["title"] = "Поставка СХД"
        tender["llm_report_result"] = {
            "report": {
                "summary": "Нужна СХД.",
                "outreach_brief": {
                    "who_to_find": "руководитель ИТ-инфраструктуры или ЦОД",
                    "why_call": "Закупка показывает активную задачу по хранению данных. Возможно, рядом есть вопросы backup.",
                    "first_phrase": "Здравствуйте, увидели, что у вас сейчас есть задачи по СХД, хотели познакомиться с ответственным за инфраструктуру.",
                    "questions": [
                        "Кто отвечает за хранение данных?",
                        "Есть ли планы по backup?",
                        "Можно познакомиться с ИТ-руководителем?",
                        "Какой массив нужен?",
                    ],
                },
            }
        }

        card = self.digest.format_card(tender)
        outreach = self.outreach_block(card)

        self.assertIn("Кого искать: руководитель ИТ-инфраструктуры или ЦОД", outreach)
        self.assertIn("Почему звонить: Закупка выглядит как сигнал активной задачи: СХД и хранение данных.", outreach)
        self.assertIn("Первая фраза: Здравствуйте, увидели, что у вас сейчас есть задачи по СХД и хранению данных", outreach)
        self.assertIn("1. Кто отвечает за СХД и хранение данных внутри организации?", outreach)
        self.assertIn("2. Есть ли похожие задачи по росту данных, backup или отказоустойчивости", outreach)
        self.assertIn("3. Можно ли познакомиться с ИТ-руководителем или техническим ответственным?", outreach)
        self.assertNotIn("4.", outreach)
        self.assertNotIn("Какой массив нужен", outreach)
        self.assertNotIn("по вашей закупке №", outreach.lower())

    def test_outreach_brief_procedure_first_opening_is_replaced(self) -> None:
        tender = today_row("procedure-first", llm_recommendation="go")
        tender["title"] = "Поставка серверов"
        tender["llm_report_result"] = {
            "report": {
                "summary": "Нужны серверы.",
                "outreach_brief": {
                    "first_phrase": "Здравствуйте, мы по вашей закупке №123 хотим обсудить участие.",
                    "questions": ["Кто отвечает за это направление внутри организации?"],
                },
            }
        }

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn("Первая фраза: Здравствуйте, увидели, что у вас сейчас есть задачи по серверной инфраструктуре", outreach)
        self.assertNotIn("по вашей закупке", outreach.lower())
        self.assertNotIn("обсудить участие", outreach.lower())

    def test_outreach_fallback_is_generated_without_llm_brief(self) -> None:
        tender = today_row("fallback", llm_recommendation=None)
        tender["title"] = "Модернизация сетевой инфраструктуры"
        tender["result"] = {
            "summary": "Нужны коммутаторы и Wi-Fi.",
            "why_relevant": ["network"],
        }

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn("Почему звонить: Закупка выглядит как сигнал активной задачи: сетевая инфраструктура.", outreach)
        self.assertIn("Кого искать:", outreach)
        self.assertIn("Первая фраза:", outreach)
        self.assertIn("Что выяснить:", outreach)

    def test_outreach_category_mapping_keeps_servers_out_of_storage(self) -> None:
        tender = today_row("server-topic", llm_recommendation="go")
        tender["title"] = "Поставка серверов для обработки данных"
        tender["llm_report_result"] = {
            "report": {
                "summary": "Нужны серверы для обработки данных.",
            }
        }

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn("серверная инфраструктура", outreach)
        self.assertNotIn("активной задачи: СХД и хранение данных", outreach)

    def test_outreach_category_mapping_detects_storage(self) -> None:
        tender = today_row("storage-topic", llm_recommendation="go")
        tender["title"] = "Поставка СХД для резервного копирования"

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn("СХД и хранение данных", outreach)

    def test_outreach_category_mapping_detects_security_pak(self) -> None:
        tender = today_row("security-topic", llm_recommendation="go")
        tender["title"] = "Поставка ПАК защиты информации"

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn("защищенная инфраструктура / ИБ", outreach)
        self.assertNotIn("активной задачи: серверная инфраструктура", outreach)

    def test_outreach_questions_do_not_include_procedure_first_words(self) -> None:
        tender = today_row("procedure-questions", llm_recommendation="go")
        tender["title"] = "Поставка серверов"
        tender["llm_report_result"] = {
            "report": {
                "outreach_brief": {
                    "questions": [
                        "Какой срок поставки?",
                        "Допускается ли подача заявки?",
                        "Какая группа реестра?",
                    ],
                },
                "questions_to_customer": [
                    "ПП 1875 применяется?",
                    "Требуется ли письмо производителя?",
                    "Допускается эквивалент?",
                ],
            }
        }

        outreach = self.outreach_block(self.digest.format_card(tender)).lower()

        for banned in (
            "срок поставки",
            "подача заявки",
            "реестр",
            "пп 1875",
            "письмо производителя",
            "эквивалент",
        ):
            self.assertNotIn(banned, outreach)

    def test_outreach_who_to_find_handles_procurement_center_customer(self) -> None:
        tender = today_row("procurement-center", llm_recommendation="go")
        tender["title"] = "Поставка серверов"
        tender["customer_name"] = "Комитет государственных закупок города Москвы"
        tender["raw"] = {
            "full": {
                "contactPerson": "Закупочный специалист",
                "contactPhone": "+7 999 000-00-00",
            }
        }

        outreach = self.outreach_block(self.digest.format_card(tender))

        self.assertIn(
            "Кого искать: технического заказчика или ИТ-ответственного у конечного получателя; "
            "закупочный контакт использовать как вход и попросить соединить с ИТ.",
            outreach,
        )

    def test_processing_status_uses_api_documents_count_when_available(self) -> None:
        tender = today_row("95721719", docs_count=2, docs_with_text=2)
        tender["raw"] = {
            "full": {
                "documents": [
                    {"id": "notice", "title": "444854856_1_Извещение_ОНМ_серверы пк.docx"},
                    {"id": "contract", "title": "444854860_2_Проект договора обор-е.docx"},
                    {"id": "kd", "title": "2_КД_материалы_ОНМ_серверы пк.docx"},
                    {"id": "appendix", "title": "Приложение 2 Техническое задание.docx"},
                ]
            }
        }

        block = self.digest.processing_status_block(tender)

        self.assertIn("2 из 4 доступных", block)
        self.assertNotIn("Документы обработаны (2/2)", block)
        self.assertIn(
            "Есть необработанные документы, похожие на КД/ТЗ/спецификацию",
            block,
        )

    def test_partial_rate_limit_document_warning_is_shown_in_digest_blocks(self) -> None:
        tender = today_row("partial-docs", docs_count=1, docs_with_text=1)
        tender["partial_document_download_event"] = json.dumps(
            {
                "status": "partial_document_download",
                "reason": "rate_limit_429",
                "documents_skipped_due_to_rate_limit": 2,
                "stopped_due_to_rate_limit": True,
            }
        )
        tender["document_risk_result"] = {
            "tailoring_risk": "low",
            "risk_score": 10,
            "documents_with_text": 1,
        }

        warning = self.digest.PARTIAL_DOCUMENT_DOWNLOAD_WARNING_TEXT

        self.assertIn(warning, self.digest.processing_status_block(tender))
        self.assertIn(warning, self.digest.document_analysis_block(tender))

    def test_preliminary_no_tech_spec_card_marks_warning_and_caps_confidence(self) -> None:
        tender = today_row("95622481", llm_recommendation="go")
        tender["docs_with_text"] = 2
        tender["document_risk_result"] = {
            "documents_with_text": 2,
            "doc_summaries": [
                {
                    "filename": "Извещение.pdf",
                    "doc_type": "other",
                    "doc_type_text": "прочий документ",
                    "text_len": 3000,
                },
                {
                    "filename": "Проект договора.pdf",
                    "doc_type": "contract",
                    "doc_type_text": "проект контракта",
                    "text_len": 5000,
                },
            ],
        }
        tender["llm_report_result"] = {
            "report": {
                "summary": "ТЗ отсутствует в доступных документах.",
                "recommendation": {"decision": "go", "confidence": "high"},
                "next_action": ["Позвонить заказчику и уточнить комплект."],
            }
        }

        block = self.digest.llm_presales_block(tender)
        card = self.digest.format_card(tender)

        self.assertIn("⚠️ Предварительный разбор без ТЗ/спецификации", block)
        self.assertIn("В доступных документах не найдено техническое задание или спецификация", block)
        self.assertIn("Вердикт LLM: maybe / уверенность: low", block)
        self.assertLess(
            block.index("Запросить ТЗ/спецификацию/перечень оборудования"),
            block.index("Позвонить заказчику"),
        )
        self.assertIn("📞 Для знакомства", card)
        self.assertIn("Рекомендация: проверить и взять в работу", card)
        self.assertNotIn("⚠️ Предварительный разбор без ТЗ/спецификации", card)
        self.assertNotIn("Рекомендованные действия:", card)
        self.assertNotIn("Запросить ТЗ/спецификацию/перечень оборудования", card)
        self.assertEqual(self.digest.recent_llm_recommendation(tender), "maybe")

    def test_references_only_specification_keeps_preliminary_warning(self) -> None:
        tender = today_row("95721719", llm_recommendation="go")
        tender["docs_with_text"] = 1
        tender["document_risk_result"] = {
            "documents_with_text": 1,
            "doc_summaries": [
                {
                    "filename": "Проект договора обор-е.docx",
                    "doc_type": "contract",
                    "doc_type_text": "проект контракта",
                    "technical_spec_detection": {
                        "detected": False,
                        "reason": "references_specification_only",
                    },
                    "text_len": 5000,
                }
            ],
        }
        tender["llm_report_result"] = {
            "meta": {
                "documents_with_text": 1,
                "technical_spec_documents_count": 0,
                "documents_summary": [
                    {
                        "filename": "Проект договора обор-е.docx",
                        "doc_type": "проект контракта",
                        "technical_spec_reference_note": (
                            "Документ ссылается на спецификацию, но сама спецификация не найдена"
                        ),
                        "text_len": 5000,
                    }
                ],
            },
            "report": {
                "summary": "Спецификация упоминается в договоре, но таблицы нет.",
                "recommendation": {"decision": "go", "confidence": "high"},
            },
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("Предварительный разбор без ТЗ/спецификации", block)
        self.assertIn("Спецификация упоминается в документах", block)
        self.assertIn("Вердикт LLM: maybe / уверенность: low", block)

    def test_llm_presales_block_renders_delivery_list_as_bullets(self) -> None:
        tender = today_row("delivery-list")
        tender["llm_report_result"] = {
            "report": {
                "summary": "Сроки надо проверить.",
                "delivery_feasibility": [
                    "Общий срок исполнения 90 дней",
                    "ПНР зависит от доступа на объект",
                ],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("Срок поставки / оценка:", block)
        self.assertIn("— Общий срок исполнения 90 дней", block)
        self.assertIn("— ПНР зависит от доступа на объект", block)
        self.assertNotIn("['Общий срок", block)

    def test_llm_presales_block_uses_customer_clarification_heading(self) -> None:
        tender = today_row("questions")
        tender["llm_report_result"] = {
            "report": {
                "summary": "Есть вопросы.",
                "questions_to_customer": ["Подтвердить возможность аналога"],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("Что уточнить у заказчика:", block)
        self.assertNotIn("Что спросить заказчику", block)

    def test_llm_presales_block_uses_precise_tz_problems_heading(self) -> None:
        tender = today_row("tz-problems")
        tender["llm_report_result"] = {
            "report": {
                "summary": "Есть неясность.",
                "problems_in_tz": ["Срок поставки в ТЗ не указан."],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("Проблемы и неясности ТЗ:", block)
        self.assertIn("— Срок поставки в ТЗ не указан.", block)
        self.assertNotIn("Проблемы в ТЗ:", block)

    def test_llm_presales_block_cleans_stored_hardware_requirements_from_tz_problems(self) -> None:
        tender = today_row("stored-bad-problems")
        tender["llm_report_result"] = {
            "report": {
                "summary": "Старый сохраненный отчет.",
                "critical_requirements": None,
                "problems_in_tz": [
                    "Количество и конфигурация CPU (ядра, потоки, частота, кэш)",
                    "Тип, объем, скорость и количество слотов ОЗУ",
                    "Тип, интерфейс, объем и скорость вращения дисков",
                    "Уровни RAID и объем кэша контроллера",
                    "Резервирование и мощность БП/охлаждения",
                    "Высота в стойке (≤2U)",
                    "Версия PCIe (5.0)",
                    "Наличие выделенного порта управления и видеоинтерфейса",
                ],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn("Проблемы и неясности ТЗ:", block)
        self.assertIn(
            "— Явных противоречий в ТЗ не найдено; проверить срок поставки, "
            "эквивалентность и реестровые требования.",
            block,
        )
        self.assertNotIn("Количество и конфигурация CPU", block)
        self.assertNotIn("Тип, объем, скорость", block)
        self.assertNotIn("Уровни RAID", block)
        self.assertNotIn("видеоинтерфейса", block)

    def test_llm_presales_block_keeps_real_tz_contradiction_after_cleanup(self) -> None:
        tender = today_row("stored-real-problem")
        problem = "Одновременное требование 12 SFF и 12 LFF в 2U физически невозможно"
        tender["llm_report_result"] = {
            "report": {
                "summary": "Есть реальное противоречие.",
                "problems_in_tz": [problem],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn(f"— {problem}", block)
        self.assertNotIn("Явных противоречий", block)

    def test_llm_presales_block_keeps_missing_delivery_term_after_cleanup(self) -> None:
        tender = today_row("stored-missing-delivery")
        problem = "Срок поставки не указан"
        tender["llm_report_result"] = {
            "report": {
                "summary": "Есть неясность по сроку.",
                "problems_in_tz": [problem],
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertIn(f"— {problem}", block)
        self.assertNotIn("Явных противоречий", block)

    def test_deep_llm_guidance_suppresses_generic_checks_and_next_action(self) -> None:
        tender = today_row("deep-guidance")
        tender["result"] = {
            "why_relevant": ["profile"],
            "manual_checks": ["Проверить требования к участнику"],
        }
        tender["llm_report_result"] = {
            "report": {
                "summary": "Глубокий разбор готов.",
                "what_presales_should_check": ["Проверить реестр и поставляемость"],
                "next_action": ["Запросить КП у дистрибьютора"],
            }
        }

        card = self.digest.format_card(tender)

        self.assertIn("📞 Для знакомства", card)
        self.assertNotIn("Что проверить пресейлу:", card)
        self.assertNotIn("Рекомендованные действия:", card)
        self.assertNotIn("\nЧто проверить:\n", card)
        self.assertNotIn("\nСледующее действие:\n", card)

    def test_regular_technical_spec_card_does_not_get_preliminary_warning(self) -> None:
        tender = today_row("normal", llm_recommendation="go")
        tender["docs_with_text"] = 1
        tender["document_risk_result"] = {
            "documents_with_text": 1,
            "doc_summaries": [
                {
                    "filename": "ТЗ.pdf",
                    "doc_type": "technical_spec",
                    "doc_type_text": "ТЗ / описание объекта закупки",
                    "text_len": 3000,
                }
            ],
        }
        tender["llm_report_result"] = {
            "report": {
                "summary": "Есть нормальное ТЗ.",
                "recommendation": {"decision": "go", "confidence": "high"},
                "technical_spec_analysis": {"what_is_required": ["сервер 2 шт."]},
                "extracted_spec_facts": {
                    "supply_items_count": 1,
                    "technical_characteristics_count": 1,
                },
            }
        }

        block = self.digest.llm_presales_block(tender)

        self.assertNotIn("Предварительный разбор без ТЗ/спецификации", block)
        self.assertIn("Вердикт LLM: go / уверенность: high", block)

    def test_content_detected_kd_spec_does_not_get_preliminary_warning(self) -> None:
        tender = today_row("kd-hidden-spec", llm_recommendation="go")
        tender["docs_with_text"] = 1
        tender["document_risk_result"] = {
            "documents_with_text": 1,
            "doc_summaries": [
                {
                    "filename": "КД материалы.docx",
                    "doc_type": "technical_spec",
                    "doc_type_text": "ТЗ / описание объекта закупки",
                    "doc_type_reason": "technical_section_found_inside_document",
                    "technical_spec_detection": {"detected": True},
                    "text_len": 6000,
                }
            ],
        }
        tender["llm_report_result"] = {
            "meta": {
                "documents_with_text": 1,
                "technical_spec_documents_count": 1,
                "technical_spec_found_inside_count": 1,
                "documents_summary": [
                    {
                        "filename": "КД материалы.docx",
                        "doc_type": "ТЗ / описание объекта закупки",
                        "doc_type_reason": "technical_section_found_inside_document",
                        "text_len": 6000,
                    }
                ],
            },
            "report": {
                "summary": "ТЗ спрятано внутри КД.",
                "recommendation": {"decision": "go", "confidence": "high"},
            },
        }

        block = self.digest.llm_presales_block(tender)

        self.assertNotIn("Предварительный разбор без ТЗ/спецификации", block)
        self.assertIn("Техническая часть найдена в КД/приложении", block)
        self.assertIn("Вердикт LLM: go / уверенность: high", block)

    def test_spec_facts_suppress_preliminary_warning_without_tz_filename(self) -> None:
        tender = today_row("facts-hidden-spec", llm_recommendation="go")
        tender["docs_with_text"] = 1
        tender["document_risk_result"] = {
            "documents_with_text": 1,
            "doc_summaries": [
                {
                    "filename": "Документация.xlsx",
                    "doc_type": "other",
                    "doc_type_text": "прочий документ",
                    "text_len": 5000,
                }
            ],
        }
        tender["llm_report_result"] = {
            "meta": {
                "documents_with_text": 1,
                "technical_spec_documents_count": 0,
                "spec_facts": {
                    "supply_items_count": 1,
                    "technical_characteristics_count": 1,
                    "supply_items": [{"name": "Сервер", "quantity": "2"}],
                },
            },
            "report": {
                "summary": "Спецификация извлечена из таблицы.",
                "recommendation": {"decision": "go", "confidence": "high"},
            },
        }

        block = self.digest.llm_presales_block(tender)

        self.assertNotIn("Предварительный разбор без ТЗ/спецификации", block)
        self.assertIn("Вердикт LLM: go / уверенность: high", block)

    def test_regular_digest_summary_shows_deadline_warning_count(self) -> None:
        cards = [self.digest.TenderDigestCard(tender_id="fresh", message_text="card")]
        stats = self.digest.DigestSelectionStats(total_found=1, shown=1, deadline_warning=2)

        with patch.object(self.digest, "build_digest_cards_with_selection_stats", return_value=(cards, stats)):
            messages = self.digest.build_digest_messages()

        self.assertIn("С близким/истекшим сроком: 2", messages[0])

    def test_today_digest_hides_llm_no_go(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            today_row("hidden-llm-no-go", deadline_at=now + timedelta(days=4), llm_recommendation="no_go"),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_no_go, 1)
        self.assertEqual(stats.shown, 1)

    def test_today_digest_hides_current_hard_noise_candidate(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        noise = today_row("hard-noise", deadline_at=now + timedelta(days=4))
        noise["title"] = "Kaspersky antivirus license renewal"
        rows = [noise, today_row("visible", deadline_at=now + timedelta(days=4))]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_by_lead_hard_noise, 1)

    def test_today_digest_hides_latest_lead_triage_reject_without_full_report(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rejected = today_row("triage-reject", deadline_at=now + timedelta(days=4))
        rejected.update(
            {
                "lead_triage_analysis_type": self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                "lead_triage_decision": "reject",
                "lead_triage_created_at": now,
                "lead_triage_result": {"report": {"lead_decision": "reject"}},
            }
        )
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            rejected,
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_by_lead_triage_reject, 1)
        self.assertEqual(stats.shown, 1)

    def test_today_digest_hides_latest_sales_in_work(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            today_row(
                "in-work",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_in_work",
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.shown, 1)

    def test_today_digest_hides_latest_sales_not_relevant(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            today_row(
                "not-relevant",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_not_relevant",
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.shown, 1)

    def test_today_digest_hides_negative_sales_feedback_statuses(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [today_row("visible", deadline_at=now + timedelta(days=4))]
        negative_statuses = tuple(sorted(self.digest.NEGATIVE_SALES_FEEDBACK_STATUSES))
        rows.extend(
            today_row(status, deadline_at=now + timedelta(days=4), latest_sales_status=status)
            for status in negative_statuses
        )

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_by_feedback, len(negative_statuses))

    def test_today_digest_hides_existing_client_customer_marker(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        marked = today_row("same-customer-new", deadline_at=now + timedelta(days=4))
        marked["existing_client_customer_status"] = "sales_existing_client"
        marked["existing_client_source_tender_id"] = "source-tender"
        rows = [today_row("visible", deadline_at=now + timedelta(days=4)), marked]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_by_feedback, 1)

    def test_today_digest_shows_latest_sales_returned(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "returned",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_returned",
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["returned"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.shown, 1)

    def test_filter_feedback_statuses_do_not_hide_today_rows(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "filter-feedback",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="filter_hidden_wrong",
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["filter-feedback"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.shown, 1)

    def test_today_digest_keeps_deadline_risk_rows(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("expired", deadline_at=now - timedelta(hours=1)),
            today_row("too-soon", deadline_at=now + timedelta(days=2, hours=23)),
            today_row("fresh", deadline_at=now + timedelta(days=3)),
            today_row("no-deadline", deadline_at=None),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["expired", "fresh", "no-deadline", "too-soon"])
        self.assertEqual(manual_rows, [])
        self.assertEqual(stats.hidden_deadline, 0)
        self.assertEqual(stats.deadline_warning, 2)
        self.assertEqual(stats.shown, 4)

    def test_today_digest_moves_manual_document_cases_to_separate_block(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            today_row(
                "marketplace-auth",
                deadline_at=now + timedelta(days=4),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "no-valid-documents",
                deadline_at=now + timedelta(days=4),
                preparation_blocked_event=json.dumps({"status": "no_valid_documents"}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "documents-missing",
                deadline_at=now + timedelta(days=4),
                preparation_requeue_event=json.dumps({"exhausted": True, "documents_downloaded": 0}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "not-ready",
                deadline_at=now + timedelta(days=4),
                preparation_requeue_event=json.dumps(
                    {"exhausted": True, "documents_downloaded": 1, "documents_with_text": 0}
                ),
                docs_count=1,
                docs_with_text=0,
            ),
            today_row(
                "manual-too-soon",
                deadline_at=now + timedelta(days=2),
                preparation_blocked_event=json.dumps({"status": "no_valid_documents"}),
                docs_count=0,
                docs_with_text=0,
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertCountEqual(
            [item["tender_id"] for item in manual_rows],
            ["marketplace-auth", "no-valid-documents", "documents-missing", "manual-too-soon"],
        )
        self.assertEqual(stats.manual_documents_needed, 4)
        self.assertEqual(stats.hidden_deadline, 0)
        self.assertEqual(stats.deadline_warning, 1)
        self.assertEqual(stats.hidden_preparation, 1)

    def test_today_digest_dedupes_external_id_and_prefers_operational_lead_card(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        manual = today_row(
            "manual-today",
            preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
            docs_count=0,
            docs_with_text=0,
        )
        lead = lead_report_row("lead-today", report_recommendation="go", created_at=now)
        manual["external_id"] = "same-external"
        lead["external_id"] = "same-external"

        with patch.object(self.digest, "get_digest_rows_today", return_value=[manual, lead]):
            cards, manual_cards, stats = self.digest.build_digest_cards_today_with_stats(
                limit=10,
                now=now,
            )

        self.assertEqual([card.tender_id for card in cards], ["lead-today"])
        self.assertEqual(manual_cards, [])
        self.assertEqual(stats.shown, 1)
        self.assertEqual(stats.manual_documents_needed, 0)

    def test_today_includes_target_hardware_marketplace_auth_manual_document_item(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "RST32615987873",
                deadline_at=now + timedelta(days=7),
                preparation_blocked_event=json.dumps({"reason": "marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            )
        ]

        with (
            patch.object(self.digest, "get_digest_rows_today", return_value=rows),
            patch.object(
                self.digest,
                "business_assessment",
                return_value={
                    "action": "proceed",
                    "market_access": "target_hardware",
                    "commercial_interest": "high",
                    "tailoring_effect": "neutral",
                },
            ),
        ):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual(selected, [])
        self.assertEqual([item["tender_id"] for item in manual_rows], ["RST32615987873"])
        self.assertEqual(manual_rows[0]["_manual_document_reason"], "preparation_blocked=marketplace_auth")
        self.assertEqual(stats.manual_documents_needed, 1)
        self.assertEqual(stats.hidden_no_go, 0)
        self.assertEqual(stats.hidden_deadline, 0)

    def test_today_hides_no_go_but_keeps_deadline_risk_marketplace_auth_items(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "no-go-marketplace-auth",
                recommendation="no_go",
                deadline_at=now + timedelta(days=7),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "deadline-risk-marketplace-auth",
                deadline_at=now + timedelta(days=2),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
        ]

        with (
            patch.object(self.digest, "get_digest_rows_today", return_value=rows),
            patch.object(
                self.digest,
                "business_assessment",
                return_value={
                    "action": "proceed",
                    "market_access": "target_hardware",
                    "commercial_interest": "high",
                    "tailoring_effect": "neutral",
                },
            ),
        ):
            selected, manual_rows, stats = self.digest.select_digest_rows_today(limit=10, now=now)

        self.assertEqual(selected, [])
        self.assertEqual([item["tender_id"] for item in manual_rows], ["deadline-risk-marketplace-auth"])
        self.assertEqual(stats.hidden_no_go, 1)
        self.assertEqual(stats.hidden_deadline, 0)
        self.assertEqual(stats.deadline_warning, 1)

    def test_today_manual_document_card_explains_action(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "95633694",
                deadline_at=now + timedelta(days=4),
                preparation_blocked_event=json.dumps({"status": "no_valid_documents"}),
                docs_count=0,
                docs_with_text=0,
            )
        ]
        rows[0]["title"] = "Поставка серверов"
        rows[0]["initial_price"] = 2_500_000
        rows[0]["url"] = "https://example.test/procedure/95633694"
        rows[0]["raw"] = {
            "full": {
                "lawId": 3,
                "documents": [{"id": "doc-1", "title": "Техническое задание"}],
            }
        }

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            cards, manual_cards, stats = self.digest.build_digest_cards_today_with_stats(limit=10, now=now)

        self.assertEqual(cards, [])
        self.assertEqual([card.tender_id for card in manual_cards], ["95633694"])
        self.assertEqual(stats.manual_documents_needed, 1)
        self.assertIn("📄 Нужны документы для LLM-разбора", manual_cards[0].message_text)
        self.assertIn("Поставка серверов", manual_cards[0].message_text)
        self.assertIn("НМЦК: 2 500 000 ₽", manual_cards[0].message_text)
        self.assertIn("Срок подачи:", manual_cards[0].message_text)
        self.assertIn("Заказчик:", manual_cards[0].message_text)
        self.assertIn("Ссылка: https://example.test/procedure/95633694", manual_cards[0].message_text)
        self.assertIn(
            "Причина: документы закрыты на площадке или не отдаются автоматически, нужно скачать КД/ТЗ вручную.",
            manual_cards[0].message_text,
        )
        self.assertIn("Почему важно: Закупка выглядит как сигнал активной задачи: серверная инфраструктура.", manual_cards[0].message_text)
        self.assertIn("📞 Для знакомства", manual_cards[0].message_text)
        self.assertNotIn("lawId", manual_cards[0].message_text)
        self.assertNotIn("Статус обработки", manual_cards[0].message_text)
        self.assertNotIn("LLM-анализ ещё не выполнялся", manual_cards[0].message_text)
        self.assertNotIn("Документы не скачаны", manual_cards[0].message_text)
        self.assertNotIn("id:", manual_cards[0].message_text)

    def test_today_manual_document_card_shows_user_facing_core_fields(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "95633695",
                deadline_at=now + timedelta(days=4),
                preparation_blocked_event=json.dumps({"status": "blocked_by_marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            )
        ]
        rows[0]["title"] = "Поставка СХД"

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            cards, manual_cards, stats = self.digest.build_digest_cards_today_with_stats(limit=10, now=now)

        self.assertEqual(cards, [])
        self.assertEqual([card.tender_id for card in manual_cards], ["95633695"])
        self.assertEqual(stats.manual_documents_needed, 1)

        text = manual_cards[0].message_text
        self.assertIn("Причина: документы закрыты на площадке, нужно скачать КД/ТЗ вручную.", text)
        self.assertIn("Почему важно: Закупка выглядит как сигнал активной задачи: СХД и хранение данных.", text)
        self.assertLess(text.index("НМЦК:"), text.index("Срок подачи:"))
        self.assertLess(text.index("Срок подачи:"), text.index("Заказчик:"))
        self.assertLess(text.index("Заказчик:"), text.index("Ссылка:"))
        self.assertNotIn("Публикация:", text)
        self.assertNotIn("Закон:", text)

    def test_today_digest_does_not_require_llm_report_for_working_card(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [today_row("rule-based-only", deadline_at=now + timedelta(days=4))]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            cards, manual_cards, stats = self.digest.build_digest_cards_today_with_stats(limit=10, now=now)

        self.assertEqual([card.tender_id for card in cards], ["rule-based-only"])
        self.assertEqual(manual_cards, [])
        self.assertIn("📞 Для знакомства", cards[0].message_text)
        self.assertIn("Почему звонить:", cards[0].message_text)
        self.assertEqual(stats.shown, 1)

    def test_in_work_digest_shows_latest_sales_in_work_only(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "in-work-newer",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_in_work",
                latest_sales_status_created_at=now - timedelta(minutes=10),
            ),
            today_row(
                "returned",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_returned",
                latest_sales_status_created_at=now - timedelta(minutes=5),
            ),
            today_row(
                "not-relevant",
                deadline_at=now + timedelta(days=4),
                latest_sales_status="sales_not_relevant",
                latest_sales_status_created_at=now - timedelta(minutes=1),
            ),
            today_row(
                "in-work-older",
                deadline_at=now + timedelta(days=5),
                latest_sales_status="sales_in_work",
                latest_sales_status_created_at=now - timedelta(hours=2),
            ),
        ]

        with patch.object(self.digest, "get_in_work_digest_rows", return_value=rows):
            selected, hidden_expired = self.digest.select_in_work_digest_rows(limit=10, now=now)

        self.assertEqual(
            [item["tender_id"] for item in selected],
            ["in-work-newer", "in-work-older"],
        )
        self.assertEqual(hidden_expired, 0)

    def test_in_work_digest_hides_expired_rows(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row(
                "expired",
                deadline_at=now - timedelta(minutes=1),
                latest_sales_status="sales_in_work",
                latest_sales_status_created_at=now - timedelta(minutes=10),
            ),
            today_row(
                "active",
                deadline_at=now + timedelta(minutes=1),
                latest_sales_status="sales_in_work",
                latest_sales_status_created_at=now - timedelta(minutes=20),
            ),
        ]

        with patch.object(self.digest, "get_in_work_digest_rows", return_value=rows):
            selected, hidden_expired = self.digest.select_in_work_digest_rows(limit=10, now=now)

        self.assertEqual([item["tender_id"] for item in selected], ["active"])
        self.assertEqual(hidden_expired, 1)

    def test_today_digest_summary_shows_filtered_counts(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            today_row("visible", deadline_at=now + timedelta(days=4)),
            today_row("hidden-no-go", recommendation="no_go", deadline_at=now + timedelta(days=4)),
            today_row("hidden-deadline", deadline_at=now + timedelta(days=2)),
            today_row(
                "manual-docs",
                deadline_at=now + timedelta(days=4),
                preparation_blocked_event=json.dumps({"reason": "marketplace_auth"}),
                docs_count=0,
                docs_with_text=0,
            ),
            today_row(
                "hidden-prep",
                deadline_at=now + timedelta(days=4),
                preparation_requeue_event=json.dumps(
                    {"exhausted": True, "documents_downloaded": 1, "documents_with_text": 0}
                ),
                docs_count=1,
                docs_with_text=0,
            ),
        ]

        with patch.object(self.digest, "get_digest_rows_today", return_value=rows):
            messages = self.digest.build_digest_messages_today(limit=10, now=now)

        self.assertIn("Найдено сегодня всего: 5", messages[0])
        self.assertIn("Показано рабочих кандидатов: 2", messages[0])
        self.assertIn("Нужны документы для LLM-разбора: 1", messages[0])
        self.assertIn("Скрыто no_go: 1", messages[0])
        self.assertIn("С близким/истекшим сроком: 1", messages[0])
        self.assertIn("Скрыто по документам/доступу: 1", messages[0])
        self.assertIn("Нужны документы для LLM-разбора: 1", messages[-2])

    def test_lead_selection_audit_shows_go_maybe_reject_processed_sections(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        go_rows = [
            lead_report_row("go-new", created_at=now),
        ]
        maybe = lead_triage_row("maybe-lead", "maybe")
        reject = lead_triage_row("reject-lead", "reject", reason="one-off support renewal")
        processed = [
            lead_report_row("processed-work", latest_sales_status="sales_in_work", created_at=now),
            lead_report_row("processed-reject", latest_sales_status="sales_not_relevant", created_at=now),
        ]

        with (
            patch.object(
                self.digest,
                "select_operational_lead_all_rows",
                return_value=(
                    go_rows,
                    self.digest.OperationalLeadAllStats(
                        total_found=3,
                        shown=1,
                        new_count=1,
                        hidden_in_work=1,
                    ),
                ),
            ),
            patch.object(self.digest, "get_lead_triage_audit_rows", return_value=[maybe, reject]),
            patch.object(self.digest, "get_lead_processed_audit_rows", return_value=processed),
            patch.object(self.digest, "latest_lead_hard_noise_summary", return_value=None),
        ):
            messages = self.digest.build_today_filter_audit_messages(limit=50, now=now)

        text = "\n\n".join(messages)
        self.assertIn("AI Tender Radar - Аудит отбора", messages[0])
        self.assertIn("Отобраны как лиды: 1", messages[0])
        self.assertIn("Отложены: 1", messages[0])
        self.assertIn("Отклонены triage: 1", messages[0])
        self.assertIn("Отсечены hard-noise: diagnostics доступны в run log", messages[0])
        self.assertIn("Уже обработаны: 2", messages[0])
        self.assertIn("Отобраны как лиды", text)
        self.assertIn("Решение: отобран как клиентский лид", text)
        self.assertIn("Решение: отложен для ручной проверки", text)
        self.assertIn("Решение: отклонен как нерелевантный сигнал", text)
        self.assertIn("Решение: уже в работе", text)
        self.assertIn("Решение: отклонен пользователем", text)
        self.assertIn("one-off support renewal", text)
        self.assertNotIn("Проверка фильтра", text)
        self.assertNotIn("🔎 Разобрать", text)
        self.assertNotIn(FORBIDDEN_ROLE_WORD, text.lower())

    def test_lead_selection_audit_limits_cards_and_splits_without_cutting_links(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        go_rows = [lead_report_row(f"visible-{index}", created_at=now) for index in range(3)]

        with (
            patch.object(
                self.digest,
                "select_operational_lead_all_rows",
                return_value=(
                    go_rows[:2],
                    self.digest.OperationalLeadAllStats(total_found=3, shown=2, new_count=2),
                ),
            ),
            patch.object(self.digest, "get_lead_triage_audit_rows", return_value=[]),
            patch.object(self.digest, "get_lead_processed_audit_rows", return_value=[]),
            patch.object(self.digest, "latest_lead_hard_noise_summary", return_value=None),
        ):
            messages = self.digest.build_today_filter_audit_messages(
                limit=2,
                now=now,
                message_limit=260,
            )

        text = "\n\n".join(messages)
        self.assertIn("visible-0", text)
        self.assertIn("visible-1", text)
        self.assertNotIn("visible-2 |", text)
        self.assertIn("Ссылка: https://example.test/visible-0", text)
        self.assertIn("Ссылка: https://example.test/visible-1", text)
        self.assertGreater(len(messages), 2)

    def test_today_query_parameterizes_llm_report_like_pattern(self) -> None:
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_digest_rows_today(limit=10)

        self.assertEqual(rows, [])
        self.assertIn("a.analysis_type LIKE %s", cursor.query)
        self.assertNotIn("LIKE 'llm_presales_report%'", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                "llm_presales_report%",
                self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                *self.digest.SALES_WORKFLOW_STATUSES,
                self.digest.SALES_EXISTING_CLIENT_STATUS,
                self.digest.PREPARATION_BLOCKED_EVENT,
                self.digest.PREPARATION_REQUEUE_EVENT,
                self.digest.PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                self.digest.DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                10,
            ),
        )

    def test_today_digest_cards_query_escapes_literal_percent_patterns(self) -> None:
        cursor = FakeCursor(on_execute=lambda query, params: assert_no_unsafe_pyformat_percent(query))

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            cards, manual_cards, stats = self.digest.build_digest_cards_today_with_stats(limit=10)

        self.assertEqual(cards, [])
        self.assertEqual(manual_cards, [])
        self.assertEqual(stats.total_found, 0)
        self.assertIn("LIKE '%%техническое задание%%'", cursor.query)

    def test_regular_digest_cards_query_escapes_literal_percent_patterns(self) -> None:
        cursor = FakeCursor(on_execute=lambda query, params: assert_no_unsafe_pyformat_percent(query))

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            cards, hidden_deadline = self.digest.build_digest_cards_with_stats(limit=10)

        self.assertEqual(cards, [])
        self.assertEqual(hidden_deadline, 0)
        self.assertIn("LIKE '%%техническое задание%%'", cursor.query)

    def test_latest_sales_feedback_query_uses_safe_status_parameters(self) -> None:
        cursor = FakeCursor(on_execute=lambda query, params: assert_no_unsafe_pyformat_percent(query))

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_digest_rows(limit=10)

        self.assertEqual(rows, [])
        expected_status_placeholders = self.digest.sql_placeholders(
            len(self.digest.SALES_FEEDBACK_LOOKUP_STATUSES)
        )
        self.assertIn(f"f.status IN ({expected_status_placeholders})", cursor.query)
        self.assertNotIn("LIKE 'sales_%'", cursor.query)
        self.assertNotIn("LIKE 'filter_%'", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                *self.digest.SALES_WORKFLOW_STATUSES,
                self.digest.SALES_EXISTING_CLIENT_STATUS,
                self.digest.PREPARATION_BLOCKED_EVENT,
                self.digest.PREPARATION_REQUEUE_EVENT,
                self.digest.PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                self.digest.DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                10,
            ),
        )

    def test_digest_rows_pagination_is_parameterized_and_stable(self) -> None:
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_digest_rows(limit=10, offset=20)

        self.assertEqual(rows, [])
        self.assertIn(
            "ORDER BY a.score DESC, t.deadline_at ASC NULLS LAST, t.id ASC",
            cursor.query,
        )
        self.assertIn("LIMIT %s OFFSET %s", cursor.query)
        self.assertEqual(cursor.params[-2:], (10, 20))

    def test_digest_sql_source_has_no_raw_single_percent_like_literals(self) -> None:
        source = Path(self.digest.__file__).read_text(encoding="utf-8")

        self.assertNotRegex(
            source,
            r"\b(?:LIKE|ILIKE)\s+'[^']*(?<!%)%(?!%)[^']*'",
        )

    def test_recent_query_filters_llm_reports_by_created_at(self) -> None:
        cutoff = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_recent_llm_digest_rows(6, limit=10, cutoff=cutoff)

        self.assertEqual(rows, [])
        self.assertIn("lr.analysis_type = %s", cursor.query)
        self.assertNotIn("lr.analysis_type LIKE %s", cursor.query)
        self.assertIn("lr.created_at >= %s", cursor.query)
        self.assertIn("a.analysis_type IN ('rule_based_score', 'rule_based_scoring')", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                cutoff,
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                self.digest.PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                self.digest.DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                10,
            ),
        )

    def test_recent_query_can_include_labeled_reports_for_debug(self) -> None:
        cutoff = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_recent_llm_digest_rows(
                6,
                limit=10,
                cutoff=cutoff,
                include_labeled=True,
            )

        self.assertEqual(rows, [])
        self.assertIn("lr.analysis_type LIKE %s", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                f"{self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE}%",
                cutoff,
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                self.digest.PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                self.digest.DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                10,
            ),
        )

    def test_operational_lead_report_rows_include_maybe_and_sort_high_first(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

        def lead_row(
            tender_id: str,
            recommendation: str,
            priority: object,
            created_at: datetime,
            *,
            analysis_type: str = "llm_customer_lead_report",
            rule_recommendation: str = "go",
            latest_sales_status: str | None = None,
        ) -> dict:
            return {
                "tender_id": tender_id,
                "llm_report_analysis_type": analysis_type,
                "llm_report_recommendation": recommendation,
                "llm_report_result": {"report": {"lead_priority": priority}},
                "llm_report_created_at": created_at,
                "recommendation": rule_recommendation,
                "latest_sales_status": latest_sales_status,
            }

        rows = [
            lead_row("historical-maybe", "maybe", "high", now),
            lead_row("historical-reject", "reject", "high", now),
            lead_row("go-medium-newer", "go", "medium", now),
            lead_row("go-high-older", "go", {"priority": "high"}, now - timedelta(hours=1)),
            lead_row("technical-go", "go", "high", now, analysis_type="llm_presales_report"),
            lead_row("missing-llm-rec", "", "high", now, rule_recommendation="go"),
            lead_row("in-work", "go", "high", now, latest_sales_status="sales_in_work"),
            lead_row("not-relevant", "go", "high", now, latest_sales_status="sales_not_relevant"),
            lead_row("legacy-skip", "go", "high", now, latest_sales_status="skip"),
        ]

        selected, stats = self.digest.split_operational_lead_queue_rows(rows, limit=10)

        self.assertEqual(
            [item["tender_id"] for item in selected],
            ["historical-maybe", "go-high-older", "go-medium-newer"],
        )
        self.assertEqual(stats.shown, 3)
        self.assertEqual(stats.hidden_in_work, 1)
        self.assertEqual(stats.hidden_negative_feedback, 2)

    def test_operational_lead_queue_uses_latest_duplicate_external_id_report(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        older_high = lead_report_row(
            "older-high",
            priority="high",
            report_recommendation="go",
            created_at=now - timedelta(hours=1),
        )
        newer_low = lead_report_row(
            "newer-low",
            priority="low",
            report_recommendation="go",
            created_at=now,
        )
        older_high["external_id"] = "same-external"
        newer_low["external_id"] = "same-external"

        selected, stats = self.digest.split_operational_lead_queue_rows(
            [older_high, newer_low],
            limit=10,
        )

        self.assertEqual([item["tender_id"] for item in selected], ["newer-low"])
        self.assertEqual(stats.shown, 1)

    def test_operational_lead_duplicate_latest_no_go_hides_older_go(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        older_go = lead_report_row(
            "older-go",
            report_recommendation="go",
            created_at=now - timedelta(hours=1),
        )
        newer_no_go = lead_report_row(
            "newer-no-go",
            report_recommendation="no_go",
            created_at=now,
        )
        older_go["external_id"] = "same-external"
        newer_no_go["external_id"] = "same-external"

        selected, stats = self.digest.split_operational_lead_queue_rows(
            [older_go, newer_no_go],
            limit=10,
        )

        self.assertEqual(selected, [])
        self.assertEqual(stats.shown, 0)

    def test_operational_lead_queue_uses_latest_go_triage_as_gate(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            lead_report_row(
                "triage-go-report-maybe",
                report_recommendation="maybe",
                triage_decision="go",
                created_at=now,
            ),
            lead_report_row(
                "triage-maybe-report-maybe",
                report_recommendation="maybe",
                triage_decision="maybe",
                created_at=now,
            ),
            lead_report_row(
                "triage-reject-report-go",
                report_recommendation="go",
                triage_decision="reject",
                created_at=now,
                triage_created_at=now + timedelta(minutes=1),
            ),
            lead_report_row(
                "triage-go-report-before-triage",
                report_recommendation="maybe",
                triage_decision="go",
                created_at=now,
                triage_created_at=now + timedelta(minutes=1),
            ),
            lead_report_row(
                "legacy-report-go",
                report_recommendation="go",
                created_at=now - timedelta(hours=1),
            ),
            lead_report_row("legacy-report-maybe", report_recommendation="maybe", created_at=now),
        ]

        selected, stats = self.digest.split_operational_lead_queue_rows(rows, limit=10)

        self.assertEqual(
            [item["tender_id"] for item in selected],
            [
                "legacy-report-maybe",
                "triage-go-report-maybe",
                "triage-maybe-report-maybe",
                "legacy-report-go",
            ],
        )
        self.assertEqual(stats.shown, 4)
        self.assertEqual(
            self.digest.operational_queue_decision_from_row(rows[0]),
            "go",
        )
        self.assertEqual(self.digest.operational_queue_decision_from_row(rows[1]), "maybe")
        self.assertIsNone(self.digest.operational_queue_decision_from_row(rows[2]))
        self.assertIsNone(self.digest.operational_queue_decision_from_row(rows[3]))
        self.assertEqual(
            self.digest.operational_queue_decision_from_row(rows[4]),
            "go",
        )

    def test_operational_lead_full_report_newer_than_triage_reject_stays_visible(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        row = lead_report_row(
            "newer-full-report",
            report_recommendation="go",
            triage_decision="reject",
            triage_created_at=now - timedelta(hours=1),
            created_at=now,
        )

        selected, stats = self.digest.split_operational_lead_queue_rows([row], limit=10)

        self.assertEqual([item["tender_id"] for item in selected], ["newer-full-report"])
        self.assertEqual(stats.hidden_by_lead_triage_reject, 0)
        self.assertEqual(self.digest.operational_queue_decision_from_row(row), "go")

    def test_operational_lead_today_all_and_in_work_use_product_queues(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        today = datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc)
        yesterday_moscow = datetime(2026, 5, 4, 20, 59, tzinfo=timezone.utc)

        triage_today = lead_report_row(
            "triage-go-report-maybe-today",
            report_recommendation="maybe",
            triage_decision="go",
            created_at=today,
        )
        triage_yesterday = lead_report_row(
            "triage-go-report-maybe-yesterday",
            report_recommendation="maybe",
            triage_decision="go",
            created_at=yesterday_moscow,
        )
        triage_maybe = lead_report_row(
            "triage-maybe",
            report_recommendation="maybe",
            triage_decision="maybe",
            created_at=today,
        )
        triage_reject = lead_report_row(
            "triage-reject",
            report_recommendation="go",
            triage_decision="reject",
            created_at=today,
            triage_created_at=today + timedelta(minutes=1),
        )
        in_work = lead_report_row(
            "sales-in-work",
            report_recommendation="maybe",
            triage_decision="go",
            latest_sales_status=self.digest.SALES_IN_WORK_STATUS,
            created_at=today,
        )
        negative = lead_report_row(
            "sales-not-relevant",
            report_recommendation="maybe",
            triage_decision="go",
            latest_sales_status=self.digest.SALES_NOT_RELEVANT_STATUS,
            created_at=today,
        )
        legacy_today = lead_report_row(
            "legacy-report-go-today",
            report_recommendation="go",
            created_at=today + timedelta(minutes=10),
        )
        legacy_yesterday = lead_report_row(
            "legacy-report-go-yesterday",
            report_recommendation="go",
            created_at=yesterday_moscow,
        )
        rows = [
            triage_today,
            triage_yesterday,
            triage_maybe,
            triage_reject,
            in_work,
            negative,
            legacy_today,
            legacy_yesterday,
        ]

        with patch.object(
            self.digest,
            "get_operational_lead_report_rows",
            return_value=rows,
        ) as getter:
            today_rows, today_stats = self.digest.select_operational_lead_queue_rows(
                limit=20,
                now=now,
            )

        getter.assert_called_once()
        self.assertIsNone(getter.call_args.kwargs["limit"])
        self.assertEqual(
            getattr(getter.call_args.kwargs["report_created_from"].tzinfo, "key", ""),
            "Europe/Moscow",
        )
        self.assertEqual(
            {item["tender_id"] for item in today_rows},
            {"triage-go-report-maybe-today", "triage-maybe", "legacy-report-go-today"},
        )
        self.assertEqual(today_stats.shown, 3)
        self.assertEqual(today_stats.hidden_in_work, 1)
        self.assertEqual(today_stats.hidden_negative_feedback, 1)

        with patch.object(self.digest, "get_operational_lead_report_rows", return_value=rows):
            all_rows, all_stats = self.digest.select_operational_lead_all_rows(
                limit=None,
                now=now,
            )

        self.assertEqual(
            {item["tender_id"] for item in all_rows},
            {
                "triage-go-report-maybe-today",
                "triage-go-report-maybe-yesterday",
                "triage-maybe",
                "legacy-report-go-today",
                "legacy-report-go-yesterday",
            },
        )
        self.assertEqual(all_stats.shown, 5)
        self.assertEqual(all_stats.new_count, 5)
        self.assertEqual(all_stats.hidden_in_work, 1)
        self.assertEqual(all_stats.hidden_negative_feedback, 1)

        with patch.object(
            self.digest,
            "get_operational_lead_report_in_work_rows",
            return_value=[triage_today, in_work, negative],
        ):
            in_work_rows, in_work_stats = self.digest.select_operational_lead_in_work_rows(
                limit=20,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in in_work_rows], ["sales-in-work"])
        self.assertEqual(in_work_stats.shown, 1)

    def test_operational_lead_queue_sales_and_negative_feedback_rules_stay_active(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        visible = lead_report_row(
            "triage-go-visible",
            report_recommendation="maybe",
            triage_decision="go",
            created_at=now,
        )
        in_work = lead_report_row(
            "triage-go-in-work",
            report_recommendation="maybe",
            triage_decision="go",
            latest_sales_status=self.digest.SALES_IN_WORK_STATUS,
            created_at=now,
        )
        negative = lead_report_row(
            "triage-go-negative",
            report_recommendation="maybe",
            triage_decision="go",
            latest_sales_status=self.digest.SALES_NOT_RELEVANT_STATUS,
            created_at=now,
        )

        queue_rows, queue_stats = self.digest.split_operational_lead_queue_rows(
            [visible, in_work, negative],
            limit=10,
        )
        all_rows, all_stats = self.digest.split_operational_lead_all_rows(
            [visible, in_work, negative],
            limit=10,
        )

        self.assertEqual([item["tender_id"] for item in queue_rows], ["triage-go-visible"])
        self.assertEqual([item["tender_id"] for item in all_rows], ["triage-go-visible"])
        self.assertEqual(queue_stats.hidden_in_work, 1)
        self.assertEqual(queue_stats.hidden_negative_feedback, 1)
        self.assertEqual(all_stats.hidden_in_work, 1)
        self.assertEqual(all_stats.hidden_negative_feedback, 1)

        with patch.object(
            self.digest,
            "get_operational_lead_report_in_work_rows",
            return_value=[visible, in_work, negative],
        ):
            selected, stats = self.digest.select_operational_lead_in_work_rows(limit=10)

        self.assertEqual([item["tender_id"] for item in selected], ["triage-go-in-work"])
        self.assertEqual(stats.shown, 1)

    def test_operational_lead_queues_hide_current_hard_noise(self) -> None:
        visible = lead_report_row("visible")
        noise = lead_report_row("hard-noise")
        noise["title"] = "Kaspersky antivirus license renewal"

        queue_rows, queue_stats = self.digest.split_operational_lead_queue_rows(
            [noise, visible],
            limit=10,
        )
        all_rows, all_stats = self.digest.split_operational_lead_all_rows(
            [noise, visible],
            limit=None,
        )

        self.assertEqual([row["tender_id"] for row in queue_rows], ["visible"])
        self.assertEqual([row["tender_id"] for row in all_rows], ["visible"])
        self.assertEqual(queue_stats.hidden_by_lead_hard_noise, 1)
        self.assertEqual(all_stats.hidden_by_lead_hard_noise, 1)

    def test_operational_lead_all_rows_hide_in_work_and_negative_feedback(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            lead_report_row("new", priority="medium", created_at=now),
            lead_report_row("in-work", priority="high", latest_sales_status="sales_in_work", created_at=now),
            lead_report_row("not-relevant", latest_sales_status="sales_not_relevant", created_at=now),
            lead_report_row("skip", latest_sales_status="skip", created_at=now),
            {
                **lead_report_row("technical", created_at=now),
                "llm_report_analysis_type": "llm_presales_report",
            },
        ]

        selected, stats = self.digest.split_operational_lead_all_rows(rows, limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["new"])
        self.assertEqual(stats.shown, 1)
        self.assertEqual(stats.new_count, 1)
        self.assertEqual(stats.in_work_count, 0)
        self.assertEqual(stats.hidden_in_work, 1)
        self.assertEqual(stats.hidden_negative_feedback, 2)

    def test_operational_lead_rows_hide_existing_client_customer_marker(self) -> None:
        visible = lead_report_row("new")
        same_customer = lead_report_row("same-customer-new")
        same_customer["existing_client_customer_status"] = "sales_existing_client"
        same_customer["existing_client_source_tender_id"] = "source-tender"
        returned_override = lead_report_row(
            "returned-same-customer",
            latest_sales_status="sales_returned",
        )
        returned_override["existing_client_customer_status"] = "sales_existing_client"
        returned_override["existing_client_source_tender_id"] = "source-tender"

        selected, stats = self.digest.split_operational_lead_all_rows(
            [same_customer, visible, returned_override],
            limit=None,
        )

        self.assertEqual(
            [item["tender_id"] for item in selected],
            ["new", "returned-same-customer"],
        )
        self.assertEqual(stats.hidden_negative_feedback, 1)

    def test_operational_lead_all_keeps_fresh_maybe_lead_report_without_feedback(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            lead_report_row(
                "95901843",
                priority="medium",
                report_recommendation="maybe",
                created_at=now,
            )
        ]

        selected, stats = self.digest.split_operational_lead_all_rows(rows, limit=None)

        self.assertEqual([item["tender_id"] for item in selected], ["95901843"])
        self.assertEqual(stats.shown, 1)
        self.assertEqual(stats.hidden_negative_feedback, 0)

    def test_operational_lead_selection_reasons_name_hidden_cases(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            lead_report_row("shown", created_at=now),
            lead_report_row(
                "feedback",
                latest_sales_status=self.digest.SALES_NOT_RELEVANT_STATUS,
                created_at=now,
            ),
            {
                **lead_report_row("status", created_at=now),
                "llm_report_analysis_type": "llm_presales_report",
            },
            lead_report_row(
                "triage-reject",
                triage_decision="reject",
                triage_created_at=now + timedelta(minutes=1),
                created_at=now,
            ),
            lead_report_row("limit", priority="low", created_at=now - timedelta(hours=1)),
        ]

        reasons = {
            row["tender_id"]: reason
            for row, reason in self.digest.operational_lead_all_selection_reasons(
                rows,
                limit=1,
            )
        }

        self.assertEqual(reasons["shown"], self.digest.DIGEST_REASON_SHOWN)
        self.assertEqual(reasons["feedback"], self.digest.HIDDEN_BY_FEEDBACK)
        self.assertEqual(reasons["status"], self.digest.HIDDEN_BY_STATUS)
        self.assertEqual(reasons["triage-reject"], self.digest.HIDDEN_BY_LEAD_TRIAGE_REJECT)
        self.assertEqual(reasons["limit"], self.digest.HIDDEN_BY_DIGEST_LIMIT)

    def test_operational_lead_report_query_uses_triage_gate_with_legacy_fallback(self) -> None:
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_operational_lead_report_rows(limit=10)

        self.assertEqual(rows, [])
        self.assertIn("lead_triage_decision", cursor.query)
        self.assertIn("triage.lead_decision = 'go'", cursor.query)
        self.assertIn("triage.lead_decision = 'maybe'", cursor.query)
        self.assertIn("created_at >= triage.created_at", cursor.query)
        self.assertIn("created_at > triage.created_at", cursor.query)
        self.assertIn("triage.created_at IS NULL", cursor.query)
        self.assertIn("lr_report.analysis_type = %s", cursor.query)
        self.assertIn("ORDER BY lr_report.created_at DESC", cursor.query)
        self.assertIn(
            "lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)",
            cursor.query,
        )
        self.assertIn("ELSE lower(replace(coalesce(lr.recommendation, ''), '-', '_'))", cursor.query)
        self.assertIn("result #>> '{report,lead_priority,priority}'", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "go",
                "maybe",
                "go",
                "maybe",
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                *self.digest.SALES_WORKFLOW_STATUSES,
                self.digest.SALES_EXISTING_CLIENT_STATUS,
                10,
            ),
        )

    def test_operational_lead_report_in_work_query_filters_sales_in_work(self) -> None:
        cursor = FakeCursor()

        with patch.object(self.digest.psycopg, "connect", return_value=FakeConnection(cursor)):
            rows = self.digest.get_operational_lead_report_in_work_rows(limit=10)

        self.assertEqual(rows, [])
        self.assertIn("lead_triage_decision", cursor.query)
        self.assertIn("triage.lead_decision = 'go'", cursor.query)
        self.assertIn("triage.lead_decision = 'maybe'", cursor.query)
        self.assertIn("created_at >= triage.created_at", cursor.query)
        self.assertIn("created_at > triage.created_at", cursor.query)
        self.assertIn("triage.created_at IS NULL", cursor.query)
        self.assertIn("lr_report.analysis_type = %s", cursor.query)
        self.assertIn("ORDER BY lr_report.created_at DESC", cursor.query)
        self.assertIn(
            "lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)",
            cursor.query,
        )
        self.assertIn("WHERE sales_feedback.status = %s", cursor.query)
        self.assertEqual(
            cursor.params,
            (
                self.digest.LEAD_TRIAGE_ANALYSIS_TYPE,
                self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "go",
                "maybe",
                "go",
                "maybe",
                *self.digest.SALES_FEEDBACK_LOOKUP_STATUSES,
                self.digest.SALES_IN_WORK_STATUS,
                10,
            ),
        )

    def test_operational_lead_in_work_rows_return_sales_in_work_only(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "tender_id": "in-work",
                "llm_report_analysis_type": self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "llm_report_recommendation": "go",
                "llm_report_result": {"report": {"lead_priority": "high"}},
                "llm_report_created_at": now,
                "latest_sales_status": self.digest.SALES_IN_WORK_STATUS,
            },
            {
                "tender_id": "not-relevant",
                "llm_report_analysis_type": self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "llm_report_recommendation": "go",
                "llm_report_result": {"report": {"lead_priority": "high"}},
                "llm_report_created_at": now,
                "latest_sales_status": self.digest.SALES_NOT_RELEVANT_STATUS,
            },
        ]

        with patch.object(self.digest, "get_operational_lead_report_in_work_rows", return_value=rows):
            selected, stats = self.digest.select_operational_lead_in_work_rows(limit=10)

        self.assertEqual([item["tender_id"] for item in selected], ["in-work"])
        self.assertEqual(stats.shown, 1)

    def test_sales_feedback_moves_operational_lead_between_queues(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

        def lead_with_status(status: str | None) -> dict:
            return {
                "tender_id": "lead",
                "llm_report_analysis_type": self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "llm_report_recommendation": "go",
                "llm_report_result": {"report": {"lead_priority": "high"}},
                "llm_report_created_at": now,
                "latest_sales_status": status,
            }

        visible, _ = self.digest.split_operational_lead_queue_rows(
            [lead_with_status(None)],
            limit=10,
        )
        all_visible, _ = self.digest.split_operational_lead_all_rows(
            [lead_with_status(None)],
            limit=10,
        )
        in_work_hidden, _ = self.digest.split_operational_lead_queue_rows(
            [lead_with_status(self.digest.SALES_IN_WORK_STATUS)],
            limit=10,
        )
        all_in_work_hidden, _ = self.digest.split_operational_lead_all_rows(
            [lead_with_status(self.digest.SALES_IN_WORK_STATUS)],
            limit=10,
        )
        negative_hidden, _ = self.digest.split_operational_lead_queue_rows(
            [lead_with_status(self.digest.SALES_NOT_RELEVANT_STATUS)],
            limit=10,
        )
        all_negative_hidden, _ = self.digest.split_operational_lead_all_rows(
            [lead_with_status(self.digest.SALES_NOT_RELEVANT_STATUS)],
            limit=10,
        )
        returned_visible, _ = self.digest.split_operational_lead_all_rows(
            [lead_with_status(self.digest.SALES_RETURNED_STATUS)],
            limit=10,
        )

        self.assertEqual([item["tender_id"] for item in visible], ["lead"])
        self.assertEqual([item["tender_id"] for item in all_visible], ["lead"])
        self.assertEqual(in_work_hidden, [])
        self.assertEqual(all_in_work_hidden, [])
        self.assertEqual(negative_hidden, [])
        self.assertEqual(all_negative_hidden, [])
        self.assertEqual([item["tender_id"] for item in returned_visible], ["lead"])

        with patch.object(
            self.digest,
            "get_operational_lead_report_in_work_rows",
            return_value=[lead_with_status(self.digest.SALES_IN_WORK_STATUS)],
        ):
            in_work_rows, _ = self.digest.select_operational_lead_in_work_rows(limit=10)
        with patch.object(
            self.digest,
            "get_operational_lead_report_in_work_rows",
            return_value=[lead_with_status(self.digest.SALES_NOT_RELEVANT_STATUS)],
        ):
            negative_in_work_rows, _ = self.digest.select_operational_lead_in_work_rows(limit=10)

        self.assertEqual([item["tender_id"] for item in in_work_rows], ["lead"])
        self.assertEqual(negative_in_work_rows, [])

    def test_operational_lead_queue_summary_uses_visible_lead_count(self) -> None:
        stats = self.digest.OperationalLeadQueueStats(
            total_found=5,
            shown=2,
            hidden_in_work=1,
            hidden_negative_feedback=1,
        )

        text = self.digest.operational_lead_queue_summary_text(stats)

        self.assertIn("AI Tender Radar - новые клиентские лиды сегодня", text)
        self.assertNotIn(FORBIDDEN_ROLE_WORD, text.lower())
        self.assertIn("Новых лидов сегодня: 2", text)
        self.assertIn('Скрыто как "в работе": 1', text)
        self.assertIn("Скрыто негативным feedback: 1", text)

    def test_caller_lead_card_contains_only_call_first_fields(self) -> None:
        tender = {
            "tender_id": "lead-card",
            "external_id": "lead-card",
            "title": "Поставка СХД для ЦОД",
            "customer_name": "ООО Заказчик",
            "initial_price": 12_500_000,
            "url": "https://example.test/lead-card",
            "raw": {
                "full": {
                    "contactPerson": "Иван Иванов",
                    "contactPhone": "+7 999 000-00-00",
                    "contactEMail": "buyer@example.test",
                }
            },
            "llm_report_analysis_type": self.digest.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_recommendation": "go",
            "llm_report_confidence": "high",
            "llm_report_created_at": datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
            "llm_report_result": {
                "report": {
                    "lead_summary": "Есть инфраструктурный сигнал по хранению данных.",
                    "customer_signal": "Заказчик обновляет СХД для ЦОД.",
                    "possible_needs": [
                        "расширение хранения данных",
                        "резервное копирование",
                        "сервисная поддержка",
                        "планирование обновления ЦОД",
                        "лишняя потребность",
                    ],
                    "target_end_customer_roles": ["ИТ-директор", "руководитель ЦОД"],
                    "target_roles": ["ИТ-директор", "руководитель инфраструктуры"],
                    "procurement_contact_role": "закупочный контакт как вход",
                    "opening_phrase": "Добрый день. Хотели познакомиться с ответственным за хранение данных.",
                    "discovery_questions": [
                        "Кто отвечает за развитие СХД?",
                        "Какие смежные задачи по резервному копированию есть?",
                        "Когда удобно передать контакт менеджеру?",
                        "Лишний вопрос",
                    ],
                    "lead_priority": "high",
                    "confidence": "high",
                    "next_action": "Позвонить закупочному контакту и попросить соединить с ИТ-ответственным.",
                    "supply_composition": ["СХД"],
                    "critical_tz_requirements": ["Техническое требование"],
                    "problems_in_tz": ["Проблема"],
                    "document_risk": {"level": "high"},
                }
            },
        }

        text = self.digest.format_caller_lead_card(tender, index=1)

        self.assertIn("AI Tender Radar - новые клиентские лиды сегодня", self.digest.operational_lead_queue_summary_text(self.digest.OperationalLeadQueueStats(total_found=1, shown=1)))
        self.assertIn("Рекомендация: передать в первичную обработку", text)
        self.assertIn("Приоритет: высокий", text)
        self.assertIn("Уверенность: высокая", text)
        self.assertIn("Документы:", text)
        self.assertIn("Срок подачи (справочно):", text)
        self.assertIn("📞 Первичный контакт", text)
        self.assertIn("Сигнал по заказчику:", text)
        self.assertIn("Возможные потребности:", text)
        self.assertIn("Кого искать:", text)
        self.assertIn("Первая фраза:", text)
        self.assertIn("Что выяснить:", text)
        self.assertIn("Следующее действие:", text)
        self.assertIn("Контакт закупки:", text)
        self.assertEqual(text.count("Лишний вопрос"), 0)
        self.assertEqual(text.count("лишняя потребность"), 0)
        for forbidden in (
            "Критичные требования ТЗ",
            "Проблемы и неясности ТЗ",
            "Состав поставки",
            "Документы обработаны",
            "Риск заточки",
            "document_risk",
            FORBIDDEN_ROLE_WORD,
        ):
            self.assertNotIn(forbidden, text)

    def test_caller_lead_card_shows_operational_decision_for_triage_go_report_maybe(self) -> None:
        tender = lead_report_row(
            "triage-go-report-maybe",
            report_recommendation="maybe",
            triage_decision="go",
        )

        text = self.digest.format_caller_lead_card(tender, index=1)

        self.assertIn(self.digest.call_first_recommendation_text("go"), text)
        self.assertNotIn(self.digest.call_first_recommendation_text("maybe"), text)

    def test_lead_cards_show_deadline_warning_without_hiding(self) -> None:
        expired_deadline = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
        caller_tender = lead_report_row("expired-caller-lead")
        caller_tender["deadline_at"] = expired_deadline
        working_tender = today_row("expired-working-lead", deadline_at=expired_deadline)

        caller_text = self.digest.format_caller_lead_card(caller_tender, index=1)
        working_text = self.digest.format_card(working_tender)

        self.assertIn(self.digest.DEADLINE_LEAD_WARNING_TEXT, caller_text)
        self.assertIn(self.digest.DEADLINE_LEAD_WARNING_TEXT, working_text)

    def test_recent_digest_sees_wrapped_customer_lead_report(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        tender = lead_report_row(
            "95901843",
            report_recommendation="maybe",
            created_at=now - timedelta(minutes=20),
        )
        tender["llm_report_result"]["report"]["document_status"] = {
            "code": "technical_spec_found",
        }

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=[tender]):
            cards = self.digest.build_recent_llm_digest_cards(24, limit=5, now=now)

        self.assertEqual([card.tender_id for card in cards], ["95901843"])
        self.assertIn(
            self.digest.document_status_label("technical_spec_found"),
            cards[0].message_text,
        )

    def test_recent_digest_supports_legacy_top_level_lead_report(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        tender = lead_report_row(
            "legacy-top-level",
            report_recommendation="",
            created_at=now - timedelta(minutes=20),
        )
        tender["llm_report_result"] = {
            "lead_summary": "customer storage refresh",
            "lead_priority": "high",
            "confidence": "high",
            "document_status": {"code": "technical_spec_found"},
        }

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=[tender]):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in selected], ["legacy-top-level"])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 0)
        self.assertEqual(self.digest.recent_llm_recommendation(tender), "go")

    def test_recent_mode_excludes_old_llm_reports_and_sorts_newest_first(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            row("fresh-older", now - timedelta(hours=1), "maybe"),
            row("old", now - timedelta(hours=3), "maybe"),
            row("fresh-newer", now - timedelta(minutes=10), "maybe"),
        ]

        with (
            patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows),
            patch.object(self.digest, "effective_recommendation", return_value="go"),
            patch.object(self.digest, "format_card", side_effect=lambda tender, index=None: tender["title"]),
        ):
            cards = self.digest.build_recent_llm_digest_cards(2, limit=5, now=now)

        self.assertEqual([card.tender_id for card in cards], ["fresh-newer", "fresh-older"])

    def test_recent_digest_uses_latest_operational_report_for_duplicate_external_id(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        older = lead_report_row(
            "older-report-row",
            report_recommendation="go",
            created_at=now - timedelta(hours=1),
        )
        newer = lead_report_row(
            "newer-report-row",
            report_recommendation="go",
            created_at=now - timedelta(minutes=5),
        )
        older["external_id"] = "same-external"
        newer["external_id"] = "same-external"

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=[older, newer]):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in selected], ["newer-report-row"])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 0)

    def test_recent_digest_duplicate_external_id_hides_latest_no_go(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        older_go = lead_report_row(
            "older-go",
            report_recommendation="go",
            created_at=now - timedelta(hours=1),
        )
        newer_no_go = lead_report_row(
            "newer-no-go",
            report_recommendation="no_go",
            created_at=now - timedelta(minutes=5),
        )
        older_go["external_id"] = "same-external"
        newer_no_go["external_id"] = "same-external"

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=[older_go, newer_no_go]):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual(selected, [])
        self.assertEqual(hidden_no_go, 1)
        self.assertEqual(hidden_deadline, 0)

    def test_recent_digest_duplicate_external_id_keeps_latest_feedback_semantics(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        older_go = lead_report_row(
            "older-go",
            report_recommendation="go",
            created_at=now - timedelta(hours=1),
        )
        newer_not_relevant = lead_report_row(
            "newer-not-relevant",
            report_recommendation="go",
            latest_sales_status=self.digest.SALES_NOT_RELEVANT_STATUS,
            created_at=now - timedelta(minutes=5),
        )
        older_go["external_id"] = "same-external"
        newer_not_relevant["external_id"] = "same-external"

        with patch.object(
            self.digest,
            "get_recent_llm_digest_rows",
            return_value=[older_go, newer_not_relevant],
        ):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual(selected, [])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 0)

    def test_recent_empty_message_is_clear(self) -> None:
        with patch.object(self.digest, "select_recent_llm_digest_rows", return_value=([], 0, 0)):
            messages = self.digest.build_recent_llm_digest_messages(24)

        self.assertEqual(
            messages,
            [
                "AI Tender Radar: свежих рабочих LLM-отчетов за последние 24 ч нет. "
                "Скрыто no_go: 0. С близким/истекшим сроком: 0."
            ],
        )

    def test_recent_header_names_mode_and_period(self) -> None:
        rows = [row("1", datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc))]

        with (
            patch.object(self.digest, "select_recent_llm_digest_rows", return_value=(rows, 0, 0)),
            patch.object(self.digest, "format_card", return_value="card"),
        ):
            messages = self.digest.build_recent_llm_digest_messages(24)

        self.assertIn("AI Tender Radar - свежие LLM-отчеты за последние 24 ч", messages[0])
        self.assertIn("Скрыто no_go: 0", messages[0])
        self.assertIn("С близким/истекшим сроком: 0", messages[0])
        self.assertEqual(messages[1], "card")

    def test_recent_digest_hides_llm_no_go_by_default_and_counts_hidden(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            row("visible", now - timedelta(minutes=20), "maybe"),
            row("hidden-no-go", now - timedelta(minutes=10), "no_go"),
        ]

        with (
            patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows),
            patch.object(self.digest, "format_card", side_effect=lambda tender, index=None: tender["title"]),
        ):
            messages = self.digest.build_recent_llm_digest_messages(24, now=now)

        self.assertIn("Скрыто no_go: 1", messages[0])
        self.assertEqual(messages[1:], ["visible"])

    def test_recent_digest_empty_when_only_no_go_are_hidden(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [row("hidden-no-go", now - timedelta(minutes=10), "no_go")]

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows):
            messages = self.digest.build_recent_llm_digest_messages(24, now=now)

        self.assertEqual(
            messages,
            [
                "AI Tender Radar: свежих рабочих LLM-отчетов за последние 24 ч нет. "
                "Скрыто no_go: 1. С близким/истекшим сроком: 0."
            ],
        )

    def test_recent_digest_keeps_deadline_risk_rows_and_counts_warning(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            row("fresh", now - timedelta(minutes=20), "maybe", deadline_at=now + timedelta(days=3)),
            row("too-soon-maybe", now - timedelta(minutes=10), "maybe", deadline_at=now + timedelta(days=2)),
            row("too-soon-go", now - timedelta(minutes=5), "go", deadline_at=now + timedelta(days=1)),
        ]

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in selected], ["too-soon-go", "too-soon-maybe", "fresh"])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 2)

    def test_recent_digest_hides_latest_sales_in_work(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [
            row("visible", now - timedelta(minutes=20), "maybe"),
            row(
                "in-work",
                now - timedelta(minutes=10),
                "maybe",
                latest_sales_status="sales_in_work",
            ),
        ]

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=5,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 0)

    def test_recent_digest_hides_negative_sales_feedback_statuses(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [row("visible", now - timedelta(minutes=20), "maybe")]
        negative_statuses = tuple(sorted(self.digest.NEGATIVE_SALES_FEEDBACK_STATUSES))
        rows.extend(
            row(status, now - timedelta(minutes=10), "maybe", latest_sales_status=status)
            for status in negative_statuses
        )

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows):
            selected, hidden_no_go, hidden_deadline = self.digest.select_recent_llm_digest_rows(
                24,
                limit=10,
                now=now,
            )

        self.assertEqual([item["tender_id"] for item in selected], ["visible"])
        self.assertEqual(hidden_no_go, 0)
        self.assertEqual(hidden_deadline, 0)

    def test_recent_digest_summary_shows_deadline_warning_count(self) -> None:
        with patch.object(self.digest, "select_recent_llm_digest_rows", return_value=([], 0, 2)):
            messages = self.digest.build_recent_llm_digest_messages(24)

        self.assertIn("С близким/истекшим сроком: 2", messages[0])

    def test_recent_digest_with_include_no_go_keeps_no_go(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [row("visible-no-go", now - timedelta(minutes=10), "no_go")]

        with (
            patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows),
            patch.object(self.digest, "format_card", side_effect=lambda tender, index=None: tender["title"]),
        ):
            messages = self.digest.build_recent_llm_digest_messages(24, include_no_go=True, now=now)

        self.assertEqual(messages[1:], ["visible-no-go"])
        self.assertIn("debug: включая no_go", messages[0])
        self.assertIn("Диагностический режим: это не рабочий digest", messages[0])

    def test_recent_include_no_go_card_uses_red_no_go_marking(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        rows = [row("visible-no-go", now - timedelta(minutes=10), "no_go")]

        with patch.object(self.digest, "get_recent_llm_digest_rows", return_value=rows):
            cards = self.digest.build_recent_llm_digest_cards(24, include_no_go=True, now=now)

        self.assertIn("🔴 visible-no-go", cards[0].message_text)
        self.assertIn("Рекомендация: не брать в работу без новой причины", cards[0].message_text)
        self.assertIn("📞 Для знакомства", cards[0].message_text)
        self.assertNotIn("Бизнес-действие: не тратить время", cards[0].message_text)
        self.assertNotIn("Бизнес-действие: быстрый ручной чек перед пресейлом", cards[0].message_text)


if __name__ == "__main__":
    unittest.main()
