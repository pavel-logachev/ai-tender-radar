from __future__ import annotations

import unittest
from decimal import Decimal

from app.evaluation.lead_triage import (
    LeadTriageDataset,
    LeadTriageGoldenCase,
    LeadTriagePrediction,
    evaluate_lead_triage,
)
from app.platform.versioning import sha256_text


def output(decision: str) -> dict[str, object]:
    return {
        "lead_decision": decision,
        "lead_priority": "high" if decision == "go" else "low",
        "confidence": "high",
        "lead_summary": "Краткое решение.",
        "likely_customer_story": "Проверяемая гипотеза.",
        "possible_needs": [],
        "target_roles": [],
        "reject_reason": "" if decision != "reject" else "Нет инфраструктурного повода.",
        "requires_full_lead_report": decision == "go",
    }


class LeadTriageEvaluationTest(unittest.TestCase):
    def build_dataset(self, *, real: bool) -> LeadTriageDataset:
        return LeadTriageDataset(
            dataset_version="lead-triage-golden-v1",
            cases=[
                LeadTriageGoldenCase(
                    case_id="case-go",
                    provenance="unit fixture",
                    privacy_class="internal_sanitized" if real else "synthetic",
                    source_sha256=sha256_text("case-go-source"),
                    expected_decision="go",
                    business_weight=Decimal("3"),
                    critical_wrong_decisions=["reject"],
                    tags=["normal"],
                    is_real_case=real,
                ),
                LeadTriageGoldenCase(
                    case_id="case-reject",
                    provenance="unit fixture",
                    privacy_class="synthetic",
                    source_sha256=sha256_text("case-reject-source"),
                    expected_decision="reject",
                    business_weight=Decimal("1"),
                    critical_wrong_decisions=["go"],
                    tags=["boundary"],
                    is_real_case=False,
                ),
            ],
        )

    def test_perfect_synthetic_run_cannot_approve_release(self) -> None:
        dataset = self.build_dataset(real=False)
        predictions = [
            LeadTriagePrediction(case_id="case-go", status="completed", output=output("go")),
            LeadTriagePrediction(
                case_id="case-reject",
                status="completed",
                output=output("reject"),
            ),
        ]

        result = evaluate_lead_triage(dataset, predictions)

        self.assertEqual(result.structured_valid_rate, 1.0)
        self.assertEqual(result.decision_accuracy, 1.0)
        self.assertFalse(result.release_evidence_eligible)
        self.assertFalse(result.release_passed)

    def test_invalid_output_is_a_hard_failure(self) -> None:
        dataset = self.build_dataset(real=True)
        invalid = output("go")
        invalid["requires_full_lead_report"] = "true"
        predictions = [
            LeadTriagePrediction(case_id="case-go", status="completed", output=invalid),
            LeadTriagePrediction(
                case_id="case-reject",
                status="completed",
                output=output("reject"),
            ),
        ]

        result = evaluate_lead_triage(dataset, predictions)

        self.assertEqual(result.structured_valid_rate, 0.5)
        self.assertEqual(result.critical_error_count, 1)
        self.assertFalse(result.release_passed)

    def test_perfect_real_run_without_operational_thresholds_cannot_pass(self) -> None:
        dataset = self.build_dataset(real=True)
        predictions = [
            LeadTriagePrediction(
                case_id="case-go",
                status="completed",
                output=output("go"),
                latency_ms=100,
                cost_units=Decimal("0.2"),
                grounding_review="passed",
            ),
            LeadTriagePrediction(
                case_id="case-reject",
                status="completed",
                output=output("reject"),
                latency_ms=120,
                cost_units=Decimal("0.1"),
                grounding_review="passed",
            ),
        ]

        result = evaluate_lead_triage(dataset, predictions)

        self.assertFalse(result.operational_thresholds_bound)
        self.assertFalse(result.release_evidence_eligible)
        self.assertFalse(result.release_passed)

    def test_real_run_passes_only_with_complete_review_and_operational_evidence(self) -> None:
        dataset = self.build_dataset(real=True)
        predictions = [
            LeadTriagePrediction(
                case_id="case-go",
                status="completed",
                output=output("go"),
                latency_ms=100,
                cost_units=Decimal("0.2"),
                grounding_review="passed",
            ),
            LeadTriagePrediction(
                case_id="case-reject",
                status="completed",
                output=output("reject"),
                latency_ms=120,
                cost_units=Decimal("0.1"),
                grounding_review="passed",
            ),
        ]

        result = evaluate_lead_triage(
            dataset,
            predictions,
            max_p95_latency_ms=150,
            max_average_cost_units=Decimal("0.25"),
            max_fallback_rate=0.0,
            max_human_review_rate=0.0,
        )

        self.assertTrue(result.operational_thresholds_bound)
        self.assertTrue(result.release_evidence_eligible)
        self.assertTrue(result.release_passed)
        self.assertEqual(result.p95_latency_ms, 120)


if __name__ == "__main__":
    unittest.main()
