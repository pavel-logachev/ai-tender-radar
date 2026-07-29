from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.llm.contracts import (
    LeadTriageOutput,
    inspect_lead_triage_output,
    validate_lead_triage_output,
)


def valid_go_payload() -> dict[str, object]:
    return {
        "lead_decision": "go",
        "lead_priority": "high",
        "confidence": "medium",
        "lead_summary": "Есть повод обсудить развитие инфраструктуры.",
        "likely_customer_story": "Заказчик обновляет серверный контур.",
        "possible_needs": ["серверы", "резервное копирование"],
        "target_roles": ["ИТ-директор"],
        "reject_reason": "",
        "requires_full_lead_report": True,
    }


class LeadTriageOutputContractTest(unittest.TestCase):
    def test_valid_exact_payload_passes(self) -> None:
        result = validate_lead_triage_output(valid_go_payload())

        self.assertEqual(result["lead_decision"], "go")
        self.assertEqual(tuple(result), tuple(LeadTriageOutput.model_fields))

    def test_unknown_or_missing_fields_fail_closed(self) -> None:
        extra = valid_go_payload()
        extra["invented_field"] = "unsafe"
        with self.assertRaises(ValidationError):
            validate_lead_triage_output(extra)

        missing = valid_go_payload()
        del missing["confidence"]
        with self.assertRaises(ValidationError):
            validate_lead_triage_output(missing)

    def test_wrong_types_are_not_coerced(self) -> None:
        payload = valid_go_payload()
        payload["requires_full_lead_report"] = "true"
        with self.assertRaises(ValidationError):
            validate_lead_triage_output(payload)

    def test_decision_invariants_fail_closed(self) -> None:
        rejected = valid_go_payload()
        rejected.update(
            lead_decision="reject",
            lead_priority="low",
            reject_reason="",
            requires_full_lead_report=False,
        )
        with self.assertRaises(ValidationError):
            validate_lead_triage_output(rejected)

    def test_shadow_inspection_reports_valid_output_without_payload(self) -> None:
        result = inspect_lead_triage_output(valid_go_payload())

        self.assertEqual(
            result,
            {
                "mode": "shadow",
                "status": "passed",
                "error_count": 0,
                "error_fields": [],
                "error_types": [],
            },
        )

    def test_shadow_inspection_sanitizes_validation_failures(self) -> None:
        payload = valid_go_payload()
        payload["requires_full_lead_report"] = "secret-looking generated value"

        result = inspect_lead_triage_output(payload)

        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["error_fields"], ["requires_full_lead_report"])
        self.assertEqual(result["error_types"], ["bool_type"])
        self.assertNotIn("secret-looking generated value", repr(result))


if __name__ == "__main__":
    unittest.main()
