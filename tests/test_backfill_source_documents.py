from __future__ import annotations

import unittest
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.pipeline import backfill_source_documents
from app.pipeline.prepare_tender_for_analysis import run_targeted_document_download


def candidate(external_id: str) -> dict:
    return {
        "external_id": external_id,
        "title": "Server supply",
        "customer_name": "Customer",
        "initial_price": 1_000_000,
    }


def audit_row(row: dict) -> dict[str, str]:
    return {
        "external_id": str(row.get("external_id") or ""),
        "title": str(row.get("title") or ""),
        "customer_name": str(row.get("customer_name") or ""),
        "initial_price": str(row.get("initial_price") or ""),
        "source_documents_count": "2",
        "docs_count": "0",
        "docs_with_text_count": "0",
        "suggested_action": "source_docs_not_downloaded",
    }


def preparation_result(
    *,
    downloaded: int,
    with_text: int,
    details: dict,
    status: str = "ready_for_llm",
) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        ready_for_llm=status == "ready_for_llm",
        document_readiness=(
            "documents_ready_with_text" if with_text else "documents_missing"
        ),
        document_counts=SimpleNamespace(
            downloaded=downloaded,
            with_extracted_text=with_text,
        ),
        marketplace_auth_blocked=False,
        errors=[],
        summary="prepared",
        steps={
            "targeted_document_download": SimpleNamespace(details=details),
        },
    )


class BackfillSourceDocumentsTest(unittest.TestCase):
    def test_dry_run_reports_candidates_without_preparation(self) -> None:
        with patch.object(
            backfill_source_documents.audit_document_selection,
            "audit_output_row",
            side_effect=audit_row,
        ):
            rows = backfill_source_documents.run_backfill(
                [candidate("00000034")],
                apply=False,
                limit_docs=5,
            )

        self.assertEqual(rows[0]["batch_status"], "dry_run_would_prepare")
        self.assertEqual(rows[0]["planner_used"], "false")
        self.assertEqual(rows[0]["suggested_action_before"], "source_docs_not_downloaded")

    def test_apply_disables_llm_planner_and_stops_after_rate_limit(self) -> None:
        result = preparation_result(
            downloaded=0,
            with_text=0,
            details={
                "documents_found": 2,
                "documents_selected": 2,
                "saved_documents": 0,
                "documents_failed": 0,
                "rate_limit_retries": 1,
                "stopped_due_to_rate_limit": True,
                "document_download_planner_used": False,
            },
            status="partial_preparation",
        )

        with (
            patch.object(
                backfill_source_documents.audit_document_selection,
                "audit_output_row",
                side_effect=audit_row,
            ),
            patch(
                "app.pipeline.prepare_tender_for_analysis.prepare_tender_for_analysis",
                return_value=result,
            ) as prepare,
        ):
            rows = backfill_source_documents.run_backfill(
                [candidate("00000034"), candidate("00000042")],
                apply=True,
                limit_docs=3,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_status"], "rate_limited")
        self.assertEqual(rows[0]["rate_limited"], "true")
        self.assertEqual(rows[0]["planner_used"], "false")
        prepare.assert_called_once_with(
            external_id="00000034",
            limit_docs=3,
            force_redownload=False,
            overwrite_text=False,
            use_llm_planner=False,
        )

    def test_classifies_downloaded_with_text_as_successful_backfill(self) -> None:
        result = preparation_result(
            downloaded=1,
            with_text=1,
            details={
                "documents_found": 2,
                "documents_selected": 1,
                "saved_documents": 1,
                "stopped_due_to_rate_limit": False,
            },
        )

        self.assertEqual(
            backfill_source_documents.classify_backfill_result(result),
            "downloaded_with_text",
        )

    def test_targeted_download_accepts_disabled_llm_planner(self) -> None:
        client = SimpleNamespace(
            login=lambda: None,
            client=SimpleNamespace(close=lambda: None),
        )
        documents_module = types.ModuleType("app.collector.documents")
        documents_module.get_document_rate_limit_seconds = lambda: 1.5
        run_report = Mock(
            return_value=SimpleNamespace(to_dict=lambda: {"total_saved": 0})
        )
        documents_module.run_targeted_download_report = run_report
        z360_module = types.ModuleType("app.collector.zakupki360")
        z360_module.Zakupki360Client = lambda: client

        with patch.dict(
            sys.modules,
            {
                "app.collector.documents": documents_module,
                "app.collector.zakupki360": z360_module,
            },
        ):
            run_targeted_document_download(
                external_id="00000034",
                tender_id=None,
                limit_docs=2,
                force_redownload=False,
                use_llm_planner=False,
            )

        self.assertFalse(run_report.call_args.kwargs["use_llm_planner"])
        self.assertEqual(run_report.call_args.kwargs["document_rate_limit_seconds"], 1.5)


if __name__ == "__main__":
    unittest.main()
