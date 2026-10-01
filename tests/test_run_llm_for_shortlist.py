from __future__ import annotations

import contextlib
import importlib
import io
import json
import subprocess
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


SHORTLIST_MODULE_NAME = "app.pipeline.run_llm_for_shortlist"


def fresh_lead_triage_created_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_shortlist_dependency_stubs() -> dict[str, types.ModuleType]:
    business_profile = types.ModuleType("app.business_profile")
    business_profile.category_priority = lambda cfg: 0
    business_profile.get_price = lambda row: int(row.get("price") or 0)
    business_profile.is_excluded_vertical = lambda row, profile: False
    business_profile.is_full_deal_for_category = lambda row, profile, category_cfg: True
    business_profile.has_generic_equipment_target_hardware_evidence = lambda row, profile: False
    business_profile.is_low_priority_deal = lambda row, profile: False
    business_profile.load_business_profile = lambda path: {}
    business_profile.match_target_category = lambda row, profile: ("storage", {"label": "Storage"})
    business_profile.is_generic_equipment_title = lambda row, profile: False
    business_profile.domestic_context_keywords = lambda profile=None: []
    business_profile.domestic_safe_vendor_examples = lambda profile=None: [
        "YADRO",
        "Aquarius",
        "DEPO",
    ]
    business_profile.foreign_vendor_recommendation_keywords = lambda profile=None: [
        "dell",
        "hpe",
        "lenovo",
    ]
    business_profile.normalize = lambda value: str(value or "").lower().replace("ё", "е")

    business_rules = types.ModuleType("app.business_rules")
    business_rules.business_assessment = lambda row: {"market_access": "open"}
    business_rules.effective_recommendation = lambda row: "go"

    digest = types.ModuleType("app.digest")
    digest.DEFAULT_DEADLINE_MIN_DAYS = 3.0
    digest.SALES_EXISTING_CLIENT_STATUS = "sales_existing_client"
    digest.get_digest_rows = lambda limit: []

    def hidden_by_existing_client_customer(row: dict) -> bool:
        return (
            row.get("latest_sales_status") != "sales_returned"
            and (
                row.get("existing_client_customer_status") == "sales_existing_client"
                or bool(row.get("existing_client_source_tender_id"))
            )
        )

    def parse_report_datetime(value):
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def deadline_is_active(row: dict, *, now: datetime | None = None) -> bool:
        deadline = row.get("deadline_at")
        if deadline is None:
            return True
        current_time = now or datetime.now(timezone.utc)
        return deadline >= current_time

    def deadline_is_fresh(
        row: dict,
        min_days: float = 3.0,
        *,
        now: datetime | None = None,
    ) -> bool:
        deadline = row.get("deadline_at")
        if deadline is None:
            return True
        current_time = now or datetime.now(timezone.utc)
        return deadline >= current_time + timedelta(days=min_days)

    digest.deadline_is_active = deadline_is_active
    digest.deadline_is_fresh = deadline_is_fresh
    digest.hidden_by_existing_client_customer = hidden_by_existing_client_customer
    digest.parse_report_datetime = parse_report_datetime
    return {
        "app.business_profile": business_profile,
        "app.business_rules": business_rules,
        "app.digest": digest,
    }


def import_shortlist_with_dependency_stubs():
    dependency_stubs = build_shortlist_dependency_stubs()
    previous_shortlist_module = sys.modules.pop(SHORTLIST_MODULE_NAME, None)
    try:
        with patch.dict(sys.modules, dependency_stubs, clear=False):
            imported_shortlist = importlib.import_module(SHORTLIST_MODULE_NAME)
    finally:
        sys.modules.pop(SHORTLIST_MODULE_NAME, None)
        if previous_shortlist_module is not None:
            sys.modules[SHORTLIST_MODULE_NAME] = previous_shortlist_module

    return imported_shortlist, dependency_stubs


shortlist, SHORTLIST_DEPENDENCY_STUBS = import_shortlist_with_dependency_stubs()


class RunLLMForShortlistTest(unittest.TestCase):
    def test_shortlist_dependency_stubs_do_not_leak_into_import_state(self) -> None:
        for module_name, stub in SHORTLIST_DEPENDENCY_STUBS.items():
            self.assertIsNot(sys.modules.get(module_name), stub)
        self.assertIsNot(sys.modules.get(SHORTLIST_MODULE_NAME), shortlist)

    def test_live_triage_records_strict_contract_in_shadow_mode(self) -> None:
        payload = {
            "lead_decision": "go",
            "lead_priority": "high",
            "confidence": "medium",
            "lead_summary": "Есть повод для разговора.",
            "likely_customer_story": "Заказчик развивает инфраструктуру.",
            "possible_needs": ["серверы"],
            "target_roles": ["ИТ-директор"],
            "reject_reason": "",
            "requires_full_lead_report": "true",
        }
        response = SimpleNamespace(
            text=json.dumps(payload, ensure_ascii=False),
            provider="routerai",
            model="model/test",
            response_id="response-1",
            latency_seconds=0.25,
            usage={"total_tokens": 10},
        )
        client = SimpleNamespace(
            provider="routerai",
            generate_chat_completion=lambda **kwargs: response,
        )
        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(llm_json_mode=True)
        factory = types.ModuleType("app.llm.factory")
        factory.create_llm_client = lambda **kwargs: client
        tender_report = types.ModuleType("app.llm.tender_report")
        tender_report.build_package_from_database = lambda **kwargs: (
            "tender-1",
            {"external_id": "external-1"},
        )
        tender_report.package_to_markdown = lambda package: "context"
        tender_report.extract_json = lambda raw: payload

        with (
            patch.dict(
                sys.modules,
                {
                    "app.config": config,
                    "app.llm.factory": factory,
                    "app.llm.tender_report": tender_report,
                },
            ),
            patch.object(shortlist, "save_lead_triage_result") as save_result,
        ):
            result = shortlist.run_lead_triage_for_candidate(
                {"external_id": "external-1"}
            )

        self.assertTrue(result["requires_full_lead_report"])
        metadata = save_result.call_args.kwargs["metadata"]
        self.assertEqual(metadata["strict_validation"]["mode"], "shadow")
        self.assertEqual(metadata["strict_validation"]["status"], "failed")
        self.assertEqual(
            metadata["strict_validation"]["error_fields"],
            ["requires_full_lead_report"],
        )
        self.assertEqual(metadata["version_binding"]["model_id"], "model/test")

    def test_non_json_subprocess_failure_is_compactly_classified(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            "RuntimeError: Cannot parse JSON from LLM response: "
            + ("safe refusal text " * 100)
        )
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python", "-m", "app.llm.tender_report"],
            stderr=stderr,
        )

        failure = shortlist.describe_subprocess_failure("123", exc)

        self.assertEqual(failure.reason, "non_json_response")
        self.assertEqual(failure.returncode, 1)
        self.assertLessEqual(len(failure.detail), 500)
        self.assertIn("Cannot parse JSON from LLM response", failure.detail)

    def test_routerai_parse_failure_is_classified_as_non_json(self) -> None:
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python", "-m", "app.llm.tender_report"],
            stderr="RuntimeError: Cannot parse RouterAI packaging pass JSON response: not json",
        )

        failure = shortlist.describe_subprocess_failure("123", exc)

        self.assertEqual(failure.reason, "non_json_response")

    def test_transient_network_error_text_is_detected(self) -> None:
        self.assertTrue(
            shortlist.is_transient_network_error_text(
                "httpx.ConnectError: [Errno -3] Temporary failure in name resolution"
            )
        )

    def test_read_timeout_is_not_safe_transient_network_error(self) -> None:
        text = (
            "httpx.ReadTimeout: read timed out\n"
            "RuntimeError: RouterAI read timeout. Request may still be running "
            "or charged upstream; not retrying automatically to avoid duplicate "
            "paid requests."
        )

        self.assertFalse(shortlist.is_transient_network_error_text(text))
        self.assertTrue(shortlist.is_read_timeout_error_text(text))

    def test_documents_missing_is_not_transient_network_error(self) -> None:
        self.assertFalse(shortlist.is_transient_network_error_text("documents_missing"))

    def test_document_readiness_prepares_missing_docs_and_logs_warning(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(0, 0),
            shortlist.CandidateDocumentCounts(0, 0),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="no_valid_documents"),
            summary="No valid downloaded documents are available.",
            document_download_was_run=True,
        )
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(stdout),
        ):
            readiness = shortlist.ensure_targeted_documents_before_llm(
                {"external_id": "missing-docs"},
                dry_run=False,
            )

        output = stdout.getvalue()
        self.assertEqual(readiness.warning_reason, "documents_missing")
        self.assertEqual(readiness.document_state, "documents_missing")
        self.assertIn("docs_before=0", output)
        self.assertIn("docs_after=0", output)
        self.assertIn("docs_with_text_after=0", output)
        self.assertIn("targeted_download_triggered=true", output)
        self.assertIn("document_state=documents_missing", output)
        self.assertIn("preparation_status=no_valid_documents", output)
        self.assertIn("reason=documents_missing", output)

    def test_document_readiness_prepares_docs_without_text(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(2, 0),
            shortlist.CandidateDocumentCounts(2, 1),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="ready_for_llm"),
            summary="Tender is ready.",
            document_download_was_run=True,
        )
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(stdout),
        ):
            readiness = shortlist.ensure_targeted_documents_before_llm(
                {"external_id": "with-text-after-prepare"},
                dry_run=False,
            )

        self.assertIsNone(readiness.warning_reason)
        self.assertEqual(readiness.docs_before, 2)
        self.assertEqual(readiness.docs_after, 2)
        self.assertEqual(readiness.docs_with_text_after, 1)
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")
        self.assertTrue(readiness.extraction_retry_triggered)

    def test_primary_technical_doc_missing_keeps_candidate_not_ready_even_with_contract_text(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(1, 1),
            shortlist.CandidateDocumentCounts(1, 1),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="partial_preparation"),
            summary="Primary technical document is not processed.",
            document_download_was_run=True,
            document_readiness=shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
            steps={},
            errors=[],
        )

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={
                    "required": True,
                    "ready": False,
                    "reason": shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
                },
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_targeted_documents_before_llm(
                {
                    "external_id": "95748890",
                    "tender_id": "tender-id",
                    "_llm_category": "servers",
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
                },
                dry_run=False,
            )

        self.assertEqual(
            readiness.warning_reason,
            shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
        )
        self.assertEqual(readiness.llm_readiness, "not_ready_for_llm")

    def test_lead_report_allows_primary_pdf_without_text_after_extraction_retry(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(5, 4),
            shortlist.CandidateDocumentCounts(5, 4),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="partial_preparation"),
            summary="Primary technical PDF produced no text.",
            document_download_was_run=False,
            document_readiness=shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
            steps={"text_extraction": SimpleNamespace(attempted=True)},
            errors=["443737157_TZ.pdf: no_text_extracted"],
        )

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={
                    "required": True,
                    "ready": False,
                    "reason": shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
                },
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_lead_documents_before_llm(
                {
                    "external_id": "95499364",
                    "tender_id": "tender-id",
                    "_llm_category": "storage",
                },
                dry_run=False,
            )

        self.assertTrue(readiness.extraction_retry_triggered)
        self.assertFalse(readiness.targeted_download_triggered)
        self.assertIsNone(readiness.warning_reason)
        self.assertEqual(
            readiness.non_blocking_warning_reason,
            shortlist.PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
        )
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_primary_technical_doc_ready_after_preparation_allows_llm(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(1, 1),
            shortlist.CandidateDocumentCounts(2, 2),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="ready_for_llm"),
            summary="Tender is ready.",
            document_download_was_run=True,
            document_readiness=shortlist.DOCUMENTS_READY_WITH_TEXT,
            steps={},
            errors=[],
        )

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={
                    "required": True,
                    "ready": False,
                    "reason": shortlist.PRIMARY_TECHNICAL_DOCUMENT_MISSING,
                },
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_targeted_documents_before_llm(
                {
                    "external_id": "95748890",
                    "tender_id": "tender-id",
                    "_llm_category": "servers",
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
                },
                dry_run=False,
            )

        self.assertIsNone(readiness.warning_reason)
        self.assertEqual(readiness.document_state, shortlist.DOCUMENTS_READY_WITH_TEXT)
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_no_primary_title_keeps_section_level_ready_case_ready(self) -> None:
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("prepare should not run")
        )

        with (
            patch.object(
                shortlist,
                "document_counts_for_candidate",
                return_value=shortlist.CandidateDocumentCounts(1, 1),
            ),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={"required": False, "ready": True, "reason": None},
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_targeted_documents_before_llm(
                {
                    "external_id": "embedded-tz",
                    "tender_id": "tender-id",
                    "_llm_category": "servers",
                    "raw": {
                        "full": {
                            "documents": [
                                {"id": "kd", "title": "КД материалы.docx"}
                            ]
                        }
                    },
                },
                dry_run=False,
            )

        self.assertEqual(readiness.document_state, shortlist.DOCUMENTS_READY_WITH_TEXT)
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_lead_report_triggers_targeted_preparation_when_documents_missing(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(0, 0),
            shortlist.CandidateDocumentCounts(1, 1),
        ]
        prepare_calls = []
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")

        def prepare(**kwargs):
            prepare_calls.append(kwargs)
            return SimpleNamespace(
                status=SimpleNamespace(value="ready_for_llm"),
                summary="prepared",
                document_download_was_run=True,
                document_readiness=shortlist.DOCUMENTS_READY_WITH_TEXT,
                steps={},
                errors=[],
            )

        prepare_module.prepare_tender_for_analysis = prepare

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={"required": False, "ready": True, "reason": None},
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_lead_documents_before_llm(
                {"external_id": "lead-missing-docs"},
                dry_run=False,
            )

        self.assertEqual(len(prepare_calls), 1)
        self.assertTrue(readiness.targeted_download_triggered)
        self.assertEqual(readiness.docs_before, 0)
        self.assertEqual(readiness.docs_after, 1)
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_lead_report_retries_extraction_when_documents_have_no_text(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(2, 0),
            shortlist.CandidateDocumentCounts(2, 1),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: SimpleNamespace(
            status=SimpleNamespace(value="ready_for_llm"),
            summary="extracted",
            document_download_was_run=True,
            document_readiness=shortlist.DOCUMENTS_READY_WITH_TEXT,
            steps={"text_extraction": SimpleNamespace(attempted=True)},
            errors=[],
        )

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={"required": False, "ready": True, "reason": None},
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_lead_documents_before_llm(
                {"external_id": "lead-no-text"},
                dry_run=False,
            )

        self.assertTrue(readiness.extraction_retry_triggered)
        self.assertEqual(readiness.docs_with_text_before, 0)
        self.assertEqual(readiness.docs_with_text_after, 1)
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_lead_report_does_not_prepare_when_text_and_primary_doc_are_ready(self) -> None:
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("prepare should not run")
        )

        with (
            patch.object(
                shortlist,
                "document_counts_for_candidate",
                return_value=shortlist.CandidateDocumentCounts(1, 1),
            ),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={"required": True, "ready": True, "reason": None},
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_lead_documents_before_llm(
                {"external_id": "lead-ready"},
                dry_run=False,
            )

        self.assertFalse(readiness.targeted_download_triggered)
        self.assertEqual(readiness.preparation_status, "not_triggered")
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_lead_report_allows_card_only_after_failed_preparation_attempt(self) -> None:
        calls = [
            shortlist.CandidateDocumentCounts(0, 0),
            shortlist.CandidateDocumentCounts(0, 0),
        ]
        prepare_module = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
        prepare_module.prepare_tender_for_analysis = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("download failed")
        )

        with (
            patch.object(shortlist, "document_counts_for_candidate", side_effect=calls),
            patch.object(
                shortlist,
                "candidate_primary_technical_document_status",
                return_value={"required": False, "ready": True, "reason": None},
            ),
            patch.dict(sys.modules, {"app.pipeline.prepare_tender_for_analysis": prepare_module}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            readiness = shortlist.ensure_lead_documents_before_llm(
                {"external_id": "lead-card-after-attempt"},
                dry_run=False,
            )

        self.assertEqual(readiness.preparation_status, "failed")
        self.assertIsNone(readiness.warning_reason)
        self.assertEqual(
            readiness.non_blocking_warning_reason,
            "lead_documents_missing_allowed_after_prepare",
        )
        self.assertEqual(readiness.llm_readiness, "ready_for_llm")

    def test_document_not_ready_skips_llm_subprocess(self) -> None:
        row = {"external_id": "no-text", "title": "No text"}

        readiness = shortlist.CandidateDocumentReadiness(
            external_id="no-text",
            docs_before=2,
            docs_with_text_before=0,
            docs_after=2,
            docs_with_text_after=0,
            targeted_download_triggered=True,
            extraction_retry_triggered=True,
            preparation_status="documents_downloaded_but_no_text",
            document_state="documents_downloaded_without_text",
            warning_reason="documents_downloaded_without_text",
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run") as run_subprocess,
        ):
            with self.assertRaises(shortlist.DocumentNotReadyError):
                shortlist.run_llm_for_candidate(
                    row,
                    dry_run=False,
                    max_spec_chars=100,
                    max_other_chars=100,
                    max_output_tokens=100,
                    timeout_seconds=None,
                )

        run_subprocess.assert_not_called()

    def test_run_llm_for_candidate_defaults_to_deep_analysis_depth(self) -> None:
        row = {"external_id": "deep-default", "title": "Deep default"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="deep-default",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run_subprocess,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            shortlist.run_llm_for_candidate(
                row,
                dry_run=False,
                max_spec_chars=240000,
                max_other_chars=40000,
                max_output_tokens=16384,
                timeout_seconds=None,
            )

        args = run_subprocess.call_args.args[0]
        self.assertIn("--analysis-depth", args)
        self.assertEqual(args[args.index("--analysis-depth") + 1], "deep")
        self.assertIn("--max-output-tokens", args)
        self.assertEqual(args[args.index("--max-output-tokens") + 1], "16384")
        self.assertIn("--report-kind", args)
        self.assertEqual(args[args.index("--report-kind") + 1], "technical")

    def test_run_llm_for_candidate_passes_lead_report_kind(self) -> None:
        row = {"external_id": "lead-mode", "title": "Lead mode"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="lead-mode",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run_subprocess,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            shortlist.run_llm_for_candidate(
                row,
                dry_run=False,
                max_spec_chars=240000,
                max_other_chars=40000,
                max_output_tokens=16384,
                timeout_seconds=None,
                report_kind="lead",
            )

        args = run_subprocess.call_args.args[0]
        self.assertIn("--report-kind", args)
        self.assertEqual(args[args.index("--report-kind") + 1], "lead")
        self.assertIn("--document-readiness-json", args)
        metadata = json.loads(args[args.index("--document-readiness-json") + 1])
        self.assertEqual(metadata["external_id"], "lead-mode")
        self.assertEqual(metadata["docs_with_text_after"], 1)

    def test_run_llm_for_candidate_passes_deepseek_manual_override(self) -> None:
        row = {"external_id": "deepseek-override", "title": "DeepSeek override"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="deepseek-override",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run_subprocess,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            shortlist.run_llm_for_candidate(
                row,
                dry_run=False,
                max_spec_chars=240000,
                max_other_chars=40000,
                max_output_tokens=16384,
                timeout_seconds=None,
                provider="routerai",
                model="deepseek/deepseek-v4-pro",
            )

        args = run_subprocess.call_args.args[0]
        self.assertIn("--provider", args)
        self.assertEqual(args[args.index("--provider") + 1], "routerai")
        self.assertIn("--model", args)
        self.assertEqual(args[args.index("--model") + 1], "deepseek/deepseek-v4-pro")

    def test_run_llm_for_candidate_retries_transient_network_subprocess_once(self) -> None:
        row = {"external_id": "net-flaky", "title": "Network flaky"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="net-flaky",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )
        transient = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python", "-m", "app.llm.tender_report"],
            stderr="httpx.ConnectError: [Errno -3] Temporary failure in name resolution",
        )
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(
                shortlist.subprocess,
                "run",
                side_effect=[transient, subprocess.CompletedProcess([], 0, stdout="created\n", stderr="")],
            ) as run_subprocess,
            patch.object(shortlist.time, "sleep") as sleep,
            contextlib.redirect_stdout(stdout),
        ):
            result = shortlist.run_llm_for_candidate(
                row,
                dry_run=False,
                max_spec_chars=100,
                max_other_chars=100,
                max_output_tokens=100,
                timeout_seconds=None,
            )

        output = stdout.getvalue()
        self.assertEqual(result.external_id, "net-flaky")
        self.assertEqual(run_subprocess.call_count, 2)
        sleep.assert_called_once_with(10)
        self.assertIn("LLM candidate transient network failure; retrying", output)
        self.assertIn("external_id=net-flaky", output)
        self.assertIn("attempt=1", output)
        self.assertIn("created", output)
        self.assertEqual(shortlist.row_network_retry_counts(row), (1, 1, 0))

    def test_run_llm_for_candidate_does_not_retry_non_json_as_network(self) -> None:
        row = {"external_id": "bad-json", "title": "Bad JSON"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="bad-json",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )
        non_json = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python", "-m", "app.llm.tender_report"],
            stderr="RuntimeError: Cannot parse JSON from LLM response: not json",
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run", side_effect=non_json) as run_subprocess,
            patch.object(shortlist.time, "sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                shortlist.run_llm_for_candidate(
                    row,
                    dry_run=False,
                    max_spec_chars=100,
                    max_other_chars=100,
                    max_output_tokens=100,
                    timeout_seconds=None,
                )

        self.assertEqual(run_subprocess.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(shortlist.row_network_retry_counts(row), (0, 0, 0))

    def test_run_llm_for_candidate_does_not_retry_read_timeout(self) -> None:
        row = {"external_id": "read-timeout", "title": "Read timeout"}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="read-timeout",
            docs_before=1,
            docs_with_text_before=1,
            docs_after=1,
            docs_with_text_after=1,
            targeted_download_triggered=False,
            extraction_retry_triggered=False,
            preparation_status="not_triggered",
            document_state=shortlist.DOCUMENTS_READY_WITH_TEXT,
        )
        read_timeout = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python", "-m", "app.llm.tender_report"],
            stderr=(
                "httpx.ReadTimeout: read timed out\n"
                "RuntimeError: RouterAI read timeout. Request may still be running "
                "or charged upstream; not retrying automatically to avoid duplicate "
                "paid requests."
            ),
        )

        with (
            patch.object(shortlist, "ensure_targeted_documents_before_llm", return_value=readiness),
            patch.object(shortlist.subprocess, "run", side_effect=read_timeout) as run_subprocess,
            patch.object(shortlist.time, "sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                shortlist.run_llm_for_candidate(
                    row,
                    dry_run=False,
                    max_spec_chars=100,
                    max_other_chars=100,
                    max_output_tokens=100,
                    timeout_seconds=None,
                )

        failure = shortlist.describe_subprocess_failure("read-timeout", ctx.exception)
        self.assertEqual(failure.reason, "read_timeout")
        self.assertIn("RouterAI read timeout", failure.detail)
        self.assertEqual(run_subprocess.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(shortlist.row_network_retry_counts(row), (0, 0, 0))

    def test_summary_includes_network_retry_counters(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            shortlist.print_llm_summary(
                ["net-flaky"],
                [],
                network_retry_attempts=1,
                network_retry_succeeded=1,
                network_retry_failed=0,
                retry_external_ids=["net-flaky"],
            )

        output = stdout.getvalue()
        self.assertIn("- network_retry_attempts: 1", output)
        self.assertIn("- network_retry_succeeded: 1", output)
        self.assertIn("- network_retry_failed: 0", output)
        self.assertIn("- retry_external_ids: net-flaky", output)

    def test_summary_includes_read_timeout_not_retried(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            shortlist.print_llm_summary(
                [],
                [
                    shortlist.LLMCandidateFailure(
                        external_id="read-timeout",
                        reason="read_timeout",
                        detail="RouterAI read timeout",
                    )
                ],
            )

        output = stdout.getvalue()
        self.assertIn("- read_timeout_not_retried: 1", output)
        self.assertIn("- read_timeout_external_ids: read-timeout", output)
        self.assertIn("- failed_external_ids: read-timeout(read_timeout)", output)

    def test_empty_selection_prints_diagnostics(self) -> None:
        rows = [
            {"external_id": "no-go", "title": "No go", "price": 10},
            {
                "external_id": "done",
                "title": "Already done",
                "price": 20,
                "llm_report_result": {"report": {"summary": "done"}},
            },
        ]
        profile = {"llm_selection": {"default_limit": 2, "default_pool_limit": 2}}
        stdout = io.StringIO()

        def fake_effective(row: dict) -> str:
            return "no_go" if row["external_id"] == "no-go" else "go"

        with (
            patch.object(sys, "argv", ["run_llm_for_shortlist", "--profile", "stub.yaml"]),
            patch.object(shortlist, "load_business_profile", return_value=profile),
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", side_effect=fake_effective),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        self.assertIn("LLM shortlist selection diagnostics:", output)
        self.assertIn("- rule_based_rows: 2", output)
        self.assertIn("- rule_based_passed: 1", output)
        self.assertIn("- eligible_for_llm_before_limit: 0", output)
        self.assertIn("effective_recommendation=no_go=1", output)
        self.assertIn("already_has_llm_report=1", output)
        self.assertIn("No LLM candidates selected.", output)

    def test_category_thresholds_are_applied_per_llm_category(self) -> None:
        rows = [
            {
                "external_id": "server-medium",
                "title": "Поставка серверного оборудования",
                "price": 3_000_000,
                "score": 80,
            },
            {
                "external_id": "storage-below",
                "title": "Поставка СХД",
                "price": 4_000_000,
                "score": 90,
            },
            {
                "external_id": "infra-medium",
                "title": "Модернизация сети с поставкой коммутаторов",
                "price": 5_000_000,
                "score": 70,
            },
            {
                "external_id": "network-floor",
                "title": "Поставка коммутаторов",
                "price": 2_000_000,
                "score": 75,
            },
            {
                "external_id": "security-floor",
                "title": "Поставка межсетевого экрана",
                "price": 2_000_000,
                "score": 74,
            },
        ]
        profile = {
            "llm_selection": {
                "fallback_min_price_rub": 5_000_000,
                "category_min_price_rub": {
                    "storage": 5_000_000,
                    "servers": 3_000_000,
                    "network": 2_000_000,
                    "security_hardware": 2_000_000,
                    "infra_project": 5_000_000,
                },
            }
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        def fake_match(row: dict, profile: dict) -> tuple[str, dict]:
            if row["external_id"] == "server-medium":
                return "servers", {"label": "Servers"}
            if row["external_id"] == "infra-medium":
                return "network", {"label": "Network"}
            if row["external_id"] == "network-floor":
                return "network", {"label": "Network"}
            if row["external_id"] == "security-floor":
                return "security_hardware", {"label": "Security"}
            return "storage", {"label": "Storage"}

        def fake_assessment(row: dict) -> dict:
            if row["external_id"] == "infra-medium":
                return {"market_access": "infra_project"}
            return {"market_access": "target_hardware"}

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", side_effect=fake_assessment),
            patch.object(shortlist, "match_target_category", side_effect=fake_match),
        ):
            selected = shortlist.select_candidates(
                profile=profile,
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual(
            [row["external_id"] for row in selected],
            ["infra-medium", "server-medium", "network-floor", "security-floor"],
        )
        self.assertEqual(diagnostics.skip_counts["price_below_min_price=5000000"], 1)

    def test_latest_negative_sales_feedback_skips_paid_llm_candidate(self) -> None:
        rows = [
            {
                "external_id": "95759231",
                "title": "Negative feedback tender",
                "price": 30_000_000,
                "score": 95,
                "latest_sales_status": "sales_not_relevant",
            },
            {
                "external_id": "active",
                "title": "Active tender",
                "price": 30_000_000,
                "score": 90,
                "latest_sales_status": "sales_in_work",
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual([row["external_id"] for row in selected], ["active"])
        self.assertEqual(diagnostics.skip_counts["sales_feedback=sales_not_relevant"], 1)

    def test_transport_security_no_go_is_skipped_before_llm_shortlist(self) -> None:
        rows = [
            {
                "external_id": "0354100008426000068",
                "title": (
                    "Оснащение объектов дорожного хозяйства инженерно-техническими "
                    "средствами обеспечения транспортной безопасности"
                ),
                "price": 250_000_000,
                "score": 95,
            }
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="no_go"),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["effective_recommendation=no_go"], 1)

    def test_lead_mode_allows_strong_infra_signals_despite_technical_blockers(self) -> None:
        rows = [
            {
                "external_id": "modernization",
                "title": "Модернизация репозитория данных / ЦОМД Минздрава",
                "customer_name": "Минздрав",
                "price": 410_700_000,
                "score": 95,
                "documents_with_text": 0,
                "preparation_status": "no_valid_documents",
            },
            {
                "external_id": "support",
                "title": "Обеспечение работы серверов АО Россети Янтарь",
                "customer_name": "АО Россети Янтарь",
                "price": 2_270_000,
                "score": 90,
                "documents_with_text": 0,
                "preparation_blocked_event": json.dumps(
                    {"status": "blocked_by_marketplace_auth"}
                ),
            },
            {
                "external_id": "components",
                "title": "Компоненты ИТ-инфраструктуры / СХД",
                "price": 8_600_000,
                "score": 85,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="no_go"),
            patch.object(
                shortlist,
                "business_assessment",
                return_value={
                    "market_access": "domestic_restricted",
                    "action": "skip_low_priority",
                },
            ),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
            patch.object(shortlist, "is_low_priority_deal", return_value=True),
            patch.object(shortlist, "is_full_deal_for_category", return_value=False),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(
            [row["external_id"] for row in selected],
            ["modernization", "components", "support"],
        )
        self.assertNotIn("market_access=domestic_restricted", diagnostics.skip_counts)
        self.assertNotIn("not_full_deal_for_category", diagnostics.skip_counts)
        self.assertNotIn("low_priority_deal", diagnostics.skip_counts)
        self.assertNotIn("preparation_no_valid_documents", diagnostics.skip_counts)
        self.assertNotIn("preparation_blocked=marketplace_auth", diagnostics.skip_counts)

    def test_lead_mode_includes_expired_and_near_deadline_strong_leads(self) -> None:
        now = datetime(2026, 5, 10, 1, 2, tzinfo=timezone.utc)
        rows = [
            {
                "external_id": "expired",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 95,
                "deadline_at": now - timedelta(days=10),
            },
            {
                "external_id": "too-soon",
                "title": "Расширение существующей СХД",
                "price": 20_000_000,
                "score": 90,
                "deadline_at": now + timedelta(days=1),
            },
            {
                "external_id": "fresh",
                "title": "Поставка сетевого оборудования",
                "price": 10_000_000,
                "score": 85,
                "deadline_at": now + timedelta(days=10),
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
                now=now,
            )

        self.assertEqual([row["external_id"] for row in selected], ["expired", "too-soon", "fresh"])
        self.assertEqual(diagnostics.eligible_for_llm_before_limit, 3)
        self.assertNotIn("lead_deadline_expired", diagnostics.skip_counts)
        self.assertNotIn("lead_deadline_lt_3d", diagnostics.skip_counts)
        self.assertNotIn("deadline_expired", diagnostics.skip_counts)
        self.assertNotIn("deadline_lt_3d", diagnostics.skip_counts)
        output = stdout.getvalue()
        self.assertIn("lead_deadline_expired_allowed", output)
        self.assertIn("lead_deadline_lt_3d_allowed", output)

    def test_lead_selection_without_debug_skips_keeps_output_compact(self) -> None:
        rows = [
            {
                "external_id": "noise",
                "title": "Поставка камер видеонаблюдения",
                "price": 30_000_000,
                "score": 95,
            },
            {
                "external_id": "negative",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 90,
                "latest_sales_status": "sales_not_relevant",
            },
            {
                "external_id": "existing",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 88,
                "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                "llm_report_result": {"report": {"lead_summary": "existing"}},
            },
            {
                "external_id": "selected",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 85,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )
            shortlist.print_selection_diagnostics(diagnostics)

        output = stdout.getvalue()
        self.assertEqual([row["external_id"] for row in selected], ["selected"])
        self.assertNotIn("Pre-triage lead candidate", output)
        self.assertNotIn("Hard-noise gate", output)
        self.assertNotIn("Skip before lead triage", output)
        self.assertNotIn("Skip LLM candidate", output)
        self.assertIn("LLM shortlist selection diagnostics:", output)
        self.assertIn("Skip summary by reason:", output)
        self.assertIn("Hard-noise summary:", output)
        self.assertIn("Existing report skipped count:", output)
        self.assertIn("Negative feedback skipped count:", output)
        self.assertIn("- selected_for_lead_triage: 1", output)
        self.assertIn("lead_hard_noise=video_surveillance: 1", output)
        self.assertIn("lead_negative_feedback=sales_not_relevant: 1", output)
        self.assertIn("lead_already_has_report: 1", output)

    def test_debug_skips_keeps_old_style_detailed_lines(self) -> None:
        rows = [
            {
                "external_id": "noise",
                "title": "Поставка камер видеонаблюдения",
                "price": 30_000_000,
                "score": 95,
            }
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        output = stdout.getvalue()
        self.assertIn("Pre-triage lead candidate noise:", output)
        self.assertIn("Hard-noise gate noise:", output)
        self.assertIn("Skip LLM candidate noise:", output)
        self.assertIn("Skip before lead triage noise:", output)

    def test_debug_skips_limit_caps_details_but_keeps_full_counters(self) -> None:
        rows = [
            {
                "external_id": f"noise-{index}",
                "title": "Поставка камер видеонаблюдения",
                "price": 30_000_000,
                "score": 95 - index,
            }
            for index in range(3)
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        debug_limiter = shortlist.DebugSkipLimiter(limit=1)
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=debug_limiter,
                diagnostics=diagnostics,
                report_kind="lead",
            )
            shortlist.print_selection_diagnostics(diagnostics)
            shortlist.print_debug_skip_limit_summary(debug_limiter)

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.hard_noise_total, 3)
        self.assertEqual(diagnostics.hard_noise_strict_skipped, 3)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=video_surveillance"], 3)
        output = stdout.getvalue()
        self.assertEqual(output.count("Hard-noise gate"), 1)
        self.assertEqual(output.count("Skip before lead triage"), 1)
        self.assertIn("lead_hard_noise=video_surveillance: 3", output)
        self.assertIn("- debug_skips_limit: 1", output)
        self.assertIn("- debug_skip_lines_suppressed:", output)

    def test_waiting_go_triage_is_prioritized_before_new_high_price_candidate(self) -> None:
        waiting_go = {
            "external_id": "waiting-go",
            "title": "Поставка серверного оборудования",
            "customer_name": "Regional hospital",
            "price": 3_000_000,
            "score": 70,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "medium",
                "confidence": "high",
                "lead_summary": "Existing go triage waiting after previous limit",
                "likely_customer_story": "Server refresh",
                "possible_needs": ["servers"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        new_candidate = {
            "external_id": "new-high-price",
            "title": "Поставка серверного оборудования",
            "customer_name": "Another customer",
            "price": 90_000_000,
            "score": 99,
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[new_candidate, waiting_go]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([row["external_id"] for row in selected], ["waiting-go"])
        self.assertTrue(selected[0]["_lead_waiting_for_full_report"])
        output = stdout.getvalue()
        self.assertIn("Pre-triage lead candidate waiting-go: go_waiting_for_full_report", output)

    def test_lead_selection_reads_next_pool_page_after_first_page_is_filtered(self) -> None:
        reported = {
            "external_id": "reported",
            "title": "Existing lead report",
            "price": 100_000_000,
            "score": 99,
            "lead_llm_report_result": {"report": {"lead_summary": "done"}},
            "operational_lead_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
        }
        rejected_by_sales = {
            "external_id": "sales-rejected",
            "title": "Rejected by sales",
            "price": 90_000_000,
            "score": 98,
            "latest_sales_status": "sales_not_relevant",
        }
        new_candidate = {
            "external_id": "new-candidate",
            "title": "Поставка серверного оборудования",
            "price": 40_000_000,
            "score": 65,
        }
        pages = {
            0: [reported, rejected_by_sales],
            2: [new_candidate],
        }
        requested_offsets: list[int] = []

        def fake_get_digest_rows(*, limit: int, offset: int = 0) -> list[dict]:
            self.assertEqual(limit, 2)
            requested_offsets.append(offset)
            return pages.get(offset, [])

        with (
            patch.object(shortlist, "get_digest_rows", side_effect=fake_get_digest_rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=2,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                report_kind="lead",
            )

        self.assertEqual([row["external_id"] for row in selected], ["new-candidate"])
        self.assertEqual(requested_offsets, [0, 2])

    def test_fresh_non_actionable_triage_does_not_crowd_out_new_candidate(self) -> None:
        now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        fresh_reject = {
            "external_id": "fresh-reject",
            "title": "Previously rejected lead",
            "price": 100_000_000,
            "score": 99,
            "lead_triage_created_at": (now - timedelta(hours=1)).isoformat(),
            "lead_triage_result": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "reject_reason": "not a current lead",
                "requires_full_lead_report": False,
            },
        }
        new_candidate = {
            "external_id": "new-candidate",
            "title": "Поставка серверного оборудования",
            "price": 20_000_000,
            "score": 65,
        }

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[fresh_reject, new_candidate]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=2,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                now=now,
                report_kind="lead",
                lead_triage_cache_ttl_hours=24,
            )

        self.assertEqual([row["external_id"] for row in selected], ["new-candidate"])

    def test_stale_non_actionable_triage_returns_to_selection_after_ttl(self) -> None:
        now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        stale_reject = {
            "external_id": "stale-reject",
            "title": "Previously rejected server lead",
            "price": 30_000_000,
            "score": 65,
            "lead_triage_created_at": (now - timedelta(hours=25)).isoformat(),
            "lead_triage_result": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "reject_reason": "old decision",
                "requires_full_lead_report": False,
            },
        }

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[stale_reject]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=1,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                now=now,
                report_kind="lead",
                lead_triage_cache_ttl_hours=24,
            )

        self.assertEqual([row["external_id"] for row in selected], ["stale-reject"])

    def test_lead_selection_uses_complete_empty_sql_snapshots_without_db_fallback(self) -> None:
        row = {
            "tender_id": "tender-1",
            "external_id": "new-candidate",
            "title": "Поставка серверного оборудования",
            "price": 20_000_000,
            "score": 65,
            "lead_triage_result": None,
            "lead_triage_analysis_type": None,
            "lead_llm_report_result": None,
            "operational_lead_report_analysis_type": None,
        }

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate") as triage_db_lookup,
            patch.object(shortlist, "analysis_result_exists_for_candidate") as report_db_lookup,
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=2,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                report_kind="lead",
            )

        self.assertEqual([item["external_id"] for item in selected], ["new-candidate"])
        triage_db_lookup.assert_not_called()
        report_db_lookup.assert_not_called()

    def test_lead_storage_signal_beats_construction_noise_for_95790607_like_title(self) -> None:
        row = {
            "external_id": "95790607",
            "title": (
                "Поставка комплекта модернизации репозитория данных для развития центра "
                "хранения и обработки медицинских данных для учреждений, "
                "подведомственных Минздраву России (ЦОМД)"
            ),
            "customer_name": "Минздрав России",
            "price": 410_700_000,
            "raw": {"note": "строительные требования к помещению заказчика"},
        }
        assessment = {"market_access": "open"}
        corpus = shortlist.lead_signal_corpus(row, assessment)

        self.assertEqual(shortlist.classify_lead_signal(row, assessment), shortlist.LEAD_SIGNAL_STRONG)
        self.assertIsNone(shortlist.lead_noise_reason_from_corpus(corpus))

    def test_95790607_like_title_is_not_hard_noise_and_can_go_to_triage(self) -> None:
        row = {
            "external_id": "95790607",
            "title": (
                "РџРѕСЃС‚Р°РІРєР° РєРѕРјРїР»РµРєС‚Р° РјРѕРґРµСЂРЅРёР·Р°С†РёРё СЂРµРїРѕР·РёС‚РѕСЂРёСЏ РґР°РЅРЅС‹С… РґР»СЏ СЂР°Р·РІРёС‚РёСЏ С†РµРЅС‚СЂР° "
                "С…СЂР°РЅРµРЅРёСЏ Рё РѕР±СЂР°Р±РѕС‚РєРё РјРµРґРёС†РёРЅСЃРєРёС… РґР°РЅРЅС‹С…"
            ),
            "customer_name": "РњРёРЅР·РґСЂР°РІ Р РѕСЃСЃРёРё",
            "price": 410_700_000,
            "score": 95,
            "raw": {"note": "СЃС‚СЂРѕРёС‚РµР»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹ РІ РїРѕРјРµС‰РµРЅРёРё"},
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertIsNone(shortlist.lead_hard_noise_reason_for_row(row, {"market_access": "open"}))
        self.assertEqual([item["external_id"] for item in selected], ["95790607"])

    def test_95800176_like_server_network_equipment_can_go_to_triage(self) -> None:
        row = {
            "external_id": "95800176",
            "title": "РџРѕСЃС‚Р°РІРєР° Рё РџРќР  СЃРµСЂРІРµСЂРЅРѕРіРѕ Рё СЃРµС‚РµРІРѕРіРѕ РѕР±РѕСЂСѓРґРѕРІР°РЅРёСЏ",
            "price": 30_000_000,
            "score": 90,
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertIsNone(
            shortlist.lead_hard_noise_reason_for_row(
                row,
                {"market_access": "target_hardware"},
            )
        )
        self.assertEqual([item["external_id"] for item in selected], ["95800176"])

    def test_generic_application_development_is_not_deterministic_strong_and_can_be_rejected_by_triage(self) -> None:
        row = {
            "external_id": "ais",
            "title": "Development and support of AIS application system",
            "customer_name": "Regional customer",
            "price": 20_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "report": {
                    "lead_decision": "reject",
                    "lead_priority": "low",
                    "confidence": "high",
                    "lead_summary": "",
                    "likely_customer_story": "",
                    "possible_needs": [],
                    "target_roles": [],
                    "reject_reason": "application development without infrastructure story",
                    "requires_full_lead_report": False,
                }
            },
        }
        stdout = io.StringIO()

        self.assertNotEqual(shortlist.classify_lead_signal(row), shortlist.LEAD_SIGNAL_STRONG)
        with contextlib.redirect_stdout(stdout):
            selected = shortlist.apply_lead_triage(
                [row],
                dry_run=True,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=5,
            )

        self.assertEqual(selected, [])
        output = stdout.getvalue()
        self.assertIn("lead_triage_decision=reject", output)
        self.assertIn("lead_triage_priority=low", output)
        self.assertIn("selected_for_lead_report=false", output)
        self.assertIn("skipped_by_lead_triage=true", output)
        self.assertIn("reject_reason=application development without infrastructure story", output)
        self.assertIn("lead_triage_reject_reason=application development without infrastructure story", output)
        self.assertNotIn("lead_triagereject_reason", output)
        self.assertIn("skipped_by_lead_triage", output)

    def test_normalize_lead_triage_keeps_sql_friendly_top_level_fields(self) -> None:
        normalized = shortlist.normalize_lead_triage(
            {
                "lead_decision": "no_go",
                "lead_priority": "low",
                "confidence": "high",
                "customer_story": "Legacy customer story",
                "why_call": "Legacy call reason",
                "likely_needs": ["backup"],
                "target_roles": ["IT owner"],
                "reject_reason": "not profile",
                "requires_full_lead_report": True,
            }
        )

        self.assertEqual(tuple(normalized), shortlist.LEAD_TRIAGE_FIELDS)
        self.assertEqual(normalized["lead_decision"], "reject")
        self.assertEqual(normalized["lead_summary"], "Legacy customer story")
        self.assertEqual(normalized["likely_customer_story"], "Legacy customer story")
        self.assertEqual(normalized["possible_needs"], ["backup"])
        self.assertEqual(normalized["target_roles"], ["IT owner"])
        self.assertEqual(normalized["reject_reason"], "not profile")
        self.assertFalse(normalized["requires_full_lead_report"])

    def test_normalize_lead_triage_cleans_non_reject_reasons(self) -> None:
        go = shortlist.normalize_lead_triage(
            {
                "lead_decision": "go",
                "lead_priority": "high",
                "reject_reason": "Не применимо",
                "requires_full_lead_report": True,
            }
        )
        maybe = shortlist.normalize_lead_triage(
            {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "reject_reason": "Не применимо",
                "requires_full_lead_report": True,
            }
        )
        maybe_with_reason = shortlist.normalize_lead_triage(
            {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "reject_reason": "недостаточно подтвержден инфраструктурный владелец",
                "requires_full_lead_report": True,
            }
        )

        self.assertEqual(go["reject_reason"], "")
        self.assertEqual(maybe["reject_reason"], "")
        self.assertEqual(
            maybe_with_reason["reject_reason"],
            "недостаточно подтвержден инфраструктурный владелец",
        )

    def test_lead_triage_prompt_prioritizes_account_development_reason(self) -> None:
        prompt = shortlist.build_lead_triage_user_prompt("Tender context")

        self.assertIn("commercial account-development reason", prompt)
        self.assertIn("not whether the text merely contains words", prompt)
        self.assertIn("Deadline is warning-only in lead mode", prompt)
        self.assertIn("Price is a priority signal, not a hard ban", prompt)
        self.assertIn("ЦОД/ОЦОД/РЦОД/ЦОМД", prompt)
        self.assertIn("virtualization, containerization, backup, DR", prompt)
        self.assertIn("installed base or future infrastructure tasks", prompt)
        self.assertIn("one server or a small supply", prompt)
        self.assertIn("prefer go with medium priority over maybe_deferred", prompt)
        self.assertIn("repair, maintenance, peripherals", prompt)
        self.assertIn("video surveillance", prompt)
        self.assertIn("latest negative feedback", prompt)

    def test_lead_building_or_furniture_without_it_terms_is_noise(self) -> None:
        row = {
            "external_id": "furniture",
            "title": "Поставка мебели и ремонт помещений",
            "price": 10_000_000,
        }
        corpus = shortlist.lead_signal_corpus(row, {"market_access": "open"})

        self.assertEqual(shortlist.classify_lead_signal(row), shortlist.LEAD_SIGNAL_NOISE)
        self.assertEqual(shortlist.lead_noise_reason_from_corpus(corpus), "construction_or_furniture")

    def test_lead_classifier_blocks_explicit_noise_before_broad_it_words(self) -> None:
        cases = [
            (
                "video",
                "Приобретение и монтаж систем видеонаблюдения на сервере",
                "video_surveillance",
            ),
            (
                "video-pak",
                "ИКТ. Поставка программно-аппаратного комплекса для видеонаблюдения",
                "video_surveillance",
            ),
            (
                "video-equipment",
                "Поставка и монтаж оборудования для системы видеонаблюдения",
                "video_surveillance",
            ),
            (
                "safe-city",
                "Комплексная система видеонаблюдения АПК Безопасный город",
                "video_surveillance",
            ),
            (
                "queue",
                "ПАК системы управления электронной очередью",
                "electronic_queue",
            ),
            (
                "queue-system",
                "Система управления электронной очередью",
                "electronic_queue",
            ),
            (
                "office",
                "Поставка оргтехники и запасных частей для вычислительной техники",
                "peripheral_office_equipment",
            ),
            (
                "office-repair-server",
                "Оказание услуг по ремонту компьютерной техники, оргтехники, МФУ и серверов",
                "peripheral_office_equipment",
            ),
            (
                "fap",
                "Строительство ФАП и некапитального строения",
                "construction_or_furniture",
            ),
            (
                "road",
                "Оборудование дорожных объектов транспортной безопасности мостов и путепроводов",
                "road_security",
            ),
            (
                "95480005",
                (
                    "Оказание услуг по обеспечению работы комплексов автоматической "
                    "фотовидеофиксации административных правонарушений в области "
                    "дорожного движения"
                ),
                "road_security",
            ),
            (
                "mesh-sale",
                "Продажа. Маршрутизатор MESH",
                "property_sale",
            ),
        ]

        for external_id, title, reason in cases:
            with self.subTest(external_id=external_id):
                row = {
                    "external_id": external_id,
                    "title": title,
                    "price": 20_000_000,
                }
                corpus = shortlist.lead_signal_corpus(row, {"market_access": "target_hardware"})

                self.assertEqual(shortlist.lead_noise_reason_from_corpus(corpus), reason)
                self.assertEqual(
                    shortlist.classify_lead_signal(row, {"market_access": "target_hardware"}),
                    shortlist.LEAD_SIGNAL_NOISE,
                )

    def test_95480005_like_photo_fixation_is_hard_noise_before_triage(self) -> None:
        row = {
            "external_id": "95480005",
            "title": (
                "Оказание услуг по обеспечению работы комплексов автоматической "
                "фотовидеофиксации административных правонарушений в области "
                "дорожного движения"
            ),
            "price": 28_000_000,
            "score": 95,
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(
            shortlist.lead_hard_noise_reason_for_row(row, {"market_access": "target_hardware"}),
            "road_security",
        )
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=road_security"], 1)
        self.assertEqual(diagnostics.eligible_for_llm_before_limit, 0)

    def test_video_surveillance_server_drives_are_hard_noise_before_triage(self) -> None:
        row = {
            "external_id": "video-drives",
            "title": "Жёсткие диски для серверов системы видеонаблюдения заказчика",
            "price": 9_000_000,
            "score": 90,
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(
            shortlist.lead_hard_noise_reason_for_row(row, {"market_access": "target_hardware"}),
            "video_surveillance",
        )
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=video_surveillance"], 1)
        self.assertEqual(diagnostics.eligible_for_llm_before_limit, 0)

    def test_pure_hard_noise_stays_strict_skipped_before_triage(self) -> None:
        rows = [
            {
                "external_id": "pure-video",
                "title": "Поставка камер видеонаблюдения и монтаж системы видеонаблюдения",
                "price": 12_000_000,
                "score": 90,
            },
            {
                "external_id": "pure-road",
                "title": "Комплексы фотовидеофиксации дорожного движения и весогабаритный контроль",
                "price": 18_000_000,
                "score": 89,
            },
            {
                "external_id": "pure-office",
                "title": "Поставка МФУ, принтеров, сканеров и оргтехники",
                "price": 7_000_000,
                "score": 88,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.hard_noise_total, 3)
        self.assertEqual(diagnostics.hard_noise_strict_skipped, 3)
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 0)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=video_surveillance"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=road_security"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=peripheral_office_equipment"], 1)

    def test_smoke_false_positive_overrides_remain_strict_skipped(self) -> None:
        rows = [
            {
                "external_id": "95499423",
                "title": "Приобретение и монтаж систем видеонаблюдения",
                "price": 12_000_000,
                "score": 95,
                "raw": {
                    "description": (
                        "сервер системы видеонаблюдения, коммутаторы, "
                        "сетевое оборудование, пусконаладка"
                    )
                },
            },
            {
                "external_id": "95808320",
                "title": "ПАК системы управления электронной очередью",
                "price": 9_000_000,
                "score": 94,
                "raw": {
                    "description": "поставка сервера электронной очереди и сетевое оборудование"
                },
            },
            {
                "external_id": "95340355",
                "title": "Поставка оргтехники и запасных частей для вычислительной техники",
                "price": 8_000_000,
                "score": 93,
                "raw": {
                    "description": (
                        "коммутаторы, сетевое оборудование, Astra Linux и "
                        "предустановленное ПО для компьютерной техники"
                    )
                },
            },
            {
                "external_id": "96088613",
                "title": "Поставка комплектующих для оргтехники и вычислительной техники",
                "price": 7_500_000,
                "score": 92,
                "raw": {
                    "description": "маршрутизаторы, сетевой монтаж, компьютерная техника"
                },
            },
            {
                "external_id": "96087372",
                "title": (
                    "Оказание услуг по содержанию и обслуживанию комплексов "
                    "автоматического весогабаритного контроля"
                ),
                "price": 15_000_000,
                "score": 91,
                "raw": {
                    "description": (
                        "сервер системы весогабаритного контроля, система хранения "
                        "данных фотофиксации, модернизация ИБ подсистемы Архимед"
                    )
                },
            },
            {
                "external_id": "95934325",
                "title": "Поставка приборов, аппаратуры и устройств учебно-демонстрационных прочих",
                "price": 6_000_000,
                "score": 90,
                "raw": {"description": "сетевое оборудование, коммутаторы, учебный класс"},
            },
            {
                "external_id": "95813981",
                "title": "Поставка приборов, аппаратуры и устройств учебно-демонстрационных прочих",
                "price": 6_500_000,
                "score": 89,
                "raw": {
                    "description": (
                        "Сетевое оборудование для интерактивного учебного класса, "
                        "приборы микроскопии"
                    )
                },
            },
            {
                "external_id": "95600679",
                "title": "Поставка лицензий Kaspersky для защиты информации почтовых серверов",
                "price": 9_500_000,
                "score": 88,
                "raw": {
                    "description": (
                        "антивирусная защита рабочих мест и почтовые серверы, "
                        "продление лицензий"
                    )
                },
            },
            {
                "external_id": "95510702",
                "title": "Продление Kaspersky Security для почтовых серверов",
                "price": 9_000_000,
                "score": 87,
                "raw": {
                    "description": (
                        "generic antivirus license for mail server protection, "
                        "продление подписки"
                    )
                },
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=15,
                pool_limit=15,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.hard_noise_strict_skipped, len(rows))
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 0)
        output = stdout.getvalue()
        self.assertIn("hard_noise_reason=video_surveillance", output)
        self.assertIn("hard_noise_reason=electronic_queue", output)
        self.assertIn("hard_noise_reason=peripheral_office_equipment", output)
        self.assertIn("hard_noise_reason=road_security", output)
        self.assertIn("hard_noise_reason=generic_antivirus_license", output)
        self.assertIn("final_decision=strict_skip", output)

    def test_target_signal_overrides_hard_noise_to_paid_triage(self) -> None:
        rows = [
            {
                "external_id": "95470865",
                "title": "ПАК для защиты информации и мониторинга инцидентов ИБ",
                "price": 34_900_000,
                "score": 93,
                "raw": {"note": "строительные работы в помещении"},
            },
            {
                "external_id": "server-storage",
                "title": "Поставка серверного оборудования и СХД для инфраструктуры DLP",
                "price": 14_100_000,
                "score": 92,
                "raw": {"note": "периферийное оборудование вычислительной техники"},
            },
            {
                "external_id": "96272875",
                "title": "Миграция Exchange на Astra Linux и RuPost",
                "price": 19_900_000,
                "score": 91,
                "raw": {"note": "продление антивирусных лицензий"},
            },
            {
                "external_id": "enterprise-infra",
                "title": "Модернизация ИТ-инфраструктуры федеральной площадки",
                "price": 24_000_000,
                "score": 90,
                "raw": {"note": "ремонт помещений и монтажные работы"},
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(
            {row["external_id"] for row in selected},
            {"95470865", "server-storage", "96272875", "enterprise-infra"},
        )
        self.assertIsNone(shortlist.lead_hard_noise_reason_for_row(rows[0], {"market_access": "target_hardware"}))
        self.assertIsNone(shortlist.lead_hard_noise_reason_for_row(rows[1], {"market_access": "target_hardware"}))
        self.assertIsNone(shortlist.lead_hard_noise_reason_for_row(rows[2], {"market_access": "target_hardware"}))
        self.assertIsNone(shortlist.lead_hard_noise_reason_for_row(rows[3], {"market_access": "target_hardware"}))
        self.assertEqual(diagnostics.hard_noise_total, 4)
        self.assertEqual(diagnostics.hard_noise_strict_skipped, 0)
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 4)
        output = stdout.getvalue()
        self.assertIn("hard_noise_reason=construction_or_furniture", output)
        self.assertIn("hard_noise_reason=peripheral_office_equipment", output)
        self.assertIn("hard_noise_reason=generic_antivirus_license", output)
        self.assertIn("final_decision=override_to_triage", output)

    def test_existing_go_waiting_overrides_hard_noise_before_triage(self) -> None:
        row = {
            "external_id": "96261999",
            "title": (
                "Выполнение работ по приведению в соответствие требованиям по защите "
                "информации объектов информатизации и мониторингу инцидентов ИБ"
            ),
            "customer_name": "РОССТАТ",
            "price": 115_827_380,
            "score": 95,
            "raw": {"note": "периферийное оборудование вычислительной техники"},
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "high",
                "lead_summary": "ИБ-инфраструктура федерального заказчика",
                "likely_customer_story": "Information security modernization",
                "possible_needs": ["ИБ", "SOC"],
                "target_roles": ["CISO"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([item["external_id"] for item in selected], ["96261999"])
        self.assertTrue(selected[0]["_lead_waiting_for_full_report"])
        self.assertEqual(diagnostics.hard_noise_total, 1)
        self.assertEqual(diagnostics.hard_noise_overridden_by_existing_go, 1)
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 0)
        output = stdout.getvalue()
        self.assertIn("hard_noise_reason=peripheral_office_equipment", output)
        self.assertIn("target_signal_matched=security", output)
        self.assertIn("final_decision=existing_go_waiting", output)

    def test_negative_feedback_wins_over_target_signal_override(self) -> None:
        row = {
            "external_id": "negative-target",
            "title": "ПАК для защиты информации и мониторинга инцидентов ИБ",
            "price": 34_900_000,
            "score": 93,
            "raw": {"note": "строительные работы"},
            "latest_sales_status": "not_profile",
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["lead_negative_feedback=not_profile"], 1)
        self.assertEqual(diagnostics.hard_noise_total, 1)
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 0)
        self.assertIn("final_decision=skipped_by_feedback", stdout.getvalue())

    def test_existing_full_report_wins_over_target_signal_override(self) -> None:
        row = {
            "external_id": "96261999-existing-report",
            "title": (
                "Выполнение работ по приведению в соответствие требованиям по защите "
                "информации объектов информатизации и мониторингу инцидентов ИБ"
            ),
            "price": 115_827_380,
            "score": 93,
            "raw": {"note": "периферийное оборудование вычислительной техники"},
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
        }
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["lead_already_has_report"], 1)
        self.assertEqual(diagnostics.hard_noise_total, 1)
        self.assertEqual(diagnostics.hard_noise_overridden_by_target_signal, 0)
        self.assertIn("final_decision=skipped_by_existing_report", stdout.getvalue())

    def test_lead_classifier_does_not_treat_generic_antivirus_as_strong(self) -> None:
        row = {
            "external_id": "antivirus",
            "title": "Поставка лицензий Kaspersky для антивирусной защиты рабочих мест",
            "price": 5_000_000,
        }
        corpus = shortlist.lead_signal_corpus(row, {"market_access": "target_hardware"})

        self.assertEqual(
            shortlist.lead_noise_reason_from_corpus(corpus),
            "generic_antivirus_license",
        )
        self.assertNotEqual(
            shortlist.classify_lead_signal(row, {"market_access": "target_hardware"}),
            shortlist.LEAD_SIGNAL_STRONG,
        )

    def test_road_security_does_not_match_clean_server_or_storage_titles(self) -> None:
        titles = [
            "Поставка сервера",
            "Поставка серверов",
            "Поставка серверного оборудования",
            "Поставка комплекта модернизации DEPO",
            "Серверы, системный блок, источник бесперебойного питания",
            "Поставка системы хранения данных для ТФОМС",
            "Техническая поддержка серверной инфраструктуры Роскадастра",
        ]

        for title in titles:
            with self.subTest(title=title):
                row = {
                    "external_id": "clean",
                    "title": title,
                    "price": 30_000_000,
                }
                corpus = shortlist.lead_signal_corpus(row, {"market_access": "target_hardware"})

                self.assertNotEqual(
                    shortlist.lead_noise_reason_from_corpus(corpus),
                    "road_security",
                )
                self.assertFalse(shortlist.lead_has_road_security_noise(corpus))

    def test_road_security_matches_explicit_road_bridge_transport_security_titles(self) -> None:
        titles = [
            "Поставка оборудования для автомобильной дороги",
            "Оборудование мостов и путепроводов рубежами контроля",
            "Аппаратно-программный комплекс транспортной безопасности",
            "Фотовидеофиксация дорожного движения и весогабаритный контроль",
            "Комплексы автоматической фотовидеофиксации административных правонарушений",
        ]

        for title in titles:
            with self.subTest(title=title):
                row = {
                    "external_id": "road",
                    "title": title,
                    "price": 30_000_000,
                }
                corpus = shortlist.lead_signal_corpus(row, {"market_access": "target_hardware"})

                self.assertEqual(shortlist.lead_noise_reason_from_corpus(corpus), "road_security")
                self.assertEqual(
                    shortlist.classify_lead_signal(row, {"market_access": "target_hardware"}),
                    shortlist.LEAD_SIGNAL_NOISE,
                )

    def test_lead_classifier_keeps_core_infrastructure_examples_strong(self) -> None:
        cases = [
            (
                "95793813",
                "Расширение существующей системы хранения данных",
            ),
            (
                "95800176",
                "Поставка и ПНР серверного и сетевого оборудования",
            ),
            (
                "95799957",
                "Оборудование ИТ-инфраструктуры: накопители, кабели, дисковые полки, жесткие диски",
            ),
            (
                "95807988",
                "Обеспечение работы серверов АО Россети Янтарь",
            ),
            (
                "95732885",
                "Система хранения данных",
            ),
            (
                "95682620",
                "ПАК виртуализации и СХД",
            ),
            (
                "95774082",
                "Поставка серверов для центра обработки данных",
            ),
            (
                "clean-rcod",
                "Модернизация РЦОД: серверы, СХД и платформа виртуализации",
            ),
        ]

        for external_id, title in cases:
            with self.subTest(external_id=external_id):
                row = {
                    "external_id": external_id,
                    "title": title,
                    "price": 30_000_000,
                }

                self.assertEqual(shortlist.classify_lead_signal(row), shortlist.LEAD_SIGNAL_STRONG)

    def test_lead_mode_blocks_noise_categories(self) -> None:
        rows = [
            {
                "external_id": "video",
                "title": "Поставка системы видеонаблюдения",
                "price": 20_000_000,
                "score": 90,
            },
            {
                "external_id": "queue",
                "title": "Электронная очередь для учреждения",
                "price": 5_000_000,
                "score": 89,
            },
            {
                "external_id": "mfp",
                "title": "Поставка МФУ и оргтехники",
                "price": 6_000_000,
                "score": 88,
            },
            {
                "external_id": "fap",
                "title": "Строительство ФАП",
                "price": 25_000_000,
                "score": 86,
            },
            {
                "external_id": "road",
                "title": "Оборудование дорожных объектов транспортной безопасности мостов",
                "price": 15_000_000,
                "score": 85,
            },
            {
                "external_id": "mesh-sale",
                "title": "Продажа. Маршрутизатор MESH",
                "price": 2_000_000,
                "score": 84,
            },
            {
                "external_id": "antivirus",
                "title": "Поставка антивирусных лицензий Kaspersky",
                "price": 3_000_000,
                "score": 83,
            },
            {
                "external_id": "server",
                "title": "Поставка серверного оборудования",
                "price": 6_000_000,
                "score": 87,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([row["external_id"] for row in selected], ["server"])
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=video_surveillance"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=electronic_queue"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=peripheral_office_equipment"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=construction_or_furniture"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=road_security"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=property_sale"], 1)
        self.assertEqual(diagnostics.skip_counts["lead_hard_noise=generic_antivirus_license"], 1)

    def test_lead_mode_blocks_negative_sales_feedback(self) -> None:
        rows = [
            {
                "external_id": "negative",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 95,
                "latest_sales_status": "sales_not_relevant",
            },
            {
                "external_id": "active",
                "title": "Поставка серверного оборудования",
                "price": 30_000_000,
                "score": 90,
                "latest_sales_status": "sales_in_work",
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="no_go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([row["external_id"] for row in selected], ["active"])
        self.assertEqual(diagnostics.skip_counts["lead_negative_feedback=sales_not_relevant"], 1)

    def test_negative_sales_feedback_statuses_do_not_reach_paid_lead_triage(self) -> None:
        statuses = tuple(sorted(shortlist.NEGATIVE_SALES_FEEDBACK_SKIP_STATUSES))
        rows = [
            {
                "external_id": f"negative-{status}",
                "title": "Server infrastructure",
                "price": 30_000_000,
                "score": 95,
                "latest_sales_status": status,
            }
            for status in statuses
        ]
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 5}}),
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_not_called()
        run_report.assert_not_called()
        output = stdout.getvalue()
        for status in statuses:
            self.assertIn(f"lead_negative_feedback={status}=1", output)

    def test_negative_feedback_blocks_same_tender_not_same_customer(self) -> None:
        rows = [
            {
                "external_id": "same-customer-rejected",
                "title": "Поставка серверного оборудования",
                "customer_name": "Same Customer",
                "price": 20_000_000,
                "score": 90,
                "latest_sales_status": "sales_not_relevant",
            },
            {
                "external_id": "same-customer-new",
                "title": "Поставка серверного оборудования",
                "customer_name": "Same Customer",
                "price": 15_000_000,
                "score": 85,
            },
            {
                "external_id": "same-customer-in-work",
                "title": "Поставка серверного оборудования",
                "customer_name": "Same Customer",
                "price": 12_000_000,
                "score": 80,
                "latest_sales_status": "sales_in_work",
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(
            [row["external_id"] for row in selected],
            ["same-customer-new", "same-customer-in-work"],
        )
        self.assertEqual(
            diagnostics.skip_counts["lead_negative_feedback=sales_not_relevant"],
            1,
        )

    def test_existing_client_customer_marker_blocks_same_customer_lead_triage(self) -> None:
        rows = [
            {
                "external_id": "same-customer-new",
                "title": "Поставка серверного оборудования",
                "customer_name": "Same Customer",
                "price": 15_000_000,
                "score": 85,
                "existing_client_customer_status": "sales_existing_client",
                "existing_client_source_tender_id": "source-tender",
            },
            {
                "external_id": "other-customer-new",
                "title": "Поставка серверного оборудования",
                "customer_name": "Other Customer",
                "price": 12_000_000,
                "score": 80,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([row["external_id"] for row in selected], ["other-customer-new"])
        self.assertEqual(
            diagnostics.skip_counts["lead_negative_feedback=sales_existing_client"],
            1,
        )

    def test_lead_report_presence_is_separate_from_technical_and_smoke_reports(self) -> None:
        rows = [
            {
                "external_id": "technical-done",
                "title": "Поставка серверного оборудования",
                "price": 10_000_000,
                "score": 90,
                "llm_report_result": {"report": {"summary": "technical"}},
            },
            {
                "external_id": "lead-smoke",
                "title": "Поставка серверного оборудования",
                "price": 9_000_000,
                "score": 89,
                "llm_report_analysis_type": "llm_customer_lead_report_smoke",
                "llm_report_result": {"report": {"lead_summary": "smoke"}},
            },
            {
                "external_id": "lead-manual-label",
                "title": "Поставка серверного оборудования",
                "price": 7_000_000,
                "score": 87,
                "llm_report_analysis_type": "llm_customer_lead_report_manual_test",
                "llm_report_result": {"report": {"lead_summary": "manual"}},
            },
            {
                "external_id": "lead-v3-label",
                "title": "Поставка серверного оборудования",
                "price": 6_000_000,
                "score": 86,
                "llm_report_analysis_type": "llm_customer_lead_report_lead_v3_test",
                "llm_report_result": {"report": {"lead_summary": "lead-v3"}},
            },
            {
                "external_id": "lead-done",
                "title": "Поставка серверного оборудования",
                "price": 8_000_000,
                "score": 88,
                "llm_report_analysis_type": "llm_customer_lead_report",
                "llm_report_result": {"report": {"lead_summary": "done"}},
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual(
            [row["external_id"] for row in selected],
            ["technical-done", "lead-smoke", "lead-manual-label", "lead-v3-label"],
        )
        self.assertEqual(diagnostics.skip_counts["lead_already_has_report"], 1)

    def test_existing_report_lookup_does_not_use_external_id_only_unit_rows(self) -> None:
        fake_psycopg = types.ModuleType("psycopg")
        executed: list[tuple[str, tuple[str, str]]] = []

        class FakeCursor:
            def execute(self, query: str, params: tuple[str, str]) -> None:
                executed.append((query, params))

            def fetchone(self) -> tuple[dict[str, object]] | None:
                return ({"report": {"lead_summary": "ambient production report"}},)

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        class FakeConnection:
            def cursor(self) -> FakeCursor:
                return FakeCursor()

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        fake_psycopg.connect = lambda database_url: FakeConnection()
        fake_config = types.ModuleType("app.config")
        fake_config.settings = SimpleNamespace(database_url="postgres://ambient")

        with patch.dict(
            sys.modules,
            {"psycopg": fake_psycopg, "app.config": fake_config},
            clear=False,
        ):
            external_id_only_exists = shortlist.analysis_result_exists_for_candidate(
                {"external_id": "95790607"},
                shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            )
            synthetic_db_like_external_id_exists = shortlist.analysis_result_exists_for_candidate(
                {
                    "external_id": "95790607",
                    "score": 95,
                    "raw": {"note": "synthetic unit row"},
                },
                shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            )
            tender_id_exists = shortlist.analysis_result_exists_for_candidate(
                {"tender_id": "tender-95790607", "external_id": "95790607"},
                shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            )
            final_guard_external_id_exists = shortlist.analysis_result_exists_for_candidate(
                {"external_id": "95790607"},
                shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
                allow_external_id_fallback=True,
            )

        self.assertFalse(external_id_only_exists)
        self.assertFalse(synthetic_db_like_external_id_exists)
        self.assertTrue(tender_id_exists)
        self.assertTrue(final_guard_external_id_exists)
        self.assertEqual(len(executed), 2)
        self.assertIn("tender_id = %s", executed[0][0])
        self.assertIn("external_id = %s", executed[1][0])

    def test_lead_selection_keeps_synthetic_external_id_row_when_ambient_report_exists(self) -> None:
        row = {
            "external_id": "95790607",
            "title": (
                "Р СџР С•РЎРѓРЎвЂљР В°Р Р†Р С”Р В° Р С”Р С•Р СР С—Р В»Р ВµР С”РЎвЂљР В° Р СР С•Р Т‘Р ВµРЎР‚Р Р…Р С‘Р В·Р В°РЎвЂ Р С‘Р С‘ "
                "РЎР‚Р ВµР С—Р С•Р В·Р С‘РЎвЂљР С•РЎР‚Р С‘РЎРЏ Р Т‘Р В°Р Р…Р Р…РЎвЂ№РЎвЂ¦"
            ),
            "customer_name": "Р СљР С‘Р Р…Р В·Р Т‘РЎР‚Р В°Р Р†",
            "price": 410_700_000,
            "score": 95,
            "raw": {"note": "synthetic unit row"},
        }
        fake_psycopg = types.ModuleType("psycopg")
        connect_calls: list[str] = []

        class FakeCursor:
            def execute(self, query: str, params: tuple[str, str]) -> None:
                pass

            def fetchone(self) -> tuple[dict[str, object]] | None:
                return ({"report": {"lead_summary": "ambient production report"}},)

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        class FakeConnection:
            def cursor(self) -> FakeCursor:
                return FakeCursor()

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        def fake_connect(database_url: str) -> FakeConnection:
            connect_calls.append(database_url)
            return FakeConnection()

        fake_psycopg.connect = fake_connect
        fake_config = types.ModuleType("app.config")
        fake_config.settings = SimpleNamespace(database_url="postgres://ambient")
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.dict(
                sys.modules,
                {"psycopg": fake_psycopg, "app.config": fake_config},
                clear=False,
            ),
            patch.object(shortlist, "get_digest_rows", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("", {})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                report_kind="lead",
            )

        self.assertEqual([item["external_id"] for item in selected], ["95790607"])
        self.assertEqual(connect_calls, [])

    def test_final_lead_report_guard_uses_external_id_when_tender_id_is_missing(self) -> None:
        fake_psycopg = types.ModuleType("psycopg")
        executed: list[tuple[str, tuple[str, str]]] = []

        class FakeCursor:
            def execute(self, query: str, params: tuple[str, str]) -> None:
                executed.append((query, params))

            def fetchone(self) -> tuple[dict[str, object]] | None:
                return ({"report": {"lead_summary": "ambient production report"}},)

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        class FakeConnection:
            def cursor(self) -> FakeCursor:
                return FakeCursor()

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        fake_psycopg.connect = lambda database_url: FakeConnection()
        fake_config = types.ModuleType("app.config")
        fake_config.settings = SimpleNamespace(database_url="postgres://ambient")

        with patch.dict(
            sys.modules,
            {"psycopg": fake_psycopg, "app.config": fake_config},
            clear=False,
        ):
            exists = shortlist.operational_lead_report_exists_for_candidate(
                {"external_id": "95790607"}
            )

        self.assertTrue(exists)
        self.assertEqual(len(executed), 1)
        self.assertIn("external_id = %s", executed[0][0])

    def test_lead_candidate_with_missing_documents_is_not_card_only_ready_in_dry_run(self) -> None:
        row = {
            "external_id": "lead-card",
            "title": "Поставка серверного оборудования",
            "price": 10_000_000,
        }

        with (
            patch.object(
                shortlist,
                "document_counts_for_candidate",
                return_value=shortlist.CandidateDocumentCounts(0, 0),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = shortlist.run_llm_for_candidate(
                row,
                dry_run=True,
                report_kind="lead",
                max_spec_chars=1000,
                max_other_chars=1000,
                max_output_tokens=1000,
                timeout_seconds=None,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.document_readiness.llm_readiness, "not_ready_for_llm")
        self.assertTrue(result.document_readiness.targeted_download_triggered)
        self.assertEqual(result.document_readiness.preparation_status, "dry_run_skipped")
        self.assertEqual(result.document_readiness.warning_reason, shortlist.DOCUMENTS_MISSING)

    def test_deadline_filter_excludes_expired_and_near_deadline_candidates(self) -> None:
        now = datetime(2026, 5, 10, 1, 2, tzinfo=timezone.utc)
        rows = [
            {
                "external_id": "expired",
                "title": "Expired",
                "price": 20_000_000,
                "score": 99,
                "deadline_at": now - timedelta(minutes=1),
            },
            {
                "external_id": "too-soon",
                "title": "Deadline risk",
                "price": 20_000_000,
                "score": 98,
                "deadline_at": now + timedelta(days=2, hours=23),
            },
            {
                "external_id": "fresh",
                "title": "Fresh",
                "price": 20_000_000,
                "score": 97,
                "deadline_at": now + timedelta(days=3),
            },
            {
                "external_id": "no-deadline",
                "title": "No deadline",
                "price": 20_000_000,
                "score": 96,
                "deadline_at": None,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=10,
                pool_limit=10,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                now=now,
                report_kind="technical",
            )

        self.assertEqual([row["external_id"] for row in selected], ["fresh", "no-deadline"])
        self.assertEqual(diagnostics.skip_counts["deadline_expired"], 1)
        self.assertEqual(diagnostics.skip_counts["deadline_lt_3d"], 1)
        self.assertEqual(diagnostics.eligible_for_llm_before_limit, 2)
        self.assertEqual(diagnostics.selected_for_llm, 2)

    def test_deadline_filtered_candidates_do_not_occupy_llm_limit(self) -> None:
        now = datetime(2026, 5, 10, 1, 2, tzinfo=timezone.utc)
        rows = [
            {
                "external_id": "expired-expensive",
                "title": "Expired expensive",
                "price": 100_000_000,
                "score": 100,
                "deadline_at": now - timedelta(days=1),
            },
            {
                "external_id": "too-soon-expensive",
                "title": "Too soon expensive",
                "price": 90_000_000,
                "score": 99,
                "deadline_at": now + timedelta(days=2),
            },
            {
                "external_id": "fresh-low-priority",
                "title": "Fresh low priority",
                "price": 10_000_000,
                "score": 10,
                "deadline_at": now + timedelta(days=5),
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=3,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
                now=now,
                report_kind="technical",
            )

        self.assertEqual([row["external_id"] for row in selected], ["fresh-low-priority"])
        self.assertEqual(diagnostics.eligible_for_llm_before_limit, 1)
        self.assertEqual(diagnostics.selected_for_llm, 1)
        self.assertEqual(diagnostics.skip_counts["deadline_expired"], 1)
        self.assertEqual(diagnostics.skip_counts["deadline_lt_3d"], 1)

    def test_exhausted_preparation_case_is_skipped_in_normal_shortlist_run(self) -> None:
        rows = [
            {
                "external_id": "exhausted-docs",
                "title": "Missing docs",
                "price": 10_000_000,
                "score": 90,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(
                shortlist,
                "preparation_exhaustion_skip_reason",
                return_value="preparation_exhausted=documents_missing",
            ),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
            )
            shortlist.print_selection_diagnostics(diagnostics)

        output = stdout.getvalue()
        self.assertEqual(selected, [])
        self.assertEqual(
            diagnostics.skip_counts["preparation_exhausted=documents_missing"],
            1,
        )
        self.assertIn(
            "Skip LLM candidate exhausted-docs: preparation_exhausted=documents_missing",
            output,
        )
        self.assertIn("preparation_exhausted=documents_missing=1", output)

    def test_force_can_include_exhausted_preparation_case(self) -> None:
        rows = [
            {
                "external_id": "exhausted-docs",
                "title": "Missing docs",
                "price": 10_000_000,
                "score": 90,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
            patch.object(
                shortlist,
                "preparation_exhaustion_skip_reason",
                side_effect=AssertionError("force should bypass exhaustion check"),
            ),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=True,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual([row["external_id"] for row in selected], ["exhausted-docs"])
        self.assertNotIn("preparation_exhausted=documents_missing", diagnostics.skip_counts)

    def test_blocked_preparation_cases_are_skipped_in_normal_shortlist_run(self) -> None:
        rows = [
            {
                "external_id": "market-auth",
                "title": "Marketplace auth",
                "price": 10_000_000,
                "score": 90,
            },
            {
                "external_id": "no-docs",
                "title": "No valid documents",
                "price": 10_000_000,
                "score": 80,
            },
        ]
        now = datetime.now(timezone.utc)
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        stdout = io.StringIO()

        def fake_blocked_event(row: dict) -> shortlist.PreparationRetryEvent:
            if row["external_id"] == "market-auth":
                return shortlist.PreparationRetryEvent(
                    payload={"status": "blocked_by_marketplace_auth"},
                    created_at=now - timedelta(hours=1),
                )
            return shortlist.PreparationRetryEvent(
                payload={"status": "no_valid_documents"},
                created_at=now - timedelta(hours=1),
            )

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "latest_preparation_blocked_event", side_effect=fake_blocked_event),
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=True,
                diagnostics=diagnostics,
            )
            shortlist.print_selection_diagnostics(diagnostics)

        output = stdout.getvalue()
        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["preparation_blocked=marketplace_auth"], 1)
        self.assertEqual(diagnostics.skip_counts["preparation_no_valid_documents"], 1)
        self.assertIn(
            "Skip LLM candidate market-auth: preparation_blocked=marketplace_auth",
            output,
        )
        self.assertIn(
            "Skip LLM candidate no-docs: preparation_no_valid_documents",
            output,
        )
        self.assertIn("preparation_blocked=marketplace_auth=1", output)
        self.assertIn("preparation_no_valid_documents=1", output)

    def test_force_can_include_blocked_preparation_case(self) -> None:
        rows = [
            {
                "external_id": "market-auth",
                "title": "Marketplace auth",
                "price": 10_000_000,
                "score": 90,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
            patch.object(
                shortlist,
                "preparation_blocked_skip_reason",
                side_effect=AssertionError("force should bypass blocked-state check"),
            ),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=True,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual([row["external_id"] for row in selected], ["market-auth"])
        self.assertNotIn("preparation_blocked=marketplace_auth", diagnostics.skip_counts)

    def test_direct_preparation_blocked_rows_are_skipped_before_llm(self) -> None:
        rows = [
            {
                "external_id": "market-auth",
                "title": "Marketplace auth",
                "price": 10_000_000,
                "score": 90,
                "documents_with_text": 0,
                "preparation_blocked_event": json.dumps(
                    {"status": "blocked_by_marketplace_auth"}
                ),
            },
            {
                "external_id": "no-docs",
                "title": "No valid documents",
                "price": 10_000_000,
                "score": 80,
                "documents_with_text": 0,
                "preparation_status": "no_valid_documents",
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(
                shortlist,
                "preparation_blocked_skip_reason",
                side_effect=AssertionError("direct skip should happen before DB event lookup"),
            ),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["preparation_blocked=marketplace_auth"], 1)
        self.assertEqual(diagnostics.skip_counts["preparation_no_valid_documents"], 1)

    def test_target_hardware_marketplace_auth_candidate_does_not_take_llm_slot(self) -> None:
        rows = [
            {
                "external_id": "RST32615987873",
                "title": "Поставка серверного оборудования виртуализации для проектов",
                "price": 875_174_000,
                "score": 95,
                "documents_with_text": 0,
                "preparation_blocked_event": json.dumps(
                    {"status": "blocked_by_marketplace_auth"}
                ),
            },
            {
                "external_id": "ready-server",
                "title": "Поставка серверного оборудования",
                "price": 20_000_000,
                "score": 80,
                "documents_with_text": 1,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "match_target_category", return_value=("servers", {"label": "Servers"})),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=1,
                pool_limit=2,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual([row["external_id"] for row in selected], ["ready-server"])
        self.assertEqual(diagnostics.skip_counts["preparation_blocked=marketplace_auth"], 1)

    def test_direct_preparation_signal_does_not_block_when_documents_have_text(self) -> None:
        rows = [
            {
                "external_id": "manual-docs-ready",
                "title": "Ready after manual document",
                "price": 10_000_000,
                "score": 90,
                "documents_with_text": 1,
                "preparation_blocked_event": json.dumps({"status": "no_valid_documents"}),
            },
        ]

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
            )

        self.assertEqual([row["external_id"] for row in selected], ["manual-docs-ready"])

    def test_ready_for_llm_candidate_with_document_text_is_selected(self) -> None:
        rows = [
            {
                "external_id": "ready",
                "title": "Ready server tender",
                "price": 10_000_000,
                "score": 90,
                "documents_with_text": 1,
                "document_state": "documents_ready_with_text",
            },
        ]

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "preparation_blocked_skip_reason", return_value=None),
            patch.object(shortlist, "preparation_exhaustion_skip_reason", return_value=None),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
            )

        self.assertEqual([row["external_id"] for row in selected], ["ready"])

    def test_preparation_blocked_skip_reason_respects_cooldown(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        recent_event = shortlist.PreparationRetryEvent(
            payload={"status": "blocked_by_marketplace_auth"},
            created_at=now - timedelta(hours=23),
        )
        old_event = shortlist.PreparationRetryEvent(
            payload={"status": "blocked_by_marketplace_auth"},
            created_at=now - timedelta(hours=25),
        )

        with patch.object(shortlist, "latest_preparation_blocked_event", return_value=recent_event):
            self.assertEqual(
                shortlist.preparation_blocked_skip_reason(
                    {"external_id": "market-auth"},
                    now=now,
                ),
                "preparation_blocked=marketplace_auth",
            )

        with patch.object(shortlist, "latest_preparation_blocked_event", return_value=old_event):
            self.assertIsNone(
                shortlist.preparation_blocked_skip_reason(
                    {"external_id": "market-auth"},
                    now=now,
                )
            )

    def test_preparation_blocked_skip_ignores_old_event_when_documents_have_text(self) -> None:
        now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        recent_event = shortlist.PreparationRetryEvent(
            payload={"status": "no_valid_documents"},
            created_at=now - timedelta(hours=1),
        )

        with (
            patch.object(shortlist, "latest_preparation_blocked_event", return_value=recent_event),
            patch.object(
                shortlist,
                "document_counts_for_candidate",
                return_value=shortlist.CandidateDocumentCounts(1, 1),
            ),
        ):
            self.assertIsNone(
                shortlist.preparation_blocked_skip_reason(
                    {"external_id": "manual-docs-ready"},
                    now=now,
                )
            )

    def test_preparation_exhaustion_skip_reason_respects_cooldown(self) -> None:
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        recent_event = shortlist.PreparationRetryEvent(
            payload={
                "exhausted": True,
                "documents_downloaded": 0,
                "documents_with_text": 0,
            },
            created_at=now - timedelta(hours=23),
        )
        old_event = shortlist.PreparationRetryEvent(
            payload={
                "exhausted": True,
                "documents_downloaded": 0,
                "documents_with_text": 0,
            },
            created_at=now - timedelta(hours=25),
        )

        with patch.object(shortlist, "latest_preparation_retry_event", return_value=recent_event):
            self.assertEqual(
                shortlist.preparation_exhaustion_skip_reason(
                    {"external_id": "exhausted-docs"},
                    now=now,
                ),
                "preparation_exhausted=documents_missing",
            )

        with patch.object(shortlist, "latest_preparation_retry_event", return_value=old_event):
            self.assertIsNone(
                shortlist.preparation_exhaustion_skip_reason(
                    {"external_id": "exhausted-docs"},
                    now=now,
                )
            )

    def test_preparation_exhaustion_skip_ignores_old_event_when_documents_have_text(self) -> None:
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        recent_event = shortlist.PreparationRetryEvent(
            payload={
                "exhausted": True,
                "documents_downloaded": 0,
                "documents_with_text": 0,
            },
            created_at=now - timedelta(hours=1),
        )

        with (
            patch.object(shortlist, "latest_preparation_retry_event", return_value=recent_event),
            patch.object(
                shortlist,
                "document_counts_for_candidate",
                return_value=shortlist.CandidateDocumentCounts(1, 1),
            ),
        ):
            self.assertIsNone(
                shortlist.preparation_exhaustion_skip_reason(
                    {"external_id": "manual-docs-ready"},
                    now=now,
                )
            )

    def test_generic_unknown_equipment_is_skipped_before_llm(self) -> None:
        rows = [
            {
                "external_id": "generic",
                "title": "Поставка оборудования",
                "price": 87_064_664,
                "score": 90,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "unknown"}),
            patch.object(shortlist, "is_generic_equipment_title", return_value=True),
            patch.object(shortlist, "has_generic_equipment_target_hardware_evidence", return_value=False),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics.skip_counts["generic_equipment_without_target_hardware"], 1)

    def test_generic_unknown_equipment_with_structured_hardware_evidence_can_pass(self) -> None:
        rows = [
            {
                "external_id": "generic-valid",
                "title": "Поставка оборудования",
                "price": 87_064_664,
                "score": 90,
            },
        ]
        diagnostics = shortlist.ShortlistSelectionDiagnostics()

        with (
            patch.object(shortlist, "get_digest_rows", return_value=rows),
            patch.object(shortlist, "effective_recommendation", return_value="go"),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "unknown"}),
            patch.object(shortlist, "is_generic_equipment_title", return_value=True),
            patch.object(shortlist, "has_generic_equipment_target_hardware_evidence", return_value=True),
            patch.object(shortlist, "match_target_category", return_value=("storage", {"label": "Storage"})),
        ):
            selected = shortlist.select_candidates(
                profile={},
                limit=5,
                pool_limit=5,
                force=False,
                min_price=5_000_000,
                include_low_priority=False,
                include_non_full_deals=False,
                include_domestic_restricted=False,
                debug_skips=False,
                diagnostics=diagnostics,
            )

        self.assertEqual([row["external_id"] for row in selected], ["generic-valid"])
        self.assertNotIn("generic_equipment_without_target_hardware", diagnostics.skip_counts)

    def test_dry_run_not_ready_candidate_is_not_counted_as_succeeded(self) -> None:
        row = {"external_id": "missing-docs", "title": "Missing docs", "price": 10}
        readiness = shortlist.CandidateDocumentReadiness(
            external_id="missing-docs",
            docs_before=0,
            docs_with_text_before=0,
            docs_after=0,
            docs_with_text_after=0,
            targeted_download_triggered=True,
            extraction_retry_triggered=False,
            preparation_status="dry_run_skipped",
            document_state="documents_missing",
            warning_reason="documents_missing",
        )

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            candidate["_llm_document_readiness"] = readiness
            return shortlist.LLMCandidateRunResult(
                external_id="missing-docs",
                document_readiness=readiness,
            )

        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_for_shortlist", "--profile", "stub.yaml", "--dry-run"]),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        self.assertIn("- succeeded: 0", output)
        self.assertIn("- failed: 1", output)
        self.assertIn("- failed_external_ids: missing-docs(documents_missing)", output)

    def test_main_continues_after_one_failed_candidate(self) -> None:
        rows = [
            {"external_id": "bad", "title": "Bad LLM response", "price": 10},
            {"external_id": "good", "title": "Good LLM response", "price": 20},
        ]
        calls: list[str] = []

        def fake_run_llm_for_candidate(row: dict, **kwargs: object) -> None:
            external_id = str(row["external_id"])
            calls.append(external_id)
            if external_id == "bad":
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["python", "-m", "app.llm.tender_report"],
                    stderr="RuntimeError: Cannot parse JSON from LLM response: not json",
                )

        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_for_shortlist", "--profile", "stub.yaml"]),
            patch.object(
                shortlist,
                "load_business_profile",
                return_value={"llm_selection": {"default_limit": 2}},
            ),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(
                shortlist,
                "get_price",
                side_effect=lambda row: int(row.get("price") or 0),
            ),
            patch.object(
                shortlist,
                "run_llm_for_candidate",
                side_effect=fake_run_llm_for_candidate,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        self.assertEqual(calls, ["bad", "bad", "good"])
        self.assertIn("LLM candidate failed: external_id=bad reason=non_json_response", output)
        self.assertIn("- succeeded: 1", output)
        self.assertIn("- failed: 1", output)
        self.assertIn("- fallback_succeeded: 0", output)
        self.assertIn("- fallback_failed: 1", output)
        self.assertIn("- failed_external_ids: bad(non_json_response)", output)

    def test_non_json_failure_succeeds_with_fallback_retry(self) -> None:
        rows = [
            {"external_id": "fallback-ok", "title": "Fallback OK", "price": 10},
        ]
        calls: list[tuple[str, str, int, int, int, str | None]] = []

        def fake_run_llm_for_candidate(row: dict, **kwargs: object) -> None:
            external_id = str(row["external_id"])
            result_label = kwargs.get("result_label")
            calls.append(
                (
                    external_id,
                    str(kwargs["analysis_depth"]),
                    int(kwargs["max_spec_chars"]),
                    int(kwargs["max_other_chars"]),
                    int(kwargs["max_output_tokens"]),
                    str(result_label) if result_label else None,
                )
            )
            if result_label != shortlist.FALLBACK_RESULT_LABEL:
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["python", "-m", "app.llm.tender_report"],
                    stderr="RuntimeError: Cannot parse JSON from LLM response: not json",
                )

        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_for_shortlist", "--profile", "stub.yaml"]),
            patch.object(
                shortlist,
                "load_business_profile",
                return_value={"llm_selection": {"default_limit": 1}},
            ),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "get_price", side_effect=lambda row: int(row.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        self.assertEqual(
            calls,
            [
                ("fallback-ok", "deep", 240000, 40000, 16384, None),
                ("fallback-ok", "deep", 120000, 20000, 16384, shortlist.FALLBACK_RESULT_LABEL),
            ],
        )
        self.assertIn("- analysis_depth=deep", output)
        self.assertIn("- max_spec_chars=240000", output)
        self.assertIn("- max_other_chars=40000", output)
        self.assertIn("- max_output_tokens=16384", output)
        self.assertIn("LLM fallback retry: external_id=fallback-ok", output)
        self.assertIn("analysis_depth=deep", output)
        self.assertIn("max_spec_chars=120000", output)
        self.assertIn("max_other_chars=20000", output)
        self.assertIn("max_output_tokens=16384", output)
        self.assertIn("reason=non_json_response", output)
        self.assertIn("LLM fallback succeeded: external_id=fallback-ok", output)
        self.assertIn("- succeeded: 1", output)
        self.assertIn("- failed: 0", output)
        self.assertIn("- fallback_succeeded: 1", output)
        self.assertIn("- fallback_failed: 0", output)
        self.assertIn("fallback-ok(routerai_fallback)", output)

    def test_cli_max_output_tokens_override_is_passed_to_deep_candidate(self) -> None:
        rows = [
            {"external_id": "override-ok", "title": "Override OK", "price": 10},
        ]
        seen_max_output_tokens: list[int] = []

        def fake_run_llm_for_candidate(row: dict, **kwargs: object) -> None:
            seen_max_output_tokens.append(int(kwargs["max_output_tokens"]))

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--max-output-tokens",
                    "22222",
                ],
            ),
            patch.object(
                shortlist,
                "load_business_profile",
                return_value={"llm_selection": {"default_limit": 1}},
            ),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "get_price", side_effect=lambda row: int(row.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            shortlist.main()

        self.assertEqual(seen_max_output_tokens, [22222])

    def test_cli_report_kind_lead_is_passed_to_candidate_runner(self) -> None:
        rows = [
            {"external_id": "lead-ok", "title": "Lead OK", "price": 10},
        ]
        seen_report_kinds: list[str] = []

        def fake_run_llm_for_candidate(row: dict, **kwargs: object) -> None:
            seen_report_kinds.append(str(kwargs["report_kind"]))

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--no-lead-triage",
                ],
            ),
            patch.object(
                shortlist,
                "load_business_profile",
                return_value={"llm_selection": {"default_limit": 1}},
            ),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "get_price", side_effect=lambda row: int(row.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            shortlist.main()

        self.assertEqual(seen_report_kinds, ["lead"])

    def test_lead_dry_run_marks_triage_pending_without_paid_triage_call(self) -> None:
        rows = [
            {"external_id": "pending", "title": "Pending lead", "price": 10_000_000},
        ]
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--dry-run",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "get_price", side_effect=lambda row: int(row.get("price") or 0)),
            patch.object(shortlist, "lead_triage_result_for_candidate", return_value=None),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        run_triage.assert_not_called()
        run_report.assert_not_called()
        self.assertIn("lead_triage_pending", output)
        self.assertIn("No lead candidates approved by triage.", output)

    def test_lead_dry_run_existing_triage_can_select_full_report_candidate(self) -> None:
        row = {
            "external_id": "approved",
            "title": "Approved lead",
            "price": 80_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "medium",
                "lead_summary": "Strategic infrastructure customer",
                "likely_customer_story": "Modernization signal",
                "possible_needs": ["servers"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        calls: list[str] = []

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            calls.append(str(candidate["external_id"]))
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--dry-run",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        run_triage.assert_not_called()
        self.assertEqual(calls, ["approved"])
        self.assertIn("lead_triage_decision=go", output)
        self.assertIn("lead_triage_priority=high", output)
        self.assertIn("selected_for_lead_report=true", output)
        self.assertIn("lead_triage_maybe_deferred=false", output)
        self.assertIn("80,000,000 руб.", output)
        self.assertNotIn("в‚Ѕ", output)
        self.assertIn("selected_for_lead_report", output)

    def test_lead_dry_run_existing_maybe_triage_is_deferred_without_flag(self) -> None:
        row = {
            "external_id": "maybe",
            "title": "Maybe lead",
            "price": 40_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "confidence": "medium",
                "lead_summary": "Potential infrastructure account",
                "likely_customer_story": "Possible future storage work",
                "possible_needs": ["storage"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--dry-run",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        run_triage.assert_not_called()
        run_report.assert_not_called()
        self.assertIn("lead_triage_decision=maybe", output)
        self.assertIn("selected_for_lead_report=false", output)
        self.assertIn("lead_triage_maybe_deferred=true", output)
        self.assertIn("skipped_by_lead_triage=true", output)
        self.assertIn("No lead candidates approved by triage.", output)

    def test_lead_dry_run_existing_maybe_triage_can_select_with_include_flag(self) -> None:
        row = {
            "external_id": "maybe-included",
            "title": "Maybe lead included",
            "price": 40_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "confidence": "medium",
                "lead_summary": "Potential infrastructure account",
                "likely_customer_story": "Possible future storage work",
                "possible_needs": ["storage"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        calls: list[str] = []

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            calls.append(str(candidate["external_id"]))
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--dry-run",
                    "--include-maybe-leads",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        run_triage.assert_not_called()
        self.assertEqual(calls, ["maybe-included"])
        self.assertIn("lead_triage_decision=maybe", output)
        self.assertIn("selected_for_lead_report=true", output)
        self.assertIn("lead_triage_maybe_deferred=false", output)

    def test_lead_dry_run_existing_reject_triage_skips_full_report_candidate(self) -> None:
        row = {
            "external_id": "rejected",
            "title": "Rejected lead",
            "price": 40_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "lead_summary": "",
                "likely_customer_story": "",
                "possible_needs": [],
                "target_roles": [],
                "reject_reason": "one-off support renewal",
                "requires_full_lead_report": False,
            },
        }
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--dry-run",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        run_triage.assert_not_called()
        run_report.assert_not_called()
        self.assertIn("lead_triage_decision=reject", output)
        self.assertIn("selected_for_lead_report=false", output)
        self.assertIn("skipped_by_lead_triage=true", output)
        self.assertIn("reject_reason=one-off support renewal", output)

    def test_lead_triage_cache_hit_summary_is_visible(self) -> None:
        row = {
            "external_id": "cache-hit",
            "title": "Cached lead",
            "price": 80_000_000,
        }
        cached_triage = {
            "created_at": fresh_lead_triage_created_at(),
            "report": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "medium",
                "lead_summary": "Cached strategic account",
                "likely_customer_story": "Refresh project",
                "possible_needs": ["servers"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            }
        }
        diagnostics = shortlist.LeadTriageDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "analysis_result_for_candidate", return_value=cached_triage),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.apply_lead_triage(
                [row],
                dry_run=False,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=1,
                diagnostics=diagnostics,
            )
            shortlist.print_lead_triage_summary(diagnostics)

        run_triage.assert_not_called()
        self.assertEqual([item["external_id"] for item in selected], ["cache-hit"])
        output = stdout.getvalue()
        self.assertIn("lead_triage_source=cache", output)
        self.assertIn("- lead_triage_cache_hits: 1", output)
        self.assertIn("- lead_triage_fresh_cache_used: 1", output)
        self.assertIn("- lead_triage_llm_attempted: 0", output)

    def test_fresh_cached_lead_triage_can_defer_without_paid_triage_call(self) -> None:
        row = {
            "external_id": "fresh-cache-maybe",
            "title": "Fresh cached maybe",
            "price": 80_000_000,
        }
        cached_triage = {
            "created_at": fresh_lead_triage_created_at(),
            "report": {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "confidence": "medium",
                "lead_summary": "Fresh but not enough for full lead report",
                "likely_customer_story": "Possible future project",
                "possible_needs": ["storage"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": False,
            },
        }
        diagnostics = shortlist.LeadTriageDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "analysis_result_for_candidate", return_value=cached_triage),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.apply_lead_triage(
                [row],
                dry_run=False,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=1,
                diagnostics=diagnostics,
            )
            shortlist.print_lead_triage_summary(diagnostics)

        run_triage.assert_not_called()
        self.assertEqual(selected, [])
        output = stdout.getvalue()
        self.assertIn("lead_triage_source=cache", output)
        self.assertIn("lead_triage_maybe_deferred=true", output)
        self.assertIn("- lead_triage_cache_hits: 1", output)
        self.assertIn("- lead_triage_fresh_cache_used: 1", output)
        self.assertIn("- lead_triage_llm_attempted: 0", output)

    def test_stale_cached_reject_triage_is_invalidated_without_full_report(self) -> None:
        row = {
            "external_id": "stale-cache-reject",
            "title": "Stale cached reject",
            "price": 80_000_000,
        }
        cached_triage = {
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
            "report": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "lead_summary": "",
                "likely_customer_story": "",
                "possible_needs": [],
                "target_roles": [],
                "reject_reason": "old cache reject",
                "requires_full_lead_report": False,
            },
        }
        live_triage = {
            "lead_decision": "go",
            "lead_priority": "high",
            "confidence": "medium",
            "lead_summary": "Fresh strategic account",
            "likely_customer_story": "Infrastructure refresh",
            "possible_needs": ["servers"],
            "target_roles": ["CIO"],
            "reject_reason": "",
            "requires_full_lead_report": True,
        }
        diagnostics = shortlist.LeadTriageDiagnostics()
        stdout = io.StringIO()

        with (
            patch.object(shortlist, "analysis_result_for_candidate", return_value=cached_triage),
            patch.object(shortlist, "operational_lead_report_exists_for_candidate", return_value=False),
            patch.object(shortlist, "run_lead_triage_for_candidate", return_value=live_triage) as run_triage,
            contextlib.redirect_stdout(stdout),
        ):
            selected = shortlist.apply_lead_triage(
                [row],
                dry_run=False,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=1,
                diagnostics=diagnostics,
            )
            shortlist.print_lead_triage_summary(diagnostics)

        run_triage.assert_called_once()
        self.assertEqual([item["external_id"] for item in selected], ["stale-cache-reject"])
        output = stdout.getvalue()
        self.assertIn("lead_triage_stale_cache_ignored", output)
        self.assertIn("lead_triage_source=llm", output)
        self.assertIn("- lead_triage_stale_cache_ignored: 1", output)
        self.assertIn("- lead_triage_llm_attempted: 1", output)

    def test_stale_existing_maybe_triage_runs_fresh_triage_and_full_report(self) -> None:
        row = {
            "external_id": "stale-existing-maybe",
            "title": "Stale existing maybe",
            "price": 80_000_000,
            "lead_triage_created_at": (
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).isoformat(),
            "lead_triage_result": {
                "lead_decision": "maybe",
                "lead_priority": "medium",
                "confidence": "medium",
                "lead_summary": "Old maybe",
                "likely_customer_story": "Old uncertain story",
                "possible_needs": ["storage"],
                "target_roles": ["IT director"],
                "reject_reason": "",
                "requires_full_lead_report": False,
            },
        }
        live_triage = {
            "lead_decision": "go",
            "lead_priority": "high",
            "confidence": "medium",
            "lead_summary": "Fresh strategic account",
            "likely_customer_story": "Infrastructure refresh",
            "possible_needs": ["storage"],
            "target_roles": ["CIO"],
            "reject_reason": "",
            "requires_full_lead_report": True,
        }
        report_calls: list[str] = []

        def fake_select_candidates(**kwargs: object) -> list[dict]:
            diagnostics = kwargs.get("diagnostics")
            if isinstance(diagnostics, shortlist.ShortlistSelectionDiagnostics):
                diagnostics.rule_based_rows = 1
                diagnostics.rule_based_passed = 1
                diagnostics.eligible_for_llm_before_limit = 1
                diagnostics.selected_for_llm = 1
                diagnostics.selected_for_lead_triage = 1
            return [row]

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            report_calls.append(str(candidate["external_id"]))
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", side_effect=fake_select_candidates),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "analysis_result_for_candidate", return_value=None),
            patch.object(shortlist, "operational_lead_report_exists_for_candidate", return_value=False),
            patch.object(shortlist, "run_lead_triage_for_candidate", return_value=live_triage) as run_triage,
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            patch.object(shortlist, "get_duplicate_operational_lead_report_rows", return_value=[]),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_called_once()
        self.assertEqual(report_calls, ["stale-existing-maybe"])
        output = stdout.getvalue()
        self.assertIn("lead_triage_stale_existing_ignored", output)
        self.assertIn("lead_triage_source=llm", output)
        self.assertIn("lead_triage_decision=go", output)
        self.assertIn("- lead_triage_llm_attempted: 1", output)
        self.assertIn("- lead_triage_stale_existing_ignored: 1", output)
        self.assertIn("- lead_full_report_succeeded: 1", output)

    def test_force_lead_triage_cli_reruns_existing_triage(self) -> None:
        row = {
            "external_id": "force-triage",
            "title": "Force triage lead",
            "price": 80_000_000,
            "lead_triage_result": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "lead_summary": "",
                "likely_customer_story": "",
                "possible_needs": [],
                "target_roles": [],
                "reject_reason": "old cached reject",
                "requires_full_lead_report": False,
            },
        }
        live_triage = {
            "lead_decision": "go",
            "lead_priority": "high",
            "confidence": "medium",
            "lead_summary": "Live strategic account",
            "likely_customer_story": "Infrastructure refresh",
            "possible_needs": ["storage"],
            "target_roles": ["CIO"],
            "reject_reason": "",
            "requires_full_lead_report": True,
        }

        def fake_select_candidates(**kwargs: object) -> list[dict]:
            diagnostics = kwargs.get("diagnostics")
            if isinstance(diagnostics, shortlist.ShortlistSelectionDiagnostics):
                diagnostics.rule_based_rows = 1
                diagnostics.rule_based_passed = 1
                diagnostics.eligible_for_llm_before_limit = 1
                diagnostics.selected_for_llm = 1
                diagnostics.selected_for_lead_triage = 1
            return [row]

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--force-lead-triage",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", side_effect=fake_select_candidates),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate", return_value=live_triage) as run_triage,
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate) as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_called_once()
        run_report.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("- selected_for_lead_triage: 1", output)
        self.assertIn("lead_triage_pending lead_triage_source=llm", output)
        self.assertNotIn("lead_triage_pendinglead_triage_source", output)
        self.assertIn("lead_triage_source=llm", output)
        self.assertIn("lead_triage_decision=go", output)
        self.assertIn("- lead_triage_llm_attempted: 1", output)
        self.assertIn("- lead_triage_reused_existing: 0", output)

    def test_force_lead_triage_does_not_rerun_existing_operational_full_report(self) -> None:
        row = {
            "external_id": "force-triage-existing-report",
            "title": "Force triage lead with report",
            "price": 80_000_000,
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
            "lead_triage_result": {
                "lead_decision": "reject",
                "lead_priority": "low",
                "confidence": "high",
                "lead_summary": "",
                "likely_customer_story": "",
                "possible_needs": [],
                "target_roles": [],
                "reject_reason": "old cached reject",
                "requires_full_lead_report": False,
            },
        }
        live_triage = {
            "lead_decision": "go",
            "lead_priority": "high",
            "confidence": "medium",
            "lead_summary": "Live strategic account",
            "likely_customer_story": "Infrastructure refresh",
            "possible_needs": ["storage"],
            "target_roles": ["CIO"],
            "reject_reason": "",
            "requires_full_lead_report": True,
        }

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--force-lead-triage",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate", return_value=live_triage) as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            patch.object(shortlist, "get_duplicate_operational_lead_report_rows", return_value=[]),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_called_once()
        run_report.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("lead_triage_source=llm", output)
        self.assertIn(
            "Skip full lead report force-triage-existing-report: lead_full_report_already_exists",
            output,
        )
        self.assertIn("- lead_full_report_attempted: 0", output)
        self.assertIn("- lead_full_report_succeeded: 0", output)
        self.assertIn("- lead_full_report_failed: 0", output)
        self.assertIn("- lead_full_report_existing_skipped: 1", output)

    def test_ordinary_run_does_not_duplicate_existing_operational_full_report(self) -> None:
        row = {
            "external_id": "ordinary-existing-report",
            "title": "Ordinary lead with report",
            "price": 80_000_000,
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "medium",
                "lead_summary": "Fresh triage",
                "likely_customer_story": "Infrastructure refresh",
                "possible_needs": ["storage"],
                "target_roles": ["CIO"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            patch.object(shortlist, "get_duplicate_operational_lead_report_rows", return_value=[]),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_not_called()
        run_report.assert_not_called()
        output = stdout.getvalue()
        self.assertIn(
            "Skip full lead report ordinary-existing-report: lead_full_report_already_exists",
            output,
        )
        self.assertIn("- lead_full_report_attempted: 0", output)
        self.assertIn("- lead_full_report_succeeded: 0", output)
        self.assertIn("- lead_full_report_existing_skipped: 1", output)

    def test_default_limit_five_still_skips_existing_operational_report(self) -> None:
        existing = {
            "external_id": "default-existing-report",
            "title": "Existing operational lead report",
            "price": 80_000_000,
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "medium",
                "lead_summary": "Fresh triage",
                "likely_customer_story": "Refresh",
                "possible_needs": ["servers"],
                "target_roles": ["CIO"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        new_go = {
            "external_id": "default-new-go",
            "title": "New go lead",
            "price": 70_000_000,
            "lead_triage_created_at": fresh_lead_triage_created_at(),
            "lead_triage_result": {
                "lead_decision": "go",
                "lead_priority": "high",
                "confidence": "medium",
                "lead_summary": "New triage",
                "likely_customer_story": "Expansion",
                "possible_needs": ["storage"],
                "target_roles": ["CIO"],
                "reject_reason": "",
                "requires_full_lead_report": True,
            },
        }
        report_calls: list[str] = []

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            report_calls.append(str(candidate["external_id"]))
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                ["run_llm_for_shortlist", "--profile", "stub.yaml", "--report-kind", "lead"],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {}}),
            patch.object(shortlist, "select_candidates", return_value=[existing, new_go]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            patch.object(shortlist, "get_duplicate_operational_lead_report_rows", return_value=[]),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_not_called()
        self.assertEqual(report_calls, ["default-new-go"])
        output = stdout.getvalue()
        self.assertIn("- lead_full_report_existing_skipped: 1", output)
        self.assertIn("- lead_full_report_succeeded: 1", output)
        self.assertIn("- full_reports_created: 1", output)

    def test_force_lead_report_allows_existing_operational_full_report_refresh(self) -> None:
        row = {
            "external_id": "force-report-existing",
            "title": "Force lead report",
            "price": 80_000_000,
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
        }
        calls: list[str] = []

        def fake_run_llm_for_candidate(candidate: dict, **kwargs: object) -> shortlist.LLMCandidateRunResult:
            calls.append(str(candidate["external_id"]))
            return shortlist.LLMCandidateRunResult(external_id=str(candidate["external_id"]))

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                    "--no-lead-triage",
                    "--force-lead-report",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 1}}),
            patch.object(shortlist, "select_candidates", return_value=[row]),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            patch.object(shortlist, "get_duplicate_operational_lead_report_rows", return_value=[]),
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        self.assertEqual(calls, ["force-report-existing"])
        output = stdout.getvalue()
        self.assertNotIn("lead_full_report_already_exists", output)
        self.assertIn("- lead_full_report_attempted: 1", output)
        self.assertIn("- lead_full_report_succeeded: 1", output)
        self.assertIn("- lead_full_report_failed: 0", output)
        self.assertIn("- lead_full_report_existing_skipped: 0", output)

    def test_existing_labeled_lead_report_does_not_block_operational_full_report(self) -> None:
        row = {
            "external_id": "labeled-existing",
            "title": "Labeled lead report",
            "price": 80_000_000,
            "llm_report_analysis_type": "llm_customer_lead_report_smoke",
            "llm_report_result": {"report": {"lead_summary": "labeled"}},
        }

        with patch.object(shortlist, "analysis_result_exists_for_candidate", return_value=False):
            self.assertFalse(shortlist.operational_lead_report_exists_for_candidate(row))

    def test_existing_operational_lead_report_blocks_operational_full_report(self) -> None:
        row = {
            "external_id": "operational-existing",
            "title": "Operational lead report",
            "price": 80_000_000,
            "llm_report_analysis_type": shortlist.LEAD_LLM_REPORT_ANALYSIS_TYPE,
            "llm_report_result": {"report": {"lead_summary": "existing"}},
        }

        self.assertTrue(shortlist.operational_lead_report_exists_for_candidate(row))

    def test_lead_full_report_summary_counters_are_visible(self) -> None:
        diagnostics = shortlist.LeadFullReportDiagnostics(
            attempted=2,
            succeeded=1,
            failed=1,
            existing_skipped=3,
        )
        warning = shortlist.CandidateDocumentReadiness(
            external_id="warn",
            docs_before=0,
            docs_with_text_before=0,
            docs_after=0,
            docs_with_text_after=0,
            targeted_download_triggered=True,
            extraction_retry_triggered=False,
            preparation_status="failed",
            document_state="documents_missing",
            non_blocking_warning_reason="lead_documents_missing_allowed_after_prepare",
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            shortlist.print_llm_summary(
                ["ok"],
                [
                    shortlist.LLMCandidateFailure(
                        external_id="bad",
                        reason="subprocess_failed",
                        detail="failed",
                    )
                ],
                document_warnings=[warning],
                lead_full_report_diagnostics=diagnostics,
                duplicate_operational_lead_report_rows=[
                    {
                        "duplicate_groups_total": 1,
                        "external_id": "dup",
                        "reports_count": 2,
                    }
                ],
                report_kind="lead",
            )

        output = stdout.getvalue()
        self.assertIn("- lead_full_report_attempted: 2", output)
        self.assertIn("- lead_full_report_succeeded: 1", output)
        self.assertIn("- lead_full_report_failed: 1", output)
        self.assertIn("- lead_full_report_existing_skipped: 3", output)
        self.assertIn("- duplicate_operational_lead_reports: 1", output)
        self.assertIn("- duplicate_external_ids: dup(2)", output)
        self.assertIn("Document warnings:", output)
        self.assertIn("external_id=warn", output)
        self.assertIn("Final run status:", output)
        self.assertIn("- run_status: partial_failure", output)

    def test_lead_triage_summary_counts_go_maybe_reject(self) -> None:
        rows = [
            {
                "external_id": "go",
                "title": "Go lead",
                "lead_triage_created_at": fresh_lead_triage_created_at(),
                "lead_triage_result": {
                    "lead_decision": "go",
                    "lead_priority": "high",
                    "confidence": "medium",
                    "lead_summary": "Go",
                    "likely_customer_story": "Refresh",
                    "possible_needs": ["servers"],
                    "target_roles": ["IT"],
                    "reject_reason": "",
                    "requires_full_lead_report": True,
                },
            },
            {
                "external_id": "maybe",
                "title": "Maybe lead",
                "lead_triage_created_at": fresh_lead_triage_created_at(),
                "lead_triage_result": {
                    "lead_decision": "maybe",
                    "lead_priority": "medium",
                    "confidence": "medium",
                    "lead_summary": "Maybe",
                    "likely_customer_story": "Possible refresh",
                    "possible_needs": ["storage"],
                    "target_roles": ["IT"],
                    "reject_reason": "",
                    "requires_full_lead_report": True,
                },
            },
            {
                "external_id": "reject",
                "title": "Reject lead",
                "lead_triage_created_at": fresh_lead_triage_created_at(),
                "lead_triage_result": {
                    "lead_decision": "reject",
                    "lead_priority": "low",
                    "confidence": "high",
                    "lead_summary": "",
                    "likely_customer_story": "",
                    "possible_needs": [],
                    "target_roles": [],
                    "reject_reason": "not profile",
                    "requires_full_lead_report": False,
                },
            },
        ]
        diagnostics = shortlist.LeadTriageDiagnostics()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            selected = shortlist.apply_lead_triage(
                rows,
                dry_run=True,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=2,
                diagnostics=diagnostics,
            )
            shortlist.print_lead_triage_summary(diagnostics)

        self.assertEqual([item["external_id"] for item in selected], ["go"])
        output = stdout.getvalue()
        self.assertIn("- lead_triage_go: 1", output)
        self.assertIn("- lead_triage_maybe: 1", output)
        self.assertIn("- lead_triage_reject: 1", output)
        self.assertIn("- lead_triage_maybe_deferred: 1", output)
        self.assertIn("- lead_triage_selected_for_report: 1", output)

    def test_go_candidates_limited_by_full_report_limit_are_visible_as_waiting(self) -> None:
        rows = []
        for index in range(3):
            rows.append(
                {
                    "external_id": f"go-{index}",
                    "title": f"Go lead {index}",
                    "price": 10_000_000 + index,
                    "_llm_category_label": "Servers",
                    "lead_triage_created_at": fresh_lead_triage_created_at(),
                    "lead_triage_result": {
                        "lead_decision": "go",
                        "lead_priority": "high" if index == 0 else "medium",
                        "confidence": "medium",
                        "lead_summary": "Go",
                        "likely_customer_story": "Refresh",
                        "possible_needs": ["servers"],
                        "target_roles": ["IT"],
                        "reject_reason": "",
                        "requires_full_lead_report": True,
                    },
                }
            )
        diagnostics = shortlist.LeadTriageDiagnostics()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            selected = shortlist.apply_lead_triage(
                rows,
                dry_run=True,
                provider=None,
                model=None,
                json_mode=False,
                full_report_limit=1,
                diagnostics=diagnostics,
            )
            shortlist.print_lead_triage_summary(diagnostics)

        self.assertEqual([item["external_id"] for item in selected], ["go-0"])
        output = stdout.getvalue()
        self.assertIn("- lead_triage_go: 3", output)
        self.assertIn("- lead_triage_selected_for_report: 1", output)
        self.assertIn("- lead_triage_go_waiting_for_report: 2", output)
        self.assertIn("- lead_report_limit_reached_go: 2", output)
        self.assertIn("- go_waiting_for_full_report: 2", output)
        self.assertIn("Go candidates waiting for full report:", output)
        self.assertIn("external_id=go-1", output)
        self.assertIn("category=Servers", output)
        self.assertIn("triage_priority=medium", output)

    def test_selection_diagnostics_include_pre_triage_skip_categories(self) -> None:
        diagnostics = shortlist.ShortlistSelectionDiagnostics()
        diagnostics.rule_based_rows = 4
        diagnostics.rule_based_passed = 1
        diagnostics.eligible_for_llm_before_limit = 1
        diagnostics.selected_for_llm = 1
        diagnostics.selected_for_lead_triage = 1
        diagnostics.pre_triage_candidates_seen = 4
        diagnostics.skip("lead_negative_feedback=bad_price")
        diagnostics.skip("lead_already_has_report")
        diagnostics.skip("lead_hard_noise=video_surveillance")
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            shortlist.print_selection_diagnostics(diagnostics)

        output = stdout.getvalue()
        self.assertIn("- pre_triage_candidates_seen: 4", output)
        self.assertIn("- skipped_before_triage_by_negative_feedback: 1", output)
        self.assertIn("- skipped_before_triage_by_existing_report: 1", output)
        self.assertIn("- skipped_before_triage_by_hard_noise: 1", output)

    def test_all_maybe_reject_lead_triage_skips_reports_with_clear_summary(self) -> None:
        rows = []
        for index in range(15):
            decision = "maybe" if index < 8 else "reject"
            rows.append(
                {
                    "external_id": f"triage-{index}",
                    "title": f"Triage {index}",
                    "price": 20_000_000 + index,
                    "lead_triage_created_at": fresh_lead_triage_created_at(),
                    "lead_triage_result": {
                        "lead_decision": decision,
                        "lead_priority": "medium" if decision == "maybe" else "low",
                        "confidence": "medium",
                        "lead_summary": "Maybe" if decision == "maybe" else "",
                        "likely_customer_story": "Possible refresh" if decision == "maybe" else "",
                        "possible_needs": ["storage"] if decision == "maybe" else [],
                        "target_roles": ["IT"] if decision == "maybe" else [],
                        "reject_reason": "" if decision == "maybe" else "not profile",
                        "requires_full_lead_report": decision == "maybe",
                    },
                }
            )

        def fake_select_candidates(**kwargs: object) -> list[dict]:
            diagnostics = kwargs.get("diagnostics")
            if isinstance(diagnostics, shortlist.ShortlistSelectionDiagnostics):
                diagnostics.rule_based_rows = 15
                diagnostics.rule_based_passed = 15
                diagnostics.eligible_for_llm_before_limit = 15
                diagnostics.selected_for_llm = 15
                diagnostics.selected_for_lead_triage = 15
            return rows

        stdout = io.StringIO()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_llm_for_shortlist",
                    "--profile",
                    "stub.yaml",
                    "--report-kind",
                    "lead",
                ],
            ),
            patch.object(shortlist, "load_business_profile", return_value={"llm_selection": {"default_limit": 5}}),
            patch.object(shortlist, "select_candidates", side_effect=fake_select_candidates),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "target_hardware"}),
            patch.object(shortlist, "get_price", side_effect=lambda candidate: int(candidate.get("price") or 0)),
            patch.object(shortlist, "run_lead_triage_for_candidate") as run_triage,
            patch.object(shortlist, "run_llm_for_candidate") as run_report,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        run_triage.assert_not_called()
        run_report.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("- selected_for_lead_triage: 15", output)
        self.assertIn("- lead_triage_candidates_total: 15", output)
        self.assertIn("- lead_triage_maybe: 8", output)
        self.assertIn("- lead_triage_reject: 7", output)
        self.assertIn("- lead_triage_selected_for_report: 0", output)
        self.assertIn("- lead_triage_maybe_deferred: 8", output)
        self.assertIn("No lead candidates approved by triage.", output)

    def test_standard_fallback_context_limits_remain_compact(self) -> None:
        self.assertEqual(
            shortlist.fallback_context_limits(
                analysis_depth="standard",
                max_spec_chars=240000,
                max_other_chars=40000,
            ),
            (30000, 6000),
        )

    def test_fallback_is_not_run_for_timeout_or_subprocess_failed(self) -> None:
        rows = [
            {"external_id": "timeout", "title": "Timeout", "price": 10},
            {"external_id": "generic", "title": "Generic failure", "price": 20},
        ]
        calls: list[str] = []

        def fake_run_llm_for_candidate(row: dict, **kwargs: object) -> None:
            external_id = str(row["external_id"])
            calls.append(external_id)
            if external_id == "timeout":
                raise subprocess.TimeoutExpired(cmd=["python"], timeout=10)

            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["python", "-m", "app.llm.tender_report"],
                stderr="RuntimeError: provider request failed",
            )

        stdout = io.StringIO()

        with (
            patch.object(sys, "argv", ["run_llm_for_shortlist", "--profile", "stub.yaml"]),
            patch.object(
                shortlist,
                "load_business_profile",
                return_value={"llm_selection": {"default_limit": 2}},
            ),
            patch.object(shortlist, "select_candidates", return_value=rows),
            patch.object(shortlist, "business_assessment", return_value={"market_access": "open"}),
            patch.object(shortlist, "get_price", side_effect=lambda row: int(row.get("price") or 0)),
            patch.object(shortlist, "run_llm_for_candidate", side_effect=fake_run_llm_for_candidate),
            patch.object(shortlist, "run_llm_fallback_for_candidate") as fallback,
            contextlib.redirect_stdout(stdout),
        ):
            shortlist.main()

        output = stdout.getvalue()
        self.assertEqual(calls, ["timeout", "generic"])
        fallback.assert_not_called()
        self.assertIn("- succeeded: 0", output)
        self.assertIn("- failed: 2", output)
        self.assertIn("- fallback_succeeded: 0", output)
        self.assertIn("- fallback_failed: 0", output)
        self.assertIn("- failed_external_ids: timeout(timeout), generic(subprocess_failed)", output)


if __name__ == "__main__":
    unittest.main()
