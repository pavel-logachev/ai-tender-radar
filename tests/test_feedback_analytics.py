from __future__ import annotations

import contextlib
import io
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.pipeline import feedback_analytics


NOW = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


def row(
    status: str,
    *,
    external_id: str = "00000016",
    title: str = "Продление антивирусных лицензий",
    user_name: str = "test-manager",
    feedback_created_at: datetime = NOW,
    tender_id: str | None = None,
    rule_based_result: dict | None = None,
    preparation_blocked_event: str | None = None,
    preparation_requeue_event: str | None = None,
    lead_report_result: dict | None = None,
    lead_triage_result: dict | None = None,
    initial_price: int = 1500000,
    category: str | None = None,
) -> dict:
    data = {
        "feedback_id": f"fb-{external_id}-{status}",
        "feedback_created_at": feedback_created_at,
        "user_name": user_name,
        "status": status,
        "comment": None,
        "tender_id": tender_id or f"tender-{external_id}",
        "external_id": external_id,
        "title": title,
        "customer_name": "ООО Заказчик",
        "initial_price": initial_price,
        "deadline_at": datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        "url": f"https://example.org/procedure/{external_id}",
        "raw": {"full": {}},
        "tender_status": "scored",
        "rule_recommendation": "maybe",
        "rule_based_result": rule_based_result or {},
        "document_risk_result": None,
        "lead_report_recommendation": None,
        "lead_report_confidence": None,
        "lead_report_result": lead_report_result,
        "lead_triage_recommendation": None,
        "lead_triage_confidence": None,
        "lead_triage_result": lead_triage_result,
        "preparation_blocked_event": preparation_blocked_event,
        "preparation_requeue_event": preparation_requeue_event,
    }
    if category:
        data["category"] = category
    return data


class FeedbackAnalyticsTest(unittest.TestCase):
    def test_sales_feedback_statuses_are_grouped_with_labels(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row("sales_in_work", external_id="workflow-1"),
                row("sales_not_relevant", external_id="workflow-2"),
                row("sales_returned", external_id="workflow-3"),
                row("sales_existing_client", external_id="workflow-4"),
                row("interesting"),
                row("in_work", external_id="2"),
                row("skip", external_id="3"),
                row("not_profile", external_id="4"),
                row("bad_price", external_id="5"),
                row("service_only", external_id="6"),
                row("bad_timing", external_id="7"),
            ],
            now=NOW,
        )

        self.assertIn("| `sales_existing_client` |", report)
        self.assertIn("| `sales_in_work` | В работе | 1 |", report)
        self.assertIn("| `sales_not_relevant` | Мимо | 1 |", report)
        self.assertIn("| `sales_returned` | Возвращено в список | 1 |", report)
        self.assertIn("| `interesting` | Интересно | 1 |", report)
        self.assertIn("| `in_work` | В работу | 1 |", report)
        self.assertIn("| `skip` | Мимо | 1 |", report)
        self.assertIn("| `not_profile` | Не наш профиль | 1 |", report)
        self.assertIn("| `bad_price` | Маленький чек | 1 |", report)
        self.assertIn("| `service_only` | Услуги/ПО без инфраструктуры | 1 |", report)
        self.assertIn("| `bad_timing` | Поздно / не сейчас | 1 |", report)

    def test_sales_workflow_statuses_are_reported_separately(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row("sales_in_work", external_id="1"),
                row("sales_in_work", external_id="2"),
                row("sales_not_relevant", external_id="3"),
                row("sales_returned", external_id="4"),
                row("sales_existing_client", external_id="6"),
                row("filter_hidden_correct", external_id="5"),
            ],
            now=NOW,
        )

        self.assertIn("## Sales workflow", report)
        self.assertIn("| `sales_existing_client` |", report)
        self.assertIn("- Взято в работу: 2", report)
        self.assertIn("- Отмечено мимо: 1", report)
        self.assertIn("- Возвращено в список: 1", report)
        self.assertIn("### Последние 20 sales workflow events", report)
        self.assertIn("external_id: 1", report)
        self.assertNotIn("external_id: 5", report.split("### Последние 20 sales workflow events", 1)[1].split("## Filter feedback", 1)[0])

    def test_latest_sales_status_per_tender_uses_last_feedback(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "sales_not_relevant",
                    external_id="flip-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW,
                ),
                row(
                    "sales_in_work",
                    external_id="flip-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW + timedelta(minutes=5),
                ),
                row("sales_not_relevant", external_id="negative-1"),
            ],
            now=NOW,
        )

        latest_section = report.split("## Latest sales status per tender", 1)[1].split(
            "## Повторный feedback",
            1,
        )[0]
        self.assertIn("- Уникальных тендеров с sales feedback: 2", latest_section)
        self.assertIn("- Latest positive sales feedback: 1", latest_section)
        self.assertIn("- Latest negative sales feedback: 1", latest_section)
        self.assertIn("| `sales_in_work` | В работе | 1 |", latest_section)
        self.assertIn("| `sales_not_relevant` | Мимо | 1 |", latest_section)

    def test_latest_sales_status_uses_reject_reason_after_miss(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "sales_not_relevant",
                    external_id="reason-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW,
                ),
                row(
                    "service_only",
                    external_id="reason-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW + timedelta(minutes=1),
                ),
            ],
            now=NOW,
        )

        latest_section = report.split("## Latest sales status per tender", 1)[1].split(
            "## Повторный feedback",
            1,
        )[0]
        self.assertIn("- Latest negative sales feedback: 1", latest_section)
        self.assertIn("| `service_only` | Услуги/ПО без инфраструктуры | 1 |", latest_section)
        self.assertNotIn("| `sales_not_relevant` | Мимо | 1 |", latest_section)

    def test_repeated_feedback_counts_status_flips(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "sales_not_relevant",
                    external_id="flip-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW,
                ),
                row(
                    "sales_in_work",
                    external_id="flip-1",
                    tender_id="same-tender",
                    feedback_created_at=NOW + timedelta(minutes=5),
                ),
                row(
                    "sales_in_work",
                    external_id="repeat-1",
                    tender_id="repeat-same-status",
                    feedback_created_at=NOW,
                ),
                row(
                    "sales_in_work",
                    external_id="repeat-1",
                    tender_id="repeat-same-status",
                    feedback_created_at=NOW + timedelta(minutes=5),
                ),
            ],
            now=NOW,
        )

        self.assertIn("- Тендеров с повторным sales feedback: 2", report)
        self.assertIn("- Тендеров со сменой sales status: 1", report)
        self.assertIn("- Смена negative -> positive: 1", report)
        self.assertIn("- Смена positive -> negative: 0", report)

    def test_quality_sections_include_category_price_and_llm_signals(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "sales_in_work",
                    external_id="storage-positive",
                    category="storage",
                    initial_price=60_000_000,
                    lead_report_result={
                        "report": {
                            "lead_priority": "high",
                            "confidence": "high",
                        }
                    },
                    lead_triage_result={
                        "report": {
                            "lead_decision": "go",
                            "lead_priority": "high",
                        }
                    },
                ),
                row(
                    "sales_not_relevant",
                    external_id="server-negative",
                    category="servers",
                    initial_price=2_000_000,
                    lead_report_result={
                        "report": {
                            "lead_priority": "medium",
                            "confidence": "low",
                        }
                    },
                    lead_triage_result={
                        "report": {
                            "lead_decision": "go",
                            "lead_priority": "medium",
                        }
                    },
                ),
            ],
            now=NOW,
        )

        self.assertIn("## Quality by category", report)
        self.assertIn("| `storage` | 1 | 1 | 0 | 100.0% |", report)
        self.assertIn("| `servers` | 1 | 0 | 1 | 0.0% |", report)
        self.assertIn("## Quality by price bucket", report)
        self.assertIn("| `50M+` | 1 | 1 | 0 | 100.0% |", report)
        self.assertIn("| `<3M` | 1 | 0 | 1 | 0.0% |", report)
        self.assertIn("### Lead report priority", report)
        self.assertIn("| `high` | 1 | 1 | 0 | 100.0% |", report)
        self.assertIn("| `medium` | 1 | 0 | 1 | 0.0% |", report)
        self.assertIn("### Lead triage decision", report)
        self.assertIn("| `go` | 2 | 1 | 1 | 50.0% |", report)

    def test_filter_feedback_statuses_are_grouped_with_labels(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row("filter_hidden_correct"),
                row("filter_hidden_wrong", external_id="2"),
                row("filter_docs_correct", external_id="3"),
                row("filter_docs_wrong", external_id="4"),
            ],
            now=NOW,
        )

        self.assertIn("| `filter_hidden_correct` | Фильтр верно скрыл | 1 |", report)
        self.assertIn("| `filter_hidden_wrong` | Фильтр ошибочно скрыл | 1 |", report)
        self.assertIn(
            '| `filter_docs_correct` | Верно отправлено в "Нужны документы" | 1 |',
            report,
        )
        self.assertIn(
            '| `filter_docs_wrong` | Ошибочно отправлено в "Нужны документы" | 1 |',
            report,
        )

    def test_filter_hidden_wrong_goes_to_filter_wrong_section(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "filter_hidden_wrong",
                    external_id="hidden-wrong",
                    title="Скрытая карточка, которую надо было показать",
                    rule_based_result={"skip_reason": "service_noise"},
                ),
            ],
            now=NOW,
        )

        section = report.split('### "Фильтр ошибся"', 1)[1].split(
            '### "Фильтр сработал верно"',
            1,
        )[0]
        self.assertIn("hidden-wrong", section)
        self.assertIn("Скрытая карточка, которую надо было показать", section)
        self.assertIn("service_noise", section)

    def test_filter_docs_wrong_goes_to_filter_wrong_section(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [
                row(
                    "filter_docs_wrong",
                    external_id="docs-wrong",
                    title="Карточка ошибочно отправлена в ручные документы",
                    preparation_blocked_event='{"status": "no_valid_documents"}',
                ),
            ],
            now=NOW,
        )

        section = report.split('### "Фильтр ошибся"', 1)[1].split(
            '### "Фильтр сработал верно"',
            1,
        )[0]
        self.assertIn("docs-wrong", section)
        self.assertIn("Карточка ошибочно отправлена в ручные документы", section)
        self.assertIn("preparation_no_valid_documents", section)

    def test_output_path_saves_same_markdown(self) -> None:
        tmpdir = Path.cwd() / "tmp_feedback_analytics_test"
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            output_path = tmpdir / "nested" / "feedback.md"
            stdout = io.StringIO()

            with (
                patch.object(feedback_analytics, "fetch_feedback_rows", return_value=[row("interesting")]),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = feedback_analytics.main(
                    ["--days", "7", "--output-path", str(output_path)]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), stdout.getvalue())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_empty_feedback_has_clear_empty_state(self) -> None:
        report = feedback_analytics.build_feedback_report([], now=NOW)

        self.assertIn("# Feedback analytics за последние 7 дней", report)
        self.assertIn("- Всего feedback-событий: 0", report)
        self.assertIn("За период feedback-событий не найдено.", report)
        self.assertIn("Sales feedback", report)
        self.assertIn("Filter feedback", report)

    def test_unknown_status_is_rendered_as_unknown_raw(self) -> None:
        report = feedback_analytics.build_feedback_report(
            [row("legacy_custom_status")],
            now=NOW,
        )

        self.assertIn(
            "| `legacy_custom_status` | unknown/raw: legacy_custom_status | 1 |",
            report,
        )
        self.assertIn("## Unknown/raw statuses", report)
        self.assertIn("- `legacy_custom_status`: 1", report)


if __name__ == "__main__":
    unittest.main()
