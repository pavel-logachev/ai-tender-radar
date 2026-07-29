from __future__ import annotations

import json
import os
import unittest
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

try:
    import httpx
    import psycopg  # noqa: F401
    import pydantic_settings  # noqa: F401
except ModuleNotFoundError:
    httpx = types.ModuleType("httpx")

    class HTTPStatusError(Exception):
        def __init__(self, message, *, request, response):
            super().__init__(message)
            self.request = request
            self.response = response

    class RequestError(Exception):
        pass

    class ConnectError(RequestError):
        pass

    class ConnectTimeout(RequestError):
        pass

    class Request:
        def __init__(self, method, url):
            self.method = method
            self.url = url

    class Response:
        def __init__(self, status_code, *, request, headers=None):
            self.status_code = status_code
            self.request = request
            self.headers = headers or {}

    httpx.HTTPStatusError = HTTPStatusError
    httpx.RequestError = RequestError
    httpx.ConnectError = ConnectError
    httpx.ConnectTimeout = ConnectTimeout
    httpx.Request = Request
    httpx.Response = Response
    sys.modules["httpx"] = httpx

    psycopg_stub = types.ModuleType("psycopg")
    rows_stub = types.ModuleType("psycopg.rows")
    rows_stub.dict_row = object()
    json_stub = types.ModuleType("psycopg.types.json")
    json_stub.Jsonb = lambda value: value
    types_stub = types.ModuleType("psycopg.types")
    types_stub.json = json_stub
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = rows_stub
    sys.modules["psycopg.types"] = types_stub
    sys.modules["psycopg.types.json"] = json_stub

    config_stub = types.ModuleType("app.config")
    config_stub.settings = types.SimpleNamespace(
        database_url="postgresql://test/test",
        z360_base_url="https://example.test",
        z360_login="login",
        z360_password="password",
        z360_rate_limit_seconds=1.1,
        z360_document_rate_limit_seconds=None,
        llm_json_mode=True,
    )
    sys.modules["app.config"] = config_stub

    business_rules_stub = types.ModuleType("app.business_rules")
    business_rules_stub.is_incumbent_extension = lambda row: False
    business_rules_stub.is_service_noise = lambda row: False
    sys.modules["app.business_rules"] = business_rules_stub

if not all(hasattr(httpx, name) for name in ("HTTPStatusError", "RequestError", "Request", "Response")):
    RequestErrorBase = getattr(httpx, "RequestError", Exception)

    class HTTPStatusError(RequestErrorBase):
        def __init__(self, message, *, request, response):
            super().__init__(message)
            self.request = request
            self.response = response

    class Request:
        def __init__(self, method, url):
            self.method = method
            self.url = url

    class Response:
        def __init__(self, status_code, *, request, headers=None):
            self.status_code = status_code
            self.request = request
            self.headers = headers or {}

    if not hasattr(httpx, "RequestError"):
        httpx.RequestError = RequestErrorBase
    if not hasattr(httpx, "HTTPStatusError"):
        httpx.HTTPStatusError = HTTPStatusError
    if not hasattr(httpx, "Request"):
        httpx.Request = Request
    if not hasattr(httpx, "Response"):
        httpx.Response = Response

business_rules_module = sys.modules.get("app.business_rules")
if business_rules_module is not None:
    if not hasattr(business_rules_module, "is_incumbent_extension"):
        business_rules_module.is_incumbent_extension = lambda row: False
    if not hasattr(business_rules_module, "is_service_noise"):
        business_rules_module.is_service_noise = lambda row: False

from app.collector.documents import (
    download_document,
    document_priority,
    document_url,
    is_allowed_direct_document_url,
    prioritize_documents_for_download,
    run_targeted_download_report,
)
from app.document_status import build_document_status


def rate_limit_error(*, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/api/orders/document/1")
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    response = httpx.Response(429, request=request, headers=headers)
    return httpx.HTTPStatusError(
        "429 Too Many Requests",
        request=request,
        response=response,
    )


class TargetedDocumentDownloadTest(unittest.TestCase):
    def test_prioritizes_high_value_documents_before_other_files(self) -> None:
        docs = [
            {"id": "other", "title": "Other attachment"},
            {"id": "contract", "title": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430"},
            {"id": "ooz", "title": "\u041e\u041e\u0417"},
            {"id": "tz", "title": "\u0422\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0435 \u0437\u0430\u0434\u0430\u043d\u0438\u0435"},
            {"id": "nmc", "title": "\u0420\u0430\u0441\u0447\u0435\u0442 \u041d\u041c\u0426\u041a"},
        ]

        ordered_ids = [doc["id"] for doc in prioritize_documents_for_download(docs)]

        self.assertEqual(ordered_ids, ["ooz", "tz", "nmc", "contract", "other"])

    def test_kd_materials_server_pc_doc_stays_above_contract_for_hardware_tender(self) -> None:
        tender = {
            "title": "Поставка оборудования не требующего монтажа (ОНМ)",
            "_llm_category": "servers",
        }
        docs = [
            {"id": "notice", "title": "444854856_1_Извещение_ОНМ_серверы пк.docx"},
            {"id": "contract", "title": "444854860_2_Проект договора обор-е.docx"},
            {"id": "kd", "title": "2_КД_материалы_ОНМ_серверы пк.docx"},
        ]

        ordered_ids = [
            doc["id"]
            for doc in prioritize_documents_for_download(docs, tender=tender)
        ]

        self.assertEqual(document_priority(docs[2], tender=tender), 1)
        self.assertEqual(ordered_ids[0], "kd")
        self.assertLess(
            document_priority(docs[2], tender=tender),
            document_priority(docs[1], tender=tender),
        )

    def test_contract_does_not_displace_kd_materials_in_limited_selection(self) -> None:
        tender = {"title": "Поставка серверного оборудования"}
        docs = [
            {"id": "contract", "title": "Проект договора.docx"},
            {"id": "notice", "title": "Извещение.docx"},
            {"id": "kd", "title": "КД материалы.docx"},
        ]

        selected = prioritize_documents_for_download(docs, tender=tender)[:1]

        self.assertEqual([doc["id"] for doc in selected], ["kd"])

    def test_targeted_download_uses_shortlist_category_context_for_generic_kd(self) -> None:
        docs = [
            {"id": "contract", "title": "Проект договора.docx"},
            {"id": "kd", "title": "КД материалы.docx"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95721719",
            "title": "Поставка оборудования не требующего монтажа (ОНМ)",
            "raw": {"full": {"documents": docs}},
        }
        attempted: list[str] = []

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            return False

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95721719",
                tender_context={"_llm_category": "servers"},
                limit_docs=1,
                document_rate_limit_seconds=1.1,
            )

        self.assertEqual(attempted, ["kd"])
        self.assertEqual(result.documents_selected_items[0]["document_id"], "kd")

    def test_document_download_planner_ranks_technical_metadata(self) -> None:
        docs = [
            {"id": "docs", "title": "Документация.docx", "fileExtension": ".docx"},
            {"id": "contract", "title": "Проект договора.doc", "fileExtension": ".doc"},
            {"id": "tz", "title": "ТехЗадание.pdf", "fileExtension": ".pdf"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95842229",
            "title": "Поставка системы хранения данных",
            "raw": {"full": {"documents": docs}},
        }
        attempted: list[str] = []

        class FakePlannerClient:
            provider = "routerai"
            model = "fake"

            def generate_chat_completion(self, **kwargs):
                return types.SimpleNamespace(
                    text=json.dumps(
                        {
                            "download_first": [
                                {
                                    "document_id": "tz",
                                    "expected_role": "technical_requirements",
                                    "priority": 0,
                                    "reason": "Likely technical assignment.",
                                }
                            ],
                            "download_if_budget_allows": [
                                {
                                    "document_id": "docs",
                                    "expected_role": "other",
                                    "priority": 1,
                                    "reason": "May contain embedded TZ.",
                                }
                            ],
                            "likely_irrelevant": [
                                {
                                    "document_id": "contract",
                                    "expected_role": "contract",
                                    "priority": 9,
                                    "reason": "Draft contract.",
                                }
                            ],
                            "missing_expected_documents": [],
                            "confidence": "high",
                        },
                        ensure_ascii=False,
                    ),
                    response_id=None,
                    usage=None,
                )

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            return False

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch(
                "app.llm.document_download_planner.create_llm_client",
                return_value=FakePlannerClient(),
            ),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95842229",
                limit_docs=3,
                document_rate_limit_seconds=1.1,
                use_llm_planner=True,
            )

        self.assertTrue(result.document_download_planner_used)
        self.assertEqual(result.document_download_planner_confidence, "high")
        self.assertLess(attempted.index("tz"), attempted.index("contract"))
        self.assertEqual(result.documents_selected_items[0]["document_id"], "tz")

    def test_document_download_planner_fallback_on_invalid_json(self) -> None:
        docs = [
            {"id": "docs", "title": "Документация.docx"},
            {"id": "contract", "title": "Проект договора.doc"},
            {"id": "tz", "title": "ТехЗадание.pdf"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95842229",
            "title": "Поставка системы хранения данных",
            "raw": {"full": {"documents": docs}},
        }
        attempted: list[str] = []

        class BadPlannerClient:
            provider = "routerai"

            def generate_chat_completion(self, **kwargs):
                return types.SimpleNamespace(text="not json", response_id=None, usage=None)

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            return False

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch(
                "app.llm.document_download_planner.create_llm_client",
                return_value=BadPlannerClient(),
            ),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95842229",
                limit_docs=3,
                document_rate_limit_seconds=1.1,
                use_llm_planner=True,
            )

        self.assertFalse(result.document_download_planner_used)
        self.assertEqual(attempted[0], "tz")
        self.assertLess(
            document_priority(docs[2], tender=tender),
            document_priority(docs[1], tender=tender),
        )

    def test_planner_accepts_documentation_role_and_fills_to_limit(self) -> None:
        docs = [
            {"id": "docs", "title": "Извещение и документация о закупке.docx"},
            {"id": "contract", "title": "Проект договора.doc"},
            {"id": "form", "title": "Форма заявки.docx"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95847264",
            "title": "Поставка оборудования",
            "raw": {"full": {"documents": docs}},
        }
        attempted: list[str] = []

        class DocumentationPlannerClient:
            provider = "routerai"

            def generate_chat_completion(self, **kwargs):
                return types.SimpleNamespace(
                    text=json.dumps(
                        {
                            "download_first": [
                                {
                                    "document_id": "docs",
                                    "expected_role": "documentation",
                                    "priority": 0,
                                    "reason": "May embed the TZ.",
                                }
                            ],
                            "download_if_budget_allows": [],
                            "likely_irrelevant": [
                                {
                                    "document_id": "contract",
                                    "expected_role": "contract",
                                    "priority": 9,
                                    "reason": "Draft contract.",
                                }
                            ],
                            "missing_expected_documents": [],
                            "confidence": "medium",
                        },
                        ensure_ascii=False,
                    ),
                    response_id=None,
                    usage=None,
                )

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            return False

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch(
                "app.llm.document_download_planner.create_llm_client",
                return_value=DocumentationPlannerClient(),
            ),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95847264",
                limit_docs=2,
                document_rate_limit_seconds=1.1,
                use_llm_planner=True,
            )

        self.assertEqual(attempted, ["docs", "form"])
        self.assertEqual(
            result.documents_selected_items[0]["expected_role"],
            "documentation",
        )
        self.assertEqual(result.documents_selected, 2)

    def test_planner_is_not_called_when_targeted_flag_is_disabled(self) -> None:
        docs = [
            {"id": "tz", "title": "ТехЗадание.pdf"},
            {"id": "contract", "title": "Проект договора.doc"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95842229",
            "title": "Поставка системы хранения данных",
            "raw": {"full": {"documents": docs}},
        }
        attempted: list[str] = []

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            return False

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch(
                "app.llm.document_download_planner.create_llm_client",
                side_effect=AssertionError("planner should not be called"),
            ),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95842229",
                limit_docs=2,
                document_rate_limit_seconds=1.1,
            )

        self.assertFalse(result.document_download_planner_used)
        self.assertEqual(attempted, ["tz", "contract"])

    def test_application_object_description_specification_gets_top_priority(self) -> None:
        doc = {
            "id": "appendix",
            "title": "Приложение 2 Описание предмета закупки Спецификация.docx",
        }

        self.assertEqual(document_priority(doc), 0)

    def test_object_description_priority_beats_contract_and_application_requirements(self) -> None:
        object_description = {
            "id": "ooz",
            "title": "Приложение 1 Описание Объекта закупки.docx",
        }
        contract = {"id": "contract", "title": "Проект контракта.docx"}
        requirements = {"id": "req", "title": "Требования к составу заявки.docx"}

        self.assertLess(document_priority(object_description), document_priority(contract))
        self.assertLess(document_priority(object_description), document_priority(requirements))

    def test_technical_assignment_has_top_priority(self) -> None:
        self.assertEqual(
            document_priority({"id": "tz", "title": "Техническое задание.docx"}),
            0,
        )

    def test_limited_targeted_selection_keeps_object_description(self) -> None:
        docs = [
            {"id": "contract", "title": "Проект контракта.docx"},
            {"id": "req", "title": "Требования к составу заявки.docx"},
            {"id": "nmc", "title": "Приложение 2 Обоснование НМЦК.docx"},
            {"id": "ooz", "title": "Приложение 1 Описание Объекта закупки.docx"},
        ]

        selected = prioritize_documents_for_download(docs)[:2]

        self.assertIn("ooz", [doc["id"] for doc in selected])
        self.assertEqual([doc["id"] for doc in selected], ["ooz", "nmc"])

    def test_immersive_sound_documents_order_primary_before_commercial_and_legal_docs(self) -> None:
        docs = [
            {"id": "gisp", "title": "Уведомления ГИСП.docx"},
            {"id": "notice", "title": "Извещение.docx"},
            {"id": "contract", "title": "Проект контракта.docx"},
            {"id": "req", "title": "Требования к составу заявки.docx"},
            {"id": "nmc", "title": "Приложение 2 Обоснование НМЦК иммеривный звук.docx"},
            {
                "id": "ooz",
                "title": "Приложение 1 Описание Объекта закупки иммерсивный звук.docx",
            },
        ]

        ordered_ids = [doc["id"] for doc in prioritize_documents_for_download(docs)]

        self.assertEqual(
            ordered_ids,
            ["ooz", "nmc", "req", "contract", "notice", "gisp"],
        )

    def test_generic_procurement_document_stays_below_primary_technical_doc(self) -> None:
        docs = [
            {"id": "generic", "title": "Документация.docx"},
            {"id": "contract", "title": "Проект договора.docx"},
            {"id": "tz", "title": "Техническое задание.docx"},
        ]

        ordered_ids = [doc["id"] for doc in prioritize_documents_for_download(docs)]

        self.assertGreater(document_priority(docs[0]), 0)
        self.assertEqual(ordered_ids, ["tz", "generic", "contract"])

    def test_targeted_download_retries_one_429_then_continues(self) -> None:
        docs = [
            {"id": "tz", "title": "\u0422\u0417"},
            {"id": "contract", "title": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430"},
            {"id": "other", "title": "Other"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95543713",
            "title": "Target tender",
            "raw": {"full": {"documents": docs}},
        }
        outcomes: list[bool | Exception] = [
            True,
            rate_limit_error(),
            True,
            True,
        ]
        attempted: list[str] = []

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch("app.collector.documents.time.sleep") as sleep,
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95543713",
                limit_docs=3,
                document_rate_limit_seconds=1.1,
            )

        self.assertEqual(attempted, ["tz", "contract", "contract", "other"])
        self.assertEqual(result.total_saved, 3)
        self.assertEqual(result.rate_limit_retries, 1)
        self.assertEqual(result.rate_limit_backoffs_seconds, [5.0])
        self.assertFalse(result.stopped_due_to_rate_limit)
        self.assertEqual(result.documents_skipped_due_to_rate_limit, 0)
        sleep.assert_called_once_with(5.0)

    def test_targeted_download_reports_rate_limited_documents_after_retry_budget(self) -> None:
        docs = [
            {"id": "tz", "title": "\u0422\u0417"},
            {"id": "contract", "title": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430"},
            {"id": "other", "title": "Other"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95543713",
            "title": "Target tender",
            "raw": {"full": {"documents": docs}},
        }
        outcomes: list[bool | Exception] = [
            True,
            rate_limit_error(),
            rate_limit_error(),
        ]
        attempted: list[str] = []

        def fake_download(client, tender, doc, **kwargs) -> bool:
            attempted.append(str(doc["id"]))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch("app.collector.documents.time.sleep") as sleep,
            patch("app.collector.documents.record_processing_event") as record_event,
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95543713",
                limit_docs=3,
                document_rate_limit_seconds=1.1,
            )

        self.assertEqual(attempted, ["tz", "contract", "contract"])
        self.assertEqual(result.total_saved, 1)
        self.assertEqual(result.rate_limit_retries, 1)
        self.assertTrue(result.stopped_due_to_rate_limit)
        self.assertEqual(result.documents_skipped_due_to_rate_limit, 2)
        self.assertEqual(
            [
                item["document_id"]
                for item in result.documents_skipped_due_to_rate_limit_items
            ],
            ["contract", "other"],
        )
        sleep.assert_called_once_with(5.0)
        record_event.assert_called_once()

    def test_targeted_download_records_non_rate_limit_failure(self) -> None:
        docs = [{"id": "tz", "title": "Technical task"}]
        tender = {
            "id": "tender-1",
            "external_id": "95543713",
            "title": "Target tender",
            "raw": {"full": {"documents": docs}},
        }

        def fake_download(client, tender, doc, **kwargs) -> bool:
            raise RuntimeError("boom")

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch("app.collector.documents.record_processing_event") as record_event,
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95543713",
                limit_docs=1,
                document_rate_limit_seconds=1.1,
            )

        self.assertEqual(result.total_saved, 0)
        self.assertEqual(result.documents_failed, 1)
        self.assertEqual(result.documents_failed_items[0]["document_id"], "tz")
        self.assertEqual(
            result.documents_failed_items[0]["reason"],
            "exception:RuntimeError",
        )
        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.args[1], "document_download_failed")

    def test_download_document_falls_back_to_allowed_direct_url_on_no_content(self) -> None:
        tender = {
            "id": "tender-1",
            "external_id": "95881047",
        }
        doc = {
            "id": "445589013",
            "title": "Technical requirements.pdf",
            "url": "https://zakupki.gov.ru/223/purchase/public/download/download.html?id=110744982",
        }

        class FakeResponse:
            def __init__(
                self,
                status_code: int,
                *,
                content: bytes = b"",
                headers: dict[str, str] | None = None,
                url: str = "https://example.test/document",
            ) -> None:
                self.status_code = status_code
                self.content = content
                self.headers = headers or {}
                self.url = url

            def raise_for_status(self) -> None:
                return None

        z360_response = FakeResponse(
            204,
            url="https://api.zakupki360.ru/api/orders/document/445589013",
        )
        direct_response = FakeResponse(
            200,
            content=b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
            headers={"content-type": "application/pdf"},
            url=doc["url"],
        )
        saved_records: list[dict] = []

        class FakeClient:
            def request(self, *args, **kwargs):
                return z360_response

        with TemporaryDirectory() as tmp_dir:
            app_dir = Path(tmp_dir)
            data_dir = app_dir / "data" / "documents"
            with (
                patch("app.collector.documents.APP_DIR", app_dir),
                patch("app.collector.documents.DATA_DIR", data_dir),
                patch("app.collector.documents.existing_document_info", return_value=None),
                patch("app.collector.documents.document_already_downloaded", return_value=False),
                patch(
                    "app.collector.documents.download_direct_document",
                    return_value=direct_response,
                ) as direct_download,
                patch(
                    "app.collector.documents.save_document_record",
                    side_effect=lambda **kwargs: saved_records.append(kwargs),
                ),
                patch("app.collector.documents.record_processing_event") as record_event,
            ):
                saved = download_document(
                    FakeClient(),
                    tender,
                    doc,
                    document_rate_limit_seconds=1.1,
                )

            self.assertTrue(saved)
            direct_download.assert_called_once()
            record_event.assert_not_called()
            self.assertEqual(saved_records[0]["document_url"], document_url("445589013"))
            saved_path = app_dir / saved_records[0]["storage_path"]
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.read_bytes(), direct_response.content)

    def test_direct_document_url_allows_only_trusted_https_zakupki_hosts(self) -> None:
        self.assertTrue(
            is_allowed_direct_document_url(
                "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/file.html"
            )
        )
        self.assertTrue(
            is_allowed_direct_document_url(
                "https://sub.zakupki.gov.ru/44fz/filestore/public/1.0/download/file.html"
            )
        )
        self.assertFalse(
            is_allowed_direct_document_url(
                "http://zakupki.gov.ru/44fz/filestore/public/1.0/download/file.html"
            )
        )
        self.assertFalse(
            is_allowed_direct_document_url(
                "https://zakupki.gov.ru.evil.test/44fz/download/file.html"
            )
        )
        self.assertFalse(
            is_allowed_direct_document_url(
                "https://token:" + "secret@zakupki.gov.ru/44fz/download/file.html"
            )
        )

    def test_rate_limited_high_value_doc_reflected_in_document_status(self) -> None:
        docs = [
            {"id": "tz", "title": "ТехЗадание.pdf"},
            {"id": "contract", "title": "Проект договора.docx"},
        ]
        tender = {
            "id": "tender-1",
            "external_id": "95842229",
            "title": "Поставка СХД",
            "raw": {"full": {"documents": docs}},
        }
        outcomes: list[Exception] = [rate_limit_error(), rate_limit_error()]

        def fake_download(client, tender, doc, **kwargs) -> bool:
            raise outcomes.pop(0)

        with (
            patch("app.collector.documents.get_target_tender", return_value=tender),
            patch("app.collector.documents.download_document", side_effect=fake_download),
            patch("app.collector.documents.time.sleep"),
            patch("app.collector.documents.record_processing_event"),
        ):
            result = run_targeted_download_report(
                object(),
                external_id="95842229",
                limit_docs=2,
                document_rate_limit_seconds=1.1,
            )

        self.assertTrue(result.stopped_due_to_rate_limit)
        self.assertTrue(result.missing_high_value_technical_document)
        self.assertEqual(result.missing_high_value_technical_document_title, "ТехЗадание.pdf")

        status = build_document_status(
            document_preparation={
                **result.to_dict(),
                "targeted_download_triggered": True,
                "docs_after": 0,
                "docs_with_text_after": 0,
            },
            documents_summary=[],
        )

        self.assertTrue(status["missing_high_value_technical_document"])
        self.assertTrue(status["documents_skipped_due_to_rate_limit"])
        self.assertIn("429/rate limit", status["warning"])


if __name__ == "__main__":
    unittest.main()
