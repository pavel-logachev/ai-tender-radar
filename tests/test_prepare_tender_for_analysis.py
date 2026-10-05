from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.pipeline.prepare_tender_for_analysis import (
    DOCUMENTS_DOWNLOADED_WITHOUT_TEXT,
    DOCUMENTS_MISSING,
    DOCUMENTS_READY_WITH_TEXT,
    DocumentCounts,
    PreparationStatus,
    PRIMARY_TECHNICAL_DOCUMENT_MISSING,
    PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
    PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT,
    StepOutcome,
    TARGETED_REQUEUE_BACKOFF_SECONDS,
    TARGETED_REQUEUE_MAX_ATTEMPTS,
    TARGETED_PREPARATION_BLOCKED_EVENT,
    TargetedDownloadRetryState,
    build_preparation_summary,
    determine_preparation_status,
    document_failure_items_from_events,
    document_readiness_state,
    extract_text_for_tender,
    is_partial_preparation,
    load_downloaded_documents,
    primary_technical_api_documents,
    primary_technical_document_status,
    prepare_tender_for_analysis,
)


class FakeDownloadReport:
    def __init__(
        self,
        *,
        total_saved: int,
        documents_found: int,
        documents_selected: int,
        stopped_due_to_rate_limit: bool,
        skipped_due_to_rate_limit: int = 0,
    ) -> None:
        self.total_saved = total_saved
        self.documents_found = documents_found
        self.documents_selected = documents_selected
        self.stopped_due_to_rate_limit = stopped_due_to_rate_limit
        self.skipped_due_to_rate_limit = skipped_due_to_rate_limit

    def to_dict(self) -> dict:
        return {
            "total_saved": self.total_saved,
            "documents_found": self.documents_found,
            "documents_selected": self.documents_selected,
            "documents_downloaded": self.total_saved,
            "documents_skipped_due_to_rate_limit": self.skipped_due_to_rate_limit,
            "documents_skipped_due_to_rate_limit_items": [],
            "rate_limit_retries": 1,
            "rate_limit_backoffs_seconds": [5.0],
            "stopped_due_to_rate_limit": self.stopped_due_to_rate_limit,
        }


class PrepareTenderForAnalysisTest(unittest.TestCase):
    def test_document_failure_items_from_events_extracts_failed_downloads(self) -> None:
        items = document_failure_items_from_events(
            [
                {
                    "event_type": "document_download_failed",
                    "message": (
                        "Document download returned empty response: "
                        "external_id=00000034, document_id=000000109, "
                        "status_code=200, reason=empty_response"
                    ),
                },
                {
                    "event_type": "document_download_rejected",
                    "message": (
                        "Rejected document download: external_id=00000034, "
                        "document_id=000000110, filename='bad.html', "
                        "detected_type='html', reason=html_response"
                    ),
                },
            ]
        )

        self.assertEqual(
            items,
            [
                {
                    "event_type": "document_download_failed",
                    "document_id": "000000109",
                    "reason": "empty_response",
                },
                {
                    "event_type": "document_download_rejected",
                    "document_id": "000000110",
                    "reason": "html_response",
                },
            ],
        )

    def test_object_description_title_is_primary_technical_doc_without_category(
        self,
    ) -> None:
        tender = {
            "id": "tender-1",
            "external_id": "00000021",
            "title": "Поставка системы иммерсивного звука",
        }
        docs = [
            {
                "id": "000000105",
                "title": "Приложение 1 Описание Объекта закупки иммерсивный звук",
            }
        ]

        primary_docs = primary_technical_api_documents(tender, docs)

        self.assertEqual(len(primary_docs), 1)
        self.assertEqual(primary_docs[0]["id"], "000000105")

    def test_load_downloaded_documents_uses_existing_schema_without_document_url(
        self,
    ) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.sql = ""
                self.params: tuple[str, ...] | None = None

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def execute(self, sql: str, params: tuple[str, ...]) -> None:
                self.sql = sql
                self.params = params

            def fetchall(self) -> list[dict[str, object]]:
                self.assert_current_query()
                return [
                    {
                        "id": "doc-1",
                        "filename": "000000105_Описание объекта закупки.docx",
                        "document_url": None,
                        "storage_path": "data/documents/000000105_Описание объекта закупки.docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "size_bytes": 42,
                        "extracted_text": "technical text",
                        "created_at": datetime(2026, 5, 8, 12, 0, 0),
                    }
                ]

            def assert_current_query(self) -> None:
                self_test.assertIn("NULL AS document_url", self.sql)
                self_test.assertNotIn("\n                    document_url,", self.sql)
                self_test.assertEqual(self.params, ("tender-1",))

        class FakeConnection:
            def __init__(self, cursor: FakeCursor) -> None:
                self._cursor = cursor

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def cursor(self, **_: object) -> FakeCursor:
                return self._cursor

        self_test = self
        cursor = FakeCursor()
        psycopg_module = types.ModuleType("psycopg")
        psycopg_rows_module = types.ModuleType("psycopg.rows")
        psycopg_rows_module.dict_row = object()
        psycopg_module.rows = psycopg_rows_module

        with (
            patch.dict(
                sys.modules,
                {"psycopg": psycopg_module, "psycopg.rows": psycopg_rows_module},
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.db_connect",
                return_value=FakeConnection(cursor),
            ),
        ):
            rows = load_downloaded_documents("tender-1")

        self.assertEqual(rows[0]["document_url"], None)
        self.assertEqual(rows[0]["filename"], "000000105_Описание объекта закупки.docx")

    def test_marketplace_auth_status_persists_blocked_event(self) -> None:
        started_at = datetime(2026, 5, 5, 10, 0, 0)
        docs = [{"id": "doc-1", "title": "\u0422\u0417"}]
        tender = {
            "id": "tender-1",
            "external_id": "00000015",
            "title": "Поставка оборудования",
        }
        recorded_events: list[tuple[str, str, dict]] = []
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: docs
        collector.record_processing_event = (
            lambda tender_id, event_type, message: recorded_events.append(
                (tender_id, event_type, json.loads(message))
            )
        )
        rejected_events = [
            {
                "event_type": "document_download_rejected",
                "message": (
                    "Rejected document download: "
                    "html_hint='external_marketplace_auth_required', "
                    "html_title='Корпоративная торговая секция ПАО «Учебный заказчик» на ЭТП «Учебная площадка»'"
                ),
                "created_at": started_at,
            }
        ]

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch("app.pipeline.prepare_tender_for_analysis.db_now", return_value=started_at),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                side_effect=[[], []],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_targeted_download_retry_state",
                return_value=TargetedDownloadRetryState(),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_targeted_document_download",
                return_value=(
                    FakeDownloadReport(
                        total_saved=0,
                        documents_found=1,
                        documents_selected=1,
                        stopped_due_to_rate_limit=False,
                    ),
                    ["marketplace auth"],
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                return_value=StepOutcome(attempted=False, succeeded=False),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                return_value=(StepOutcome(attempted=False, succeeded=False), None),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=rejected_events,
            ),
        ):
            result = prepare_tender_for_analysis(external_id="00000015", limit_docs=1)

        self.assertEqual(result.status, PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH)
        self.assertEqual(len(recorded_events), 1)
        tender_id, event_type, payload = recorded_events[0]
        self.assertEqual(tender_id, "tender-1")
        self.assertEqual(event_type, TARGETED_PREPARATION_BLOCKED_EVENT)
        self.assertEqual(payload["external_id"], "00000015")
        self.assertEqual(payload["status"], "blocked_by_marketplace_auth")
        self.assertEqual(payload["reason"], "marketplace_auth")
        self.assertEqual(payload["documents_found"], 1)
        self.assertEqual(payload["documents_downloaded"], 0)
        self.assertEqual(payload["documents_with_text"], 0)
        self.assertEqual(payload["html_hint"], "external_marketplace_auth_required")
        self.assertIn("Учебный заказчик", payload["html_title"])
        self.assertIn("cooldown_until", payload)

    def test_no_valid_documents_status_persists_blocked_event(self) -> None:
        started_at = datetime(2026, 5, 5, 10, 0, 0)
        tender = {
            "id": "tender-1",
            "external_id": "00000013",
            "title": "Система хранения данных",
        }
        recorded_events: list[tuple[str, str, dict]] = []
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: []
        collector.record_processing_event = (
            lambda tender_id, event_type, message: recorded_events.append(
                (tender_id, event_type, json.loads(message))
            )
        )

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch("app.pipeline.prepare_tender_for_analysis.db_now", return_value=started_at),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                return_value=[],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                return_value=StepOutcome(attempted=False, succeeded=False),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                return_value=(StepOutcome(attempted=False, succeeded=False), None),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=[],
            ),
        ):
            result = prepare_tender_for_analysis(external_id="00000013", limit_docs=1)

        self.assertEqual(result.status, PreparationStatus.NO_VALID_DOCUMENTS)
        self.assertEqual(len(recorded_events), 1)
        tender_id, event_type, payload = recorded_events[0]
        self.assertEqual(tender_id, "tender-1")
        self.assertEqual(event_type, TARGETED_PREPARATION_BLOCKED_EVENT)
        self.assertEqual(payload["external_id"], "00000013")
        self.assertEqual(payload["status"], "no_valid_documents")
        self.assertEqual(payload["reason"], "no_valid_documents")
        self.assertEqual(payload["documents_found"], 0)
        self.assertEqual(payload["documents_downloaded"], 0)
        self.assertEqual(payload["documents_with_text"], 0)
        self.assertIn("cooldown_until", payload)

    def test_marketplace_auth_wins_over_other_ready_signals(self) -> None:
        status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=3,
                downloaded=2,
                rejected=1,
                with_extracted_text=2,
            ),
            marketplace_auth_blocked=True,
            document_risk_ready=True,
        )

        self.assertEqual(status, PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH)

    def test_downloaded_docs_without_text_are_not_ready(self) -> None:
        status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=2,
                downloaded=2,
                rejected=0,
                with_extracted_text=0,
            ),
            marketplace_auth_blocked=False,
            document_risk_ready=False,
        )

        self.assertEqual(status, PreparationStatus.DOCUMENTS_DOWNLOADED_BUT_NO_TEXT)

    def test_rate_limited_docs_without_text_stay_partial(self) -> None:
        status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=2,
                downloaded=1,
                rejected=0,
                with_extracted_text=0,
            ),
            marketplace_auth_blocked=False,
            document_risk_ready=False,
            download_failed=True,
        )

        self.assertEqual(status, PreparationStatus.PARTIAL_PREPARATION)

    def test_existing_downloads_can_be_ready_even_without_api_inventory(self) -> None:
        status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=0,
                downloaded=1,
                rejected=0,
                with_extracted_text=1,
            ),
            marketplace_auth_blocked=False,
            document_risk_ready=True,
        )

        self.assertEqual(status, PreparationStatus.READY_FOR_LLM)

    def test_partial_flag_catches_limited_or_rejected_downloads(self) -> None:
        self.assertTrue(
            is_partial_preparation(
                status=PreparationStatus.READY_FOR_LLM,
                document_counts=DocumentCounts(
                    found=5,
                    downloaded=3,
                    rejected=1,
                    with_extracted_text=3,
                ),
                errors=[],
            )
        )

    def test_summary_for_ready_status_mentions_llm_step(self) -> None:
        summary = build_preparation_summary(
            PreparationStatus.READY_FOR_LLM,
            document_counts=DocumentCounts(
                found=1,
                downloaded=1,
                rejected=0,
                with_extracted_text=1,
            ),
            document_risk_ready=True,
        )

        self.assertIn("LLM", summary)

    def test_summary_includes_rate_limit_download_diagnostics(self) -> None:
        summary = build_preparation_summary(
            PreparationStatus.READY_FOR_LLM,
            document_counts=DocumentCounts(
                found=4,
                downloaded=1,
                rejected=0,
                with_extracted_text=1,
            ),
            document_risk_ready=True,
            download_details={
                "documents_selected": 4,
                "documents_skipped_due_to_rate_limit": 3,
                "documents_skipped_due_to_rate_limit_items": [
                    {
                        "document_id": "doc-2",
                        "title": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430",
                    }
                ],
            },
        )

        self.assertIn("documents_found=4", summary)
        self.assertIn("documents_selected=4", summary)
        self.assertIn("documents_downloaded=1", summary)
        self.assertIn("documents_skipped_due_to_rate_limit=3", summary)
        self.assertIn("doc-2", summary)

    def test_document_readiness_state_names_pipeline_states(self) -> None:
        self.assertEqual(
            document_readiness_state(DocumentCounts(downloaded=0, with_extracted_text=0)),
            DOCUMENTS_MISSING,
        )
        self.assertEqual(
            document_readiness_state(DocumentCounts(downloaded=2, with_extracted_text=0)),
            DOCUMENTS_DOWNLOADED_WITHOUT_TEXT,
        )
        self.assertEqual(
            document_readiness_state(DocumentCounts(downloaded=2, with_extracted_text=1)),
            DOCUMENTS_READY_WITH_TEXT,
        )

    def test_primary_technical_doc_missing_keeps_tender_not_ready(self) -> None:
        tender = {
            "id": "tender-1",
            "external_id": "00000021",
            "title": "Поставка серверного оборудования",
            "raw": {
                "full": {
                    "documents": [
                        {
                            "id": "000000105",
                            "title": "Приложение 1 Описание Объекта закупки.docx",
                        },
                        {"id": "000000106", "title": "Проект контракта.docx"},
                    ]
                }
            },
        }
        downloaded = [
            {
                "id": "local-contract",
                "filename": "000000106_Проект контракта.docx",
                "extracted_text": "contract text",
            }
        ]

        status = primary_technical_document_status(tender, downloaded)
        preparation_status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=2,
                downloaded=1,
                with_extracted_text=1,
            ),
            marketplace_auth_blocked=False,
            document_risk_ready=True,
            primary_technical_required=status["required"],
            primary_technical_ready=status["ready"],
        )

        self.assertTrue(status["required"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["reason"], PRIMARY_TECHNICAL_DOCUMENT_MISSING)
        self.assertEqual(preparation_status, PreparationStatus.PARTIAL_PREPARATION)

    def test_primary_technical_doc_ready_after_extraction_allows_llm(self) -> None:
        tender = {
            "id": "tender-1",
            "external_id": "00000021",
            "title": "Поставка серверного оборудования",
            "raw": {
                "full": {
                    "documents": [
                        {
                            "id": "000000105",
                            "title": "Приложение 1 Описание Объекта закупки.docx",
                        }
                    ]
                }
            },
        }
        downloaded = [
            {
                "id": "local-ooz",
                "filename": "000000105_Приложение 1 Описание Объекта закупки.docx",
                "extracted_text": "technical text",
            }
        ]

        status = primary_technical_document_status(tender, downloaded)
        preparation_status = determine_preparation_status(
            tender_found=True,
            document_counts=DocumentCounts(
                found=1,
                downloaded=1,
                with_extracted_text=1,
            ),
            marketplace_auth_blocked=False,
            document_risk_ready=True,
            primary_technical_required=status["required"],
            primary_technical_ready=status["ready"],
        )

        self.assertTrue(status["required"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["processed"][0]["document_id"], "000000105")
        self.assertTrue(status["ready"])
        self.assertEqual(preparation_status, PreparationStatus.READY_FOR_LLM)

    def test_downloaded_primary_technical_doc_without_text_is_not_processed(self) -> None:
        tender = {
            "id": "tender-1",
            "external_id": "00000021",
            "title": "Поставка серверного оборудования",
            "raw": {
                "full": {
                    "documents": [
                        {"id": "000000105", "title": "Техническое задание.docx"}
                    ]
                }
            },
        }
        downloaded = [
            {
                "id": "local-ooz",
                "filename": "000000105_Техническое задание.docx",
                "extracted_text": "",
            }
        ]

        status = primary_technical_document_status(tender, downloaded)

        self.assertTrue(status["required"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["reason"], PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED)

    def test_prepare_tender_for_analysis_accepts_rows_without_document_url(self) -> None:
        started_at = datetime(2026, 5, 8, 12, 0, 0)
        tender = {
            "id": "tender-1",
            "external_id": "00000021",
            "title": "Поставка серверного оборудования",
            "raw": {
                "full": {
                    "documents": [
                        {
                            "id": "000000105",
                            "title": "Приложение 1 Описание Объекта закупки.docx",
                        }
                    ]
                }
            },
        }
        api_docs = tender["raw"]["full"]["documents"]
        downloaded_docs = [
            {
                "id": "local-ooz",
                "filename": "000000105_Приложение 1 Описание Объекта закупки.docx",
                "storage_path": "data/documents/zakupki360_00000021/000000105_ooz.docx",
                "extracted_text": "technical text",
                "created_at": started_at,
            }
        ]
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: api_docs

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch("app.pipeline.prepare_tender_for_analysis.db_now", return_value=started_at),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                side_effect=[downloaded_docs, downloaded_docs],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_targeted_download_retry_state",
                return_value=TargetedDownloadRetryState(),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_targeted_document_download",
                return_value=(
                    FakeDownloadReport(
                        total_saved=0,
                        documents_found=1,
                        documents_selected=1,
                        stopped_due_to_rate_limit=False,
                    ),
                    [],
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                return_value=StepOutcome(attempted=True, succeeded=True),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                return_value=(
                    StepOutcome(attempted=True, succeeded=True, summary="ready"),
                    {"documents_with_text": 1, "tailoring_risk": "low"},
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=[],
            ),
        ):
            result = prepare_tender_for_analysis(external_id="00000021", limit_docs=1)

        self.assertEqual(result.status, PreparationStatus.READY_FOR_LLM)
        self.assertTrue(result.ready_for_llm)
        self.assertEqual(result.document_counts.with_extracted_text, 1)
        primary_details = result.steps["targeted_document_download"].details
        self.assertTrue(primary_details["primary_technical_documents_required"])
        self.assertEqual(primary_details["primary_technical_documents_total"], 1)
        self.assertTrue(primary_details["primary_technical_documents_ready"])
        self.assertEqual(
            primary_details["primary_technical_documents_processed"][0][
                "document_id"
            ],
            "000000105",
        )

    def test_rate_limited_targeted_download_requeues_then_recovers(self) -> None:
        started_at = datetime(2026, 5, 4, 12, 0, 0)
        second_started_at = started_at + timedelta(
            seconds=TARGETED_REQUEUE_BACKOFF_SECONDS + 1
        )
        docs = [
            {"id": "tz", "title": "\u0422\u0417"},
            {"id": "other", "title": "Other"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "00000009",
            "title": "Target tender",
        }
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: docs
        collector.record_processing_event = lambda *args, **kwargs: None

        first_report = FakeDownloadReport(
            total_saved=0,
            documents_found=2,
            documents_selected=2,
            stopped_due_to_rate_limit=True,
            skipped_due_to_rate_limit=2,
        )
        second_report = FakeDownloadReport(
            total_saved=1,
            documents_found=2,
            documents_selected=2,
            stopped_due_to_rate_limit=False,
        )

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch(
                "app.pipeline.prepare_tender_for_analysis.db_now",
                side_effect=[started_at, second_started_at],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                side_effect=[
                    [],
                    [],
                    [],
                    [
                        {
                            "id": "doc-1",
                            "filename": "tz_ТЗ.pdf",
                            "extracted_text": "ready text",
                        }
                    ],
                ],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_targeted_download_retry_state",
                side_effect=[
                    TargetedDownloadRetryState(),
                    TargetedDownloadRetryState(
                        attempts=1,
                        next_retry_after=started_at
                        + timedelta(seconds=TARGETED_REQUEUE_BACKOFF_SECONDS),
                        reason="rate_limited_before_any_document_saved",
                    ),
                ],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_targeted_document_download",
                side_effect=[
                    (first_report, ["Targeted download hit 429"]),
                    (second_report, ["Saved: TZ.pdf"]),
                ],
            ) as run_download,
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                side_effect=[
                    StepOutcome(attempted=False, succeeded=False),
                    StepOutcome(attempted=True, succeeded=True),
                ],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                side_effect=[
                    (
                        StepOutcome(
                            attempted=False,
                            succeeded=False,
                            summary="skipped",
                        ),
                        None,
                    ),
                    (
                        StepOutcome(
                            attempted=True,
                            succeeded=True,
                            summary="ready",
                        ),
                        {"documents_with_text": 1, "tailoring_risk": "low"},
                    ),
                ],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=[],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.record_targeted_retry_decision"
            ) as record_retry,
        ):
            first = prepare_tender_for_analysis(external_id="00000009", limit_docs=2)
            second = prepare_tender_for_analysis(external_id="00000009", limit_docs=2)

        self.assertEqual(first.status, PreparationStatus.PARTIAL_PREPARATION)
        first_details = first.steps["targeted_document_download"].details
        self.assertTrue(first_details["persistent_requeue_scheduled"])
        self.assertEqual(first_details["persistent_retry_attempts"], 1)
        self.assertIn("targeted_requeue_scheduled=true", first.summary)
        self.assertEqual(second.status, PreparationStatus.READY_FOR_LLM)
        self.assertTrue(second.ready_for_llm)
        self.assertEqual(second.document_counts.downloaded, 1)
        self.assertEqual(second.document_counts.with_extracted_text, 1)
        self.assertEqual(run_download.call_count, 2)
        record_retry.assert_called_once()

    def test_partial_rate_limit_keeps_llm_ready_when_primary_text_exists_and_records_warning(self) -> None:
        started_at = datetime(2026, 5, 4, 12, 0, 0)
        docs = [
            {"id": "tz", "title": "\u0422\u0417"},
            {"id": "contract", "title": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "00000009",
            "title": "Target tender",
        }
        downloaded_docs = [
            {
                "id": "local-tz",
                "filename": "tz_TZ.pdf",
                "document_url": "z360://document/tz",
                "storage_path": "data/documents/tz_TZ.pdf",
                "extracted_text": "primary technical text",
                "created_at": started_at,
            }
        ]
        recorded_events: list[tuple[str, str, dict]] = []
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: docs
        collector.record_processing_event = (
            lambda tender_id, event_type, message: recorded_events.append(
                (tender_id, event_type, json.loads(message))
            )
        )

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch(
                "app.pipeline.prepare_tender_for_analysis.db_now",
                return_value=started_at,
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                side_effect=[[], downloaded_docs],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_targeted_download_retry_state",
                return_value=TargetedDownloadRetryState(),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_targeted_document_download",
                return_value=(
                    FakeDownloadReport(
                        total_saved=1,
                        documents_found=2,
                        documents_selected=2,
                        stopped_due_to_rate_limit=True,
                        skipped_due_to_rate_limit=1,
                    ),
                    ["Stopping targeted download after 429"],
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                return_value=StepOutcome(attempted=True, succeeded=True),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                return_value=(
                    StepOutcome(attempted=True, succeeded=True, summary="ready"),
                    {"documents_with_text": 1, "tailoring_risk": "low"},
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=[],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.record_targeted_retry_decision"
            ) as record_retry,
        ):
            result = prepare_tender_for_analysis(external_id="00000009", limit_docs=2)

        self.assertEqual(result.status, PreparationStatus.READY_FOR_LLM)
        self.assertTrue(result.ready_for_llm)
        self.assertTrue(result.partial_preparation)
        record_retry.assert_not_called()
        details = result.steps["targeted_document_download"].details
        self.assertEqual(details["documents_skipped_due_to_rate_limit"], 1)
        self.assertEqual(
            details["partial_document_download_warning"],
            "documents_skipped_due_to_rate_limit",
        )
        self.assertEqual(len(recorded_events), 1)
        tender_id, event_type, payload = recorded_events[0]
        self.assertEqual(tender_id, "tender-1")
        self.assertEqual(event_type, "targeted_document_download_partial")
        self.assertEqual(payload["reason"], "rate_limit_429")
        self.assertEqual(payload["documents_skipped_due_to_rate_limit"], 1)

    def test_rate_limited_targeted_download_exhausts_after_retry_limit(self) -> None:
        started_at = datetime(2026, 5, 4, 12, 0, 0)
        docs = [{"id": "tz", "title": "\u0422\u0417"}]
        tender = {
            "id": "tender-1",
            "external_id": "00000009",
            "title": "Target tender",
        }
        collector = types.ModuleType("app.collector.documents")
        collector.get_target_tender = lambda **kwargs: tender
        collector.tender_documents = lambda tender: docs
        collector.record_processing_event = lambda *args, **kwargs: None
        report = FakeDownloadReport(
            total_saved=0,
            documents_found=1,
            documents_selected=1,
            stopped_due_to_rate_limit=True,
            skipped_due_to_rate_limit=1,
        )

        with (
            patch.dict(sys.modules, {"app.collector.documents": collector}),
            patch(
                "app.pipeline.prepare_tender_for_analysis.db_now",
                return_value=started_at,
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                side_effect=[[], []],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_targeted_download_retry_state",
                return_value=TargetedDownloadRetryState(
                    attempts=TARGETED_REQUEUE_MAX_ATTEMPTS - 1,
                    next_retry_after=started_at - timedelta(seconds=1),
                    reason="rate_limited_before_any_document_saved",
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_targeted_document_download",
                return_value=(report, ["Targeted download hit 429"]),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.extract_text_for_tender",
                return_value=StepOutcome(attempted=False, succeeded=False),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.run_document_risk_analysis",
                return_value=(
                    StepOutcome(attempted=False, succeeded=False, summary="skipped"),
                    None,
                ),
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_processing_events_since",
                return_value=[],
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.record_targeted_retry_decision"
            ) as record_retry,
        ):
            result = prepare_tender_for_analysis(external_id="00000009", limit_docs=1)

        self.assertEqual(result.status, PreparationStatus.NO_VALID_DOCUMENTS)
        details = result.steps["targeted_document_download"].details
        self.assertFalse(details["persistent_requeue_scheduled"])
        self.assertTrue(details["persistent_retry_exhausted"])
        self.assertEqual(details["persistent_retry_attempts"], TARGETED_REQUEUE_MAX_ATTEMPTS)
        self.assertEqual(
            details["final_missing_reason"],
            PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT,
        )
        self.assertIn("persistent_retry_exhausted=true", result.summary)
        self.assertIn(
            f"final_missing_reason={PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT}",
            result.summary,
        )
        record_retry.assert_called_once()

    def test_extraction_step_records_empty_text_per_document(self) -> None:
        extractor = types.ModuleType("app.document_text_extractor")
        extractor.APP_DIR = Path("/app")
        extractor.clean_text = lambda value: value
        extractor.extract_file = lambda path: ""
        extractor.empty_extraction_reason = lambda path: "antiword_not_found"
        saved: list[tuple[str, str]] = []
        extractor.save_extracted_text = lambda document_id, text: saved.append((document_id, text))
        docs = [
            {
                "id": "doc-1",
                "filename": "TZ.doc",
                "storage_path": "data/documents/TZ.doc",
                "extracted_text": "",
            }
        ]

        with (
            patch.dict(sys.modules, {"app.document_text_extractor": extractor}),
            patch(
                "app.pipeline.prepare_tender_for_analysis.load_downloaded_documents",
                return_value=docs,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = extract_text_for_tender("tender-1")

        self.assertTrue(result.attempted)
        self.assertFalse(result.succeeded)
        self.assertEqual(saved, [("doc-1", "")])
        self.assertEqual(result.details["documents_processed"], 1)
        self.assertEqual(result.details["documents_attempted_extraction"], 1)
        self.assertEqual(result.details["documents_extracted"], 0)
        self.assertEqual(result.details["documents_failed"], 1)
        self.assertEqual(result.details["failure_suffix_counts"], {".doc": 1})
        self.assertEqual(
            result.details["readiness_after_step"],
            DOCUMENTS_DOWNLOADED_WITHOUT_TEXT,
        )
        self.assertIn("antiword_not_found", result.details["errors"][0])
        self.assertEqual(result.details["document_results"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
